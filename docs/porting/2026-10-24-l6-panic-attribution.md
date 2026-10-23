# L6 panic 归因：`spawn task failed: NoMemory`

## 结论摘要

- **panic 类型**：帧分配失败。`TaskOptions::build()` 返回
  `Err(NoMemory)`，调用点用 `.expect("spawn task failed")` 把它变成
  panic；不是 `unwrap` 空值、不是断言、不是缺页异常。
- **位置与子系统**：
  - 调用点：`kernel/core/src/thread/task.rs:135`（创建用户线程）；
  - 栈：`TaskOptions::build` → `KernelStack::new_with_guard_page`
    （`ostd/src/task/kernel_stack.rs:63`）→
    `FrameAllocOptions::alloc_segment_with`（`ostd/src/mm/frame/allocator.rs:79`）
    → buddy 帧分配器返回 `None`；
  - 因此栈顶子系统是 **OSTD 帧分配器（osdk-frame-allocator buddy）**，
    由任务创建路径触发；不是 ext4、不是 block I/O、也不是 dentry 代码本身。
- **没有 OOM killer、没有 `MemFree` 类日志**：分配器直接返回 `None`。
- **源码事实**：
  - `alloc_segment_with` 的失败路径没有任何回收动作
    （`get_global_frame_allocator().alloc(layout).ok_or(Error::NoMemory)?`）；
  - 全仓没有 page cache 的全局 shrinker/LRU 回收路径（搜索
    `shrinker`/`reclaim` 只命中 exfat 的局部 LruCache）；
  - negative dentry 只有 debug 计数（`NEGATIVE_ENTRY_LIMIT = 10000`，
    `dentry.rs:1129`），超过只 `warn!`，**没有淘汰**。
- **最可能假设**：升级期间的 page cache（下载的 `.deb`、解包出的文件、
  原 rootfs 页面）在 4 GiB guest 上持续增长且没有压力回收，最终耗尽
  帧分配器；内核栈需要 **128 个连续物理页（512 KiB）**，成为第一个失败
  的分配点。负 dentry 增长（10,001 → 43,500）是同一压力下的伴随现象，
  单看字节量（几 MB 级）不太可能是唯一原因。
- 崩溃后副本仍可启动（`forky/sid`、systemd running、可登录），说明
  panic 没有留下不可恢复的磁盘状态。

## 直接证据

存于 [`evidence/2026-10-24-l6-panic/`](evidence/2026-10-24-l6-panic/)：

- `panic.txt`：`Uncaught panic` 前 40 行到 console 末尾（含调用栈原始
  转储；SMP 控制台对两份 panic 输出有交织，文件末尾被截断）；
- `context.txt`：时间线、进度、dentry 计数、E2BIG 计数。

关键原文：

```
[ 3427.512] ERROR: Uncaught panic:
    spawn task failed: NoMemory
    at /home/arch-anjie/Program/asterinas-riscv/kernel/core/src/thread/task.rs:135
Printing stack trace:
```

时间线与进度：

| 项 | 值 |
|---|---|
| panic 时间 | guest **3427.512 s**（约 57 分钟） |
| 升级进度 | **321/341** `Unpacking`、**78** `Setting up`（最后 `libxcursor1`） |
| E2BIG | **0** 条 |
| negative dentry 首个警告 | t=336.264 s，count=10001 |
| negative dentry 最后警告 | t=3427.471 s，count=43500 |
| negative dentry 警告行数 | **75,969** |

## 源码路径

| 位置 | 作用 |
|---|---|
| `kernel/core/src/thread/task.rs:127-135` | `TaskOptions::build().expect("spawn task failed")`，用户线程创建 |
| `ostd/src/task/mod.rs:205-230` | `build()` 先分配内核栈，`?` 传播 `NoMemory` |
| `ostd/src/task/kernel_stack.rs:20-70` | `DEFAULT_STACK_SIZE_IN_PAGES = 128`（512 KiB），`alloc_segment_with(128, …)` 需要连续帧 |
| `ostd/src/mm/frame/allocator.rs:79-96` | `alloc_segment_with` → buddy `alloc(layout)` → `ok_or(Error::NoMemory)`，**无回收** |
| `osdk/deps/frame-allocator/src/lib.rs` | buddy 实现；有 `TOTAL_FREE_SIZE` 计数器（可用于观测） |
| `kernel/core/src/fs/vfs/path/dentry.rs:1129-1189` | 负 dentry 上限 10,000，仅 debug warn，无淘汰 |
| `kernel/core/src/vm/page_cache/*` | 有显式 `evict` 操作，但没有内存压力触发的全局 shrinker |

## 假设与验证（按可能性排序）

### H1（最可能）：page cache 无压力回收，4 GiB 帧被吃光

**假设**：`.deb` 下载、解包出的文件、旧 rootfs 页面都留在 page cache；
帧分配器在升级末尾接近 0，创建线程时 128 页连续分配失败。

**验证（任一，成本从低到高）**：

1. 在 `task.rs:135` 的 `expect` 前加一行观测（不改行为）：

   ```rust
   info!("free frames at spawn failure: {}", osdk_frame_allocator::load_total_free_size());
   ```

   再每 60 s 打一次同一个计数器；重跑 L6（≤60 分钟）。
   若 panic 前 free size 接近 0 → H1 成立；
2. 把 guest 内存从 4 GiB 提到 8 GiB 重跑
   （`boot_debian.sh --memory 8G`，gate 目前固定 4G，需要临时加参数）；
   若升级走完 → “容量/回收”方向成立；
3. 在 page cache 里加入压力回调/shrinker（行为改动，留到下一步）。

### H2：连续帧碎片化（总空闲不为 0，但拿不到 512 KiB 连续块）

**假设**：buddy 分配器总空闲仍有余量，但 128 个连续页找不到；升级期间
大量小分配把物理内存切碎。

**验证**：

1. 同 H1 的 free-size 观测：若 panic 时 `TOTAL_FREE_SIZE` 仍很大
   （例如 >100 MB）而分配失败 → H2；
2. 用构建期环境变量把内核栈改小（不改变内核逻辑）：

   ```sh
   OSTD_TASK_STACK_SIZE_IN_PAGES=16 cargo osdk build …
   ```

   重跑 L6；若 panic 消失或大幅推迟 → 连续分配压力成立。

### H3：任务/内核栈泄漏

**假设**：apt/dpkg 大量 fork/exec，内核栈或 task 元数据未释放，空闲帧
单调下降。

**验证**：

1. 观测每 60 s 的 `TOTAL_FREE_SIZE`；若在**没有升级**、只有
   fork/exec 压力时也单调下降 → 泄漏；
2. 源码上 `KVirtArea::drop` 会 unmap 并释放各 fragment
   （`ostd/src/mm/kspace/kvirt_area.rs:205`），因此这条可能性最低，但
   需要计数验证。

## 结论与下一步

- 归因：**帧分配器耗尽（可能叠加连续帧碎片化），触发点是创建用户线程
  时的 128 页内核栈分配**；page cache 缺少压力回收是最可能的根因，
  负 dentry 缓存无淘汰是次要因素。**与 E2BIG 无关**（同轮 0 条）。
- 下一步（按成本）：
  1. 加 `TOTAL_FREE_SIZE` 观测（<10 行）并重跑，区分 H1/H2/H3；
  2. 若要临时跑通 L6：提高 guest 内存到 8 GiB，或把内核栈降到
     16–32 页（构建期变量）；
  3. 长期修复：为 page cache 增加内存压力回收/上限；负 dentry 增加
     淘汰或速率限制（目前 75,969 条 warn 也把 console 撑到 10 MB）。
