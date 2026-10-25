# ring-buffer NoMemory unwrap panic：定位与修复

日期：2026-10-25　改动前 HEAD：`1d27beab3`
验证内核：当前树重建（含本修复）sha256 前缀 **`20582f4b207b6cb1`**
镜像：`target/debian-x86-toolchain3/rootfs/debian-root.ext2`

## 结论摘要

- **已修复**：`RingBuffer::new` 在任何分配路径失败时**不再 panic**；最后一页也
  分配不出来时降级为容量 0 的 `Storage::Empty`，并打印可观测 `warn!`。
- 旧 `prime` 内核（`9e8ee5dd`）在 `lib.rs:104` 的 unwrap panic，源于
  64 KiB 连续分配失败；当前树早已用「预留池 → 分段回退 → 容量减半」
  （`99d4b116e`）替换了那条路径。本提交清掉剩下的最后兜底 `panic!`。
- **GUI 重负载复验（A）**：无 panic；10 个应用全部起窗；1.5 GiB I/O 成功；
  suspend **3/3**；`MemAvailable` 3,398,356 → 3,288,172 kB（**-3.24%**）。

## 1. `:104` 的调用链

**旧 prime 内核（`9e8ee5dd`）**：

```
RingBuffer::new(capacity)
  -> FrameAllocOptions::...alloc(nframes)   // 64 KiB 类，要求连续帧
  -> .unwrap()                              // lib.rs:104
  -> Err(NoMemory) => panic
```

在 10 个 GUI 客户端 + 2 GiB I/O 的高压/碎片化下，连续帧拿不到 → unwrap
panic（串口 3 份多 CPU 输出）。

**当前树（`99d4b116e` 之后）**：

```
RingBuffer::new(capacity)
  -> nframes > POOL_MIN_PAGES ? ReservedPoolBuffer::alloc(...)   // 预留池
  -> 否则/池未命中: alloc_segmented(nframes)                     // 分段、虚拟连续
  -> 连续失败且 nframes > 1: capacity /= 2，重试
  -> 连 1 页也失败: panic!("ring buffer: cannot allocate ...")   // 本次修复点
```

也就是说：旧的 64 KiB unwrap 已被「池 + 分段 + 减半」消除；当前树剩下的
panic 只在“连 1 页分配都失败”时发生。

## 2. 选定方案：S1 降级（保留 `new` 签名）

- **S2（返回 `Result`）**需要把 `RingBuffer::new` 改成 fallible，并改动
  pty、tty line discipline、evdev、Unix stream、pipe 等 5 处
  结构体初始化/构造调用，属于较大重构；按任务纪律不擅自做大改。
- **S1（降级）**保持 `new() -> Self` 不变：最后一页也失败时返回
  `capacity = 0` 的 `Storage::Empty`，用 `ostd::warn!` 记录：
  ```
  ring-buffer: cannot allocate <n> page(s) for a capacity of <c>: <err>;
  degrading to an empty ring buffer
  ```
  调用方通过 `capacity() == 0` / `push() == None` 观察降级；不会 panic。
- 细节：
  - `Storage::Empty` 的 `size()` 为 0，reader/writer 是零长度；
  - `Producer::push/push_slice`、`Consumer::pop/pop_slice` 在 `capacity == 0`
    时直接返回 `None`（避免 `capacity - 1` 下溢和空写）；
  - 只有非 2 的幂的容量仍 `assert!`（参数错误，不是 NoMemory）。

## 3. 复验：GUI 重负载（1.5 GiB I/O + 10 应用 + 3 次休眠）

内核重建后（`20582f4b207b6cb1`，OSDK microvm，含 aster-drm-virtio-gpu），
用同一 `desktop-drm-toolchain` 镜像和 GUI 脚本重跑；I/O 按 `/dev/shm`
容量改为 1.5 GiB / 10 分钟保持（在 45 分钟任务上限内）。

| 项 | 结果 |
|---|---|
| Xorg / DRI3 | `DAILY_DESKTOP=ok Xorg+DRI3` |
| 10 个客户端 | 全部启动：pcmanfm×2、eog×2、xterm×3、xclock/xcalc/xeyes 各 1 |
| 窗口计数 | `gui_windows total=14`（xwininfo 把子窗口一并计入：pcmanfm 4、eog 4、xterm 3、x11-apps 各 1；10 个应用均在） |
| 1.5 GiB I/O | `/dev/shm`（2,084,864 KB），`dd_rc=0 sha_rc=0`，sha256 `b7a1ca05…` |
| MemAvailable | 3,398,356 → 3,288,172 kB（**-3.24%**，<5%） |
| suspend | **3/3 ok**（rc=0，约 6–7 s 唤醒；窗口 14/13/13 存活；探测窗正常） |
| panic | 串口 **0** 个真实 panic 块（`ERROR : Uncaught panic` = 0，unwrap = 0） |
| ring-buffer 日志 | `pool hit: pages=16` 累计 **1024**；降级 warn **0** 次 |
| heap 池 | 峰值 2090/4096 页，未耗尽 |
| 时长 | 639.6 s（10 分钟保持 + 启动 + 3 次休眠） |

**判定：A（修复有效）**——无 panic，10 应用起窗，I/O 与 3 次休眠均通过。

### 观察到的应用级问题

- `xterm -e ...` 报 **`open ttydev: Inappropriate ioctl for device`**：
  xterm 窗口能起，但内部的 `-e` 命令（pty/TIOCSCTTY 路径）失败。这是独立的
  tty/pty 兼容性问题，与 ring-buffer panic 无关，记录待查。
- 该 microvm 内核的 `syslog`/`dmesg` 未实现（`read kernel buffer failed:
  Function not implemented`），所以脚本里的 `dmesg_count[...]` 是空判据；
  本报告的 panic 结论以**串口日志**为准。
- 旧 `prime` 内核仍是修复前的构建；日常门禁若继续用它，会复现旧 panic，
  应从当前树重建。

## 4. 证据

- `target/gui-fixed/console.txt`（串口全量）
- `target/gui-fixed/host.log`
- 归档：`docs/porting/evidence/2026-10-25-ringbuf-nomemory-fix/console-excerpt.txt`
- 修复代码：`kernel/libs/ring-buffer/src/lib.rs`
