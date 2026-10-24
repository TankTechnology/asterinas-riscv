# 帧分配器饥饿：`free_local=200 MB` 却分不出 1 页

日期：2026-10-24　基线：`1406bd76d`（多段内核栈）+ 本轮诊断与回退修复

## 结论摘要

- **现象**：4 KiB（order 0）分配失败，而失败瞬间本地池还有
  `free_local=200,302,592 B`（191 MiB）、全局池 `free_global=890,032,128 B`
  （849 MiB）。随后页错误返回 `ENOMEM`，堆分配器报
  `Failed to allocate a large slot` / `Heap allocation error`
  （layout 4 KiB），内核中止。
- **根因**：不是没有内存，也不是没有单页。1 页请求会先走 per-CPU 的
  1 页缓存；**缓存为空时，回填一次要 8 个连续页（order 3）**
  （`osdk/deps/frame-allocator/src/cache.rs:55-63`）。当时本地池与全局池的
  order ≥3 链表都是空的，回填失败；而 `cache::alloc` **没有退回按原 layout
  直接分配**，于是 1 页请求整个失败
  （修复前 `cache.rs:130-146`）。
- **判定：A（分配器逻辑 bug）**，不是 B（本地池不回填——它确实尝试了回填），
  也不是 C（真碎片——1 页请求只需要 order 0，而 order-0 块有 40,058 个）。
  更准确地说，是 **“回填路径的连续块要求过高 + 失败后没有降级”**。
- **最小修复**：缓存类分配失败时，退回 `pools::alloc(guard, layout)`
  按原始 layout 分配（`cache.rs:139-151`，6 行）。1 页请求只需要一个
  order-0 块，正好有大量现货。
- **验证**：第二次 L6 复测中，`cache refill failed` 出现了 8 次
  （order-3 用尽），但 **`size=4096` 的 `allocation failed` 为 0 次**——
  回退路径把每一次 1 页请求都救回来了。第三次复测把栈段降到 4 页
  （order 2）后，`spawn task failed` 为 **0**，升级**解包 341/341、
  配置 154**；随后在 `RingBuffer::new(65536)` 的 **order-4** 分配上
  panic（`ring-buffer/src/lib.rs:104`），这是同一族问题的第三个分配点。
  详见文末“修复后 L6”。

## 现象与证据

### 失败时刻

来自多段内核栈那一轮 L6（内核 `5644aff792f48c0d`）的串口：

```
[  3531.465] WARN  : frame-allocator: allocation failed: size=4096 align=4096
    free_local=200302592 free_global=890032128
    chunks_local=[40058, 3108, 657, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    chunks_global=[3, 49429, 29608, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, ...]
[  3531.465] WARN  : page fault handler failed: ... errno: ENOMEM
[  3531.488] ERROR : Failed to allocate a large slot
[  3531.488] ERROR : Heap allocation error, layout = Layout { size: 0x1000, align: 1 }
```

（`chunks_*` 是本轮新增的每阶空闲**块数**；`largest_local_order=2`、
`largest_global_order=2` 也是新增字段。）

### `free_local` 统计的是什么

`free_local` = 当前 CPU 本地池 `BuddySet::total_size()`，即**所有阶的
块大小之和**（`pools/mod.rs` 的 `alloc` 与 `set.rs` 的 `total_size`）。
它**包含各阶**，不是只算单页块；也不包含 per-CPU 的 1–4 页缓存。
逐阶相加与打印值完全吻合：

| order | 块数 | 单块字节 | 小计 |
|---:|---:|---:|---:|
| 0 | 40,058 | 4,096 | 164,077,568 |
| 1 | 3,108 | 8,192 | 25,460,736 |
| 2 | 657 | 16,384 | 10,764,288 |
| **合计** | 43,823 | — | **200,302,592** |

（机器可读版本：[`evidence/2026-10-24-frame-starvation/pre-fix-distribution.txt`](evidence/2026-10-24-frame-starvation/pre-fix-distribution.txt)）

### 1 页分配的完整查找路径

```
FrameAllocator::alloc(layout 4096/4096)
  └─ cache::alloc(guard, layout)
       ├─ nr_frames = 1 → cache1.alloc(guard)
       │    ├─ 缓存命中 → 返回
       │    └─ 缓存空 → 回填：
       │         pools::alloc(Layout(8 × 4096))       ← order 3
       │           ├─ local_pool.alloc_chunk(3)  → None（lists[3..17] 全空）
       │           └─ global_pool.alloc_chunk(3) → None（global lists[3..] 全空）
       │         → 回填失败
       └─ （修复前）直接返回 None，不尝试 pools::alloc(layout 4096/4096)
```

关键点：

1. **回填尺寸被放大**：`CacheArray<1, 12>` 的 `COUNT * 2 / 3 = 8`
   （`cache.rs:55`），所以“拿 1 页”失败路径上实际请求 **8 个连续页**。
   2 页缓存回填 4 段 × 2 页 = 8 页（同样 order 3）；
   3 页缓存回填 4 段 × 3 页 = 12 页 → order 4；
   4 页缓存回填 4 段 × 4 页 = 16 页 → order 4。
2. **buddy 本身没有分桶 bug**：`BuddySet::alloc_chunk(order)` 从 order 开始
   向上找**任意更大的块**并逐级拆分（`set.rs:63-100`）。问题是它收到的是
   order 3，而不是 order 0。
3. **没有跨阶降级**：`cache::alloc` 只看“本次缓存类分配是否成功”，失败就
   向上返回，不会按原始 layout 再试一次。这是本次饥饿的直接原因。
4. 堆层只是受害者：`HeapSlot::alloc_large(4096)` 用
   `alloc_segment_with(1)` 申请一页，失败后打
   `Failed to allocate a large slot`（`ostd/src/mm/heap/slot.rs:76-97`）；
   `handle_alloc_error` 再 `abort`（`ostd/src/mm/heap/mod.rs:86-91`）。

## 判定

| 候选 | 判定 | 证据 |
|---|---|---|
| **A. 分桶/合并/回退 bug** | **成立（本因）** | order-0 有 40,058 个块、order-1 有 3,108 个、order-2 有 657 个；1 页请求本可成功，失败只因回填要 order 3 且无降级 |
| B. 本地池不回填 | 不成立 | 本地池在失败时非空，回填确实被调用；失败原因是回填请求的**尺寸**，不是没有回填 |
| C. 真碎片（各阶都空） | 不成立 | 低阶有大量空闲；真正空的是 order ≥3，而对 1 页请求而言 order 3 是不必要的 |

补充：order ≥3 为空本身是**真实的碎片现象**（大量小对象把物理内存切成
低阶块），但它只影响“缓存回填”这种放大请求；对 1 页请求不构成真实短缺。
因此本轮的修复方向是**去掉不必要的放大与增加降级**，而不是立刻上
page cache shrinker（那是后续的总量/高阶问题）。

## 最小修复（已实现）

`osdk/deps/frame-allocator/src/cache.rs:139-151`：

```rust
let cached = match nr_frames {
    1 => cache.cache1.alloc(guard),
    2 => cache.cache2.alloc(guard),
    3 => cache.cache3.alloc(guard),
    4 => cache.cache4.alloc(guard),
    _ => return super::pools::alloc(guard, layout),
};
drop(cache);

// 回填要更大的连续块，可能失败；退回按原始 layout 分配。
cached.or_else(|| super::pools::alloc(guard, layout))
```

为什么这是最小且正确的：

- **只改失败路径**：缓存命中/回填成功时行为完全不变；
- **足够**：1 页请求 = order 0；失败时本地池有 40,058 个 order-0 块，
  直接分配必然成功。2 页（order 1）、3–4 页（order 2）同样有现货；
- **不引入新策略**：不预留、不跨 CPU 偷块、不改变回填尺寸；只是把
  “放大请求失败”降级为“原始请求”；
- 代价：碎片化时绕过缓存，pool 流量略增；正确性不变。

其他候选（未采用）：

| 方案 | 位置 | 取舍 |
|---|---|---|
| 缩小回填尺寸（如每次只回填 1 段） | `cache.rs:55` | 减少放大，但缓存收益下降；仍需降级兜底 |
| 回填失败时先合并各 CPU 本地池到全局再重试 | `pools/mod.rs:73-75` 的 TODO | 解决“高阶块在别的 CPU 池里”，但解决不了“全局也没有 order 3”；可作为第二层 |
| page cache shrinker / 压力回收 | 内核 page cache | 解决总量与高阶碎片，工作量大；本轮不做 |

### 诊断日志的限流

回填失败本身不是错误（修复后它会降级并成功），但一旦 order 3 用尽，
**每次 1 页分配都会重试回填并失败**。若每次都打印，控制台会被淹没，
所以 `cache.rs` 只打印**前 8 次**回填失败（`REFILL_FAILURE_REPORTS`），
其余静默；真正的“整单失败”仍由 `lib.rs` 的 `allocation failed` 日志
（低频）完整记录各阶分布。

## 修复后 L6

内核 `2d5fab6d62b1d9e7`（诊断 + 回退修复），同配置重跑：

| 项 | 值 |
|---|---|
| 基础/副本 sha | `450ebe3b…a083e0`（一致） |
| 可升级包 | 297 |
| 结果 | **在 t=3458.752 s 再次 panic**，但**不是** 4 KiB 失败 |
| 失败请求 | `size=65536 align=4096` → **order 4（16 页）**，来自内核栈的 16 页段 |
| 失败瞬间 | `largest_local_order=3`、`largest_global_order=3`，free 合计约 1.66 GB |
| 4 KiB 分配失败 | **0 次**（`cache refill failed` 0 次，`size=4096` 的失败 0 次） |
| 进度 | 311/341 解包、78 配置 |
| panic 原文 | `spawn task failed: NoMemory free_frames_bytes=1662894080`（CPU 3） |

失败瞬间的各阶分布（`order=4` 请求）：

```
chunks_local =[36851, 3740, 914, 1, 0, ...]   # order 0..3
chunks_global=[2, 50645, 29080, 1, 0, ...]    # order 0..3
free_local=196587520  free_global=891371520
```

判读：

1. **4 KiB 饥饿没有复现**——在本轮到达 3531 s 之前，内核先在
   order-4 栈段上 panic（3458.8 s），所以回退修复**没有被真正执行到**；
   这轮无法证明它有效，也没有证伪它（它的静态证据仍然成立：
   失败瞬间 order-0 块有 36,851 个，1 页请求本可成功）。
2. **新的失败是 order-4 的真碎片**：请求 16 个连续页，而本地/全局的
   最大块都只有 order 3（各 1 个）。这不是 A（没有可降级的低阶请求——
   16 页不能由 order-3 块拼出），而是**对 order-4 而言的 C（真碎片）**。
   16 页段是上一轮内核栈改动引入的；把段再拆小即可绕开（见下）。
3. 16 页段让 panic 从 3340 s 推迟到 3459 s，但在这个碎片水平上仍会失败。

### 第二次复测（L6KS3：验证 4 KiB 回退）

同一内核 `2d5fab6d62b1d9e7`，换 workspace 再跑一轮
（`target/debian-upgrade-verify`，基础/副本 sha 仍为 `450ebe3b…`）：

| 项 | 值 |
|---|---|
| `cache refill failed`（打印数） | **8 次**（限流上限），全部 `class_frames=1 refill_bytes=32768 refill_order=3` |
| `allocation failed: size=4096` | **0 次** |
| `allocation failed: size=65536`（order 4） | **1 次** → panic |
| panic | t=**3577.773 s**，`spawn task failed: NoMemory free_frames_bytes=1622347776` |
| 失败瞬间各阶 | local `[38774, 3202, 1070, 0, …]`、global `[2, 44727, 28437, 0, …]`；`largest_*_order=2` |
| 失败瞬间余量 | `free_local=202579968`、`free_global=832323584`（合计 1.62 GB） |
| 进度 | **333/341** 解包、78 配置 |

判读：

1. **回退修复生效（已直接验证）**：8 次回填失败（order 3 为空）全部被
   `pools::alloc(guard, layout)` 降级救回，`size=4096` 的整单失败为 **0**。
   这确认了判定 A：问题不在“没有单页”，而在“回填放大 + 无降级”。
2. **panic 仍在 → 本轮判定 C**，但原因与 4 KiB 回退无关：失败请求是
   **order 4（16 页）** 的内核栈段，而失败瞬间最大可用阶只有 order 2
   （本地 1,070、全局 28,437 个 order-2 块）。16 页无法由低阶块拼出。
3. 与第一次复测（3458.8 s、最大阶 3）相比，panic 推迟到 3577.8 s，
   解包数从 311 回到 **333**（与未加回退时的最好成绩持平）——
   回退既没有让升级倒退，也不再是阻塞点。

### 第三次复测（SEG4：4 页栈段，order 2）

栈段从 16 页降到 **4 页（32 段）**，内核 `924faa3c9c01538b`，同配置重跑：

| 项 | 值 |
|---|---|
| `spawn task failed` | **0 次**——栈段的 order-4 panic 消失 |
| `cache refill failed` | 8 次（t=3579.8，order 3 空），全部由回退救回 |
| `allocation failed: size=4096` | 0 次 |
| 解包 / 配置 | **341/341** / **154**（历史最高） |
| 新 panic | t=**3899.827 s**，`called Result::unwrap() on an Err value: NoMemory`，位置 `kernel/libs/ring-buffer/src/lib.rs:104` |
| 失败请求 | `size=65536 align=4096` → **order 4（16 页）**，来自 `RingBuffer::new(65536)` |
| 失败瞬间各阶 | local `[30083, 4816, 1745, 8, 0, …]`、global `[2, 45619, 28307, 1528, 0, …]`，`largest_*_order=3`；空闲合计约 1.62 GB |

判读：

1. **内核栈的 order-4 问题已解决**：没有一次 `spawn task failed`，
   升级把 **341 个包全部解包**、配置了 **154** 个，远超前两轮
   （303/311/333 解包、78 配置）。4 页段（order 2）在失败瞬间仍有
   1,745（本地）+ 28,307（全局）个 order-2 块可用，足以支撑栈分配。
2. **仍有 order-4 panic → 本轮判定 C**，但分配点已不是内核栈，而是
   **环形缓冲**：`RingBuffer::new(capacity)` 用**单个连续 Segment**
   承载 `capacity × size_of::<T>()` 字节（`kernel/libs/ring-buffer/src/lib.rs:96-105`）。
   失败请求 65,536 B = 16 页；默认管道缓冲
   `DEFAULT_PIPE_BUF_SIZE = 65536`（`kernel/core/src/fs/pipe/common.rs:362`）
   与 Unix stream 默认缓冲 `UNIX_STREAM_DEFAULT_BUF_SIZE = 65536`
   （`kernel/core/src/net/socket/unix/stream/connected.rs:446`）都恰好是
   16 页。升级上下文（dpkg 配置阶段大量 fork/管道）更像管道创建；
   ELF 已 strip，地址无法解析到具体调用点。
3. 结论：**同一族问题的第三个分配点**——order-7 栈（已修）、order-4 栈
   （已修）、order-4 环形缓冲（新暴露）。碎片本身没有变，只是每次把
   要求降一档，下一个“必须连续 16 页”的调用者就浮出来。

### 下一步（按优先级）

1. **环形缓冲多段化或降级**：`RingBuffer::new` 目前要求一段连续物理内存。
   两条路：
   - 把缓冲改成多段（数组 of `Segment`，或映射到连续虚拟区后用虚拟地址
     索引），与内核栈多段映射同源，改动中等；
   - 创建失败时把容量降一档（如 16 KiB / 4 页）重试，改动小但会改变
     管道语义（Linux 允许 pipe 容量协商，`F_SETPIPE_SZ` 已有）。
   推荐先做容量降级作为止血，再做多段化。
2. **再重跑 L6**：确认配置阶段能走完（`UPG stage=upgrade rc=0` 与
   `debian_version` 变化），并验证二次启动。
3. **分配器第二层**：回填失败时归并其它 CPU 的本地池到全局再重试
   （`pools/mod.rs:73-75` 的 TODO）；对 order-4 这类请求，只有当相邻的
   自由块在别的 CPU 池里时才有效。
4. **总量/高阶**：page cache 压力回收/上限；这是唯一能让 order 4+ 长期
   存在的机制，但工作量大，放在后面。

## 复现

```sh
# 失败版本（多段内核栈，无回退）
git checkout 1406bd76d -- osdk/deps/frame-allocator/src
# 修复版本：当前工作树
make test_x86_debian_full_upgrade \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/systemd-health/daily.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm \
  FULL_UPGRADE_WORKSPACE=$PWD/target/debian-upgrade-alloc
```

## 产物

- [`evidence/2026-10-24-frame-starvation/pre-fix-failure.txt`](evidence/2026-10-24-frame-starvation/pre-fix-failure.txt)：
  修复前的分配失败与堆错误原文；
- [`evidence/2026-10-24-frame-starvation/pre-fix-distribution.txt`](evidence/2026-10-24-frame-starvation/pre-fix-distribution.txt)：
  各阶块数与 `free_local` 的算术核对；
- 修复前完整串口：`target/debian-upgrade-kstack/console.txt`（10.4 MB）；
- 修复后完整串口：`target/debian-upgrade-alloc/console.txt`（10.2 MB）；
- [`evidence/2026-10-24-frame-starvation/post-fix-frame-series.tsv`](evidence/2026-10-24-frame-starvation/post-fix-frame-series.tsv)：
  修复后 29 点帧余量；
- [`evidence/2026-10-24-frame-starvation/post-fix-order4-panic.txt`](evidence/2026-10-24-frame-starvation/post-fix-order4-panic.txt)：
  order-4 失败的分配日志、panic 与调用栈。

## 限制

- 本轮修复只消除“放大请求失败”这一层；page cache 仍无压力回收，
  内存总量仍会缓慢下降，后续可能在高阶或真正的总量耗尽处再次失败；
- `TOTAL_FREE_SIZE` 与各阶统计是跨 CPU 汇总/当前 CPU + 全局的近似，
  但本轮的结论只依赖“order 0 块是否存在”，与近似无关。
