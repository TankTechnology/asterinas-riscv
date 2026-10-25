# 并行哈希 ~5 文件/秒的归因：调度切换自旋，被 flock 争用触发

日期：2026-10-25　内核：诊断用 `548e5f0debee52c0`（tmpfs 修复后）；
修复后 `923ca5c53ea46e02`
镜像：`target/debian-x86-toolchain3/rootfs/debian-root.ext2`（2 GiB）
guest：4 vCPU / 8 GiB / KVM；串口 `loglevel=4`

## 结论摘要

- **主因：A（调度/唤醒开销）**——阻塞线程在
  `ostd/src/task/processor.rs` 的 `before_switching_to()` 里
  **自旋等待** `switched_to_cpu`，串口刷出
  `WARN: Switching to a task already running in the foreground`。
- **触发条件：C（锁/同步原语争用）**——agent 的工具给**每个线程**一个
  输出 fd，`flock` 因此有 16 个互不相同的 owner；同样 200 个文件、
  同样的 hash+写记录逻辑，**每进程共享一个 fd（4 个 owner）只要
  0.13 s，每线程一个 fd（16 个 owner）要 95 s**。
- **B（文件 I/O）排除**：单线程读 5,126 文件/s、sha256 32.4 MB/s、
  `dd` 读 129.7 MB <1 s、`xargs -P8 sha256sum` 24,270 文件 74.4 s。
- **D（fork/exec）排除**：fork 100 次 1.55 s（64.6/s）、exec 50 次
  0.30 s（166.6/s）、线程创建 100 次 0.25 s（392.5/s）；工具只 fork 4 个
  进程。
- 机制：`wake_all()` 在每次 unlock 唤醒全部 15 个等待者 → 调度器在同一
  时刻对多个任务做切换决策 → 同一任务被两个 CPU 同时选中，其中一个 CPU
  在 `before_switching_to()` 里空转，等待另一个 CPU 完成切换；空转期间
  **不睡眠、不发生上下文切换**，但烧掉 130%+ 的 CPU（guest 共 400%）。
- **已修复触发条件**（`bd904ff64`）：把 flock 的等待队列改为每个 inode
  一个、unlock 只 `wake_one()`。同一 200 文件 4×4 基准从 **95.15 s →
  0.151 s**，agent 工具从 **56.38 s → 0.287 s**；全量 `/usr`（21,657 文件 /
  990.6 MiB）从“1,232 s 只完成 5,384 条”变为 **31.66 s 全部完成、0 错误，
  且与 `sha256sum` 基线逐条一致**；内核 `923ca5c5` 复验。
- 调度器侧的双重选中竞态（issue #1633）仍在，但被 flock 唤醒风暴放大
  的路径已消除；调度器修复方案仍列在第 4 节，需要单独的 ktest 与压测。

## 1. 现象复现（最重要的对照）

同一台 guest、同一份 200 个小文件（1.36 MB）、同一个 uid 1000、
同一套 hash+flock+两次 `write` 逻辑：

| 变体 | flock owner 数 | 耗时 | 吞吐 | 系统时间 | 上下文切换 |
|---|---|---|---|---|---|
| `perf_bench --mode lock` 4 进程×4 线程（**每进程共享一个 fd**） | 4 | **0.13 s** | 1,538 文件/s | 0.18 s | 0 |
| `perf_bench --mode lock_tfd` 4×4（**每线程自己的 fd**） | 16 | **95.15 s** | **2.1 文件/s** | 128.3 s | 0 |
| agent 的 `stress_hash.py` 4×4（每线程自己的 fd） | 16 | **56.38 s** | **3.5 文件/s** | 未采 | 未采 |
| `--mode lock_tfd` 4 进程×1 线程 | 4 | 0.083 s | 2,403 文件/s | 0.05 s | 0 |
| `--mode lock_tfd` 1 进程×4 线程 | 4（同进程） | 1.38 s | 145 文件/s | 1.70 s | 0 |

关键读数：

- 95.15 s 那次，worker 0 在 **0.0416 s** 就完成，worker 1/2/3 各耗
  95 s，**系统时间 38.9 s / 44.8 s / …**，`ru_nvcsw = ru_nivcsw = 0`；
- CPU 利用率 134.9%（单核百分比，guest 有 4 核）→ 大量 CPU 被占着，
  但线程**从未让出**（`ru_nvcsw/ru_nivcsw = 0`；Asterinas 的 rusage 计数
  可能不完整，所以结论以 `stime` 与串口 WARN 为准，两者一致）；
- 串口同时出现 `Switching to a task already running in the foreground`：
  本次 62 次，全量 /usr 跑 414 次，隔离跑 22 次。

## 2. 六项隔离测量

样本：`/usr/lib` 中 200 个 ≤64 KiB 的文件，共 1.36 MB；全部以
uid 1000 运行；工具 `tools/x86/debian/perf_bench.py`，
`cpu%` 为“单核百分比”（4 核上限 400%）。

| # | 测量 | 命令要点 | 耗时 | 吞吐 | CPU% | 备注 |
|---|---|---|---|---|---|---|
| 1 | 基线（1 进程 1 线程） | `--mode hash --procs 1 --threads 1` | 0.0436 s（warm；冷 0.1438 s） | 4,585 文件/s；29.8 MiB/s | 45.9% | 单线程本身正常 |
| 2 | 多进程 | `xargs -P8 sha256sum` | 1 s（200 文件）；/usr 全量 74.4 s | 24,270 文件 / 1,093.5 MiB | — | 进程并行正常 |
| 3 | 多线程 | `--mode hash --procs 1 --threads 8` | 0.1075 s | 1,861 文件/s | 113.5% | 相对 1×1 **无加速** |
| 4 | 4 进程×4 线程 | `--mode hash --procs 4 --threads 4` | 0.11 s | 1,819 文件/s | 123.7% | 无加速，CPU 远未饱和 |
| 4b | 4×4 + flock（共享 fd） | `--mode lock` | 0.13 s | 1,538 文件/s | 136.1% | 4 个 owner |
| 4c | 4×4 + flock（每线程 fd） | `--mode lock_tfd` | 95.15 s | 2.1 文件/s | 134.9% | **16 个 owner，崩掉** |
| 5 | 上下文切换/系统调用 | `getrusage` 计数 + 串口 WARN | — | — | — | 慢跑全程 **nvcsw=nivcsw=0**，stime 39–45 s/worker |
| 6 | CPU 利用率 | 由 rusage 算 | — | — | 124–135%（上限 400%） | CPU 有 2/3 空闲，线程在核内自旋 |
| 附 | fork 100 / thread 100 / exec 50 | `--mode fork/thread/exec` | 1.55 / 0.25 / 0.30 s | 64.6 / 392.5 / 166.6 次/s | — | 进程启动不慢 |
| 附 | 大文件 I/O | `sha256sum libLLVM.so.19.1`（129.7 MB）；`dd` 读 | 4 s；<1 s | 32.4 MB/s；页缓存命中 | — | I/O 不慢 |

### flock 微基准（锁本身，不含 hash）

`flock_rate.py`：每个线程对同一个文件做 N 次 `LOCK_EX / write / UNLOCK`。

| 变体 | owner 数 | 结果 |
|---|---|---|
| own 4×4（每线程 fd） | 16 | 320 次加锁在 180 s 内**未完成**；`timeout` 杀掉 Python 父进程后，4 个已 fork 的子进程仍在核内自旋，guest 基本失去响应，本 gate 只能终止（未能测得后续 shared/4×1 行） |
| own 4×1 的等价物 | 4 | 用 `perf_bench --mode lock_tfd --procs 4 --threads 1` 测得 **0.083 s**，完成 |

即：锁数量相同，仅仅 owner 数从 4 变到 16，就从毫秒级退化到 >180 s。
慢的是 flock 的等待/唤醒路径本身，不是 hash。（gate 被自旋进程卡住本身
也是一条严重性证据；复现时应用 `timeout -s KILL` 并杀整个进程组。）

## 3. 归因与证据链

1. **B 排除**：单线程 hash/read 都很快，`xargs` 全量 74.4 s，大文件
   32 MB/s；没有慢 I/O。
2. **D 排除**：fork/exec 每秒数十到数百次，工具只 fork 4 次。
3. **C 是触发条件**：把每线程独立 fd 改成每进程共享 fd，耗时从 95 s
   → 0.13 s（700 倍）；1 进程 4 个 fd 也从 0.1 s 级退到 1.38 s，
   说明争用随 owner 数增长。
4. **A 是机制**：慢跑期间 `ru_nvcsw = ru_nivcsw = 0`，而系统时间
   39–45 s/worker、CPU 134.9%，说明线程**没有睡眠、没有切换**，在核内
   空转；同一时间串口大量
   `WARN: Switching to a task already running in the foreground`。
   该日志来自 `ostd/src/task/processor.rs` 的 `before_switching_to()`：
   它在 `next_task.switched_to_cpu` 上 `spin_loop()`，直到另一个 CPU
   完成切换。命中 `ostd/src/task/scheduler/mod.rs:594-599` 的已知
   FIXME（决策与切换不原子，issue #1633）。
5. **flock 把“切换决策”放大成风暴**：`kernel/core/src/fs/file/flock.rs`
   的 `unlock()` 对等待队列调用 `wake_all()`，一次 unlock 唤醒全部
   15 个等待者；这些任务同时被调度，撞上第 4 条的双重选中窗口。
   共享 fd 时同 owner 的锁在 `try_set_lock` 里直接替换，没有等待者，
   所以不触发。

**结论（四选一）：A（调度/唤醒开销）**；C（锁同步争用）是触发条件，
不是根因；B、D 排除。

## 4. 修复方案

这不是 flock 语义错误，而是调度器在唤醒-切换竞争窗口里的自旋。两个
候选，方案 2 已实施并验证：

1. **调度器（根因）**：`pick_next` / `enqueue` 时跳过
   `switched_to_cpu == true` 的任务（说明它已在另一个 CPU 上运行或正在
   被切换过去），把它留在/放回队列，改选下一个或 idle；避免
   `before_switching_to()` 的长自旋。需要给 `Task` 增加只读访问器，
   并覆盖 issue #1633 描述的双重调度窗口；风险中等，需要 ktest 与
   flock/网络压测。
2. **flock（降低触发概率）**：把等待队列从“每个 FlockItem 一个”改为
   “每个 inode 一个”，unlock 时 `wake_one()` 而不是 `wake_all()`，
   并处理被丢弃 item 上的等待者（当前 `Drop` 也会 `wake_all`）。能把
   每次 unlock 的并发切换尝试从 15 降到 1，但单独做不能消除竞态。

**已实施方案 2**（commit `bd904ff64`，内核 `923ca5c5`）：
`kernel/core/src/fs/file/flock.rs` 把等待队列从“每个 FlockItem 一个”
改为 `FlockList` 上一个 per-inode 队列，`unlock()` 只调用 `wake_one()`
（先释放锁表 mutex 再唤醒）。这样被唤醒者要么拿到锁、要么重新入队，不会
丢唤醒；每次 unlock 只产生一个切换决策，不再形成 15 路风暴。

复验（同样的 200 文件、4 进程×4 线程、每线程一个 fd）：

| 变体 | 修复前 | 修复后 |
|---|---|---|
| `perf_bench --mode lock_tfd` 4×4 | 95.15 s（2.1 文件/s） | **0.151 s（1,324 文件/s）** |
| agent `stress_hash.py` 4×4 | 56.38 s（3.5 文件/s） | **0.287 s（697 文件/s）** |
| `lock_tfd` 1 进程×4 线程 | 1.38 s | 0.125 s |
| `lock_tfd` 4 进程×1 线程 | 0.083 s | 0.077 s |
| hash-only 4×4（对照） | 0.103 s | 0.103 s |
| agent 工具全量 `/usr`（21,657 文件 / 990.6 MiB） | 1,232 s 只完成 5,384 条 | **31.66 s 完成 21,657 条（0 错误；与单写者基线逐条一致：matched 21,657 / missing 0 / extra 0 / mismatch 0）** |

串口 `Switching to a task…` 从 62 次降到 2 次，且不再出现秒级停顿。

### 校验基线的并发 append 现象（新发现，未修）

第一次全量校验 `verify_ok=0`：`raw` 的 21,657 条记录全部可解析，但
`xargs -P8 sha256sum --zero >> usr.baseline.z` 生成的基线有 **114 条
记录被拼接**（例：`ca27a636ostnamectl`、`c15fa7d0…e737e_facets.tcc`），
导致 missing/extra/mismatch。Linux 上同样命令（3 轮 × 5,000 文件）
**0 条 malformed**，因此这**疑似 Asterinas 并发 append 写非原子**：
多个进程共享一个 `O_APPEND` fd 时，写入会互相穿插。改为 **单写者**
（`xargs -P1`）后基线可比较；该并发 append 问题已单独留证，留待后续
复现与修复（不在本轮 60 分钟范围内）。
方案 1（调度器 `pick_next` 跳过 `switched_to_cpu` 的任务）仍建议后续做，
以覆盖非 flock 路径；需单独的 ktest 与压测。

## 5. 对 Debian 可用性的影响（如实记录）

- 真实工作负载若“多线程 + 多 owner 文件锁 + 高频记录”会出现灾难性
  退化：该工具 200 个文件 56 s，全量 `/usr` 24,270 文件在 1,232 s 内只
  完成 5,384 条（约 4.4 条/s），`/usr/lib` 600 s 内只完成约 871 条。
- `xargs -P8`、单线程 hash、编译、I/O 都不受影响，所以普通使用感知
  不明显；受影响的是“高争用同步原语”这一类并行程序。
- 修复前可作为规避：每个进程共享一个 flock fd（或按进程分片），或把
  记录写入改成无锁的单写者队列。

## 6. 复现

```sh
# 六项隔离
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/parallel_perf_diag.sh \
make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/debian-x86-toolchain3/rootfs/debian-root.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm X86_DEBIAN_XORG_TIMEOUT=1800 \
  DAILY_USE_WORKSPACE=$PWD/target/perf-diag

# owner 数对照 + flock 微基准
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/parallel_perf_lockfd.sh \
make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/debian-x86-toolchain3/rootfs/debian-root.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm X86_DEBIAN_XORG_TIMEOUT=1500 \
  DAILY_USE_WORKSPACE=$PWD/target/perf-lockfd
```

Linux（宿主）对照：同一 `perf_bench.py`、同一 200 文件样本，
`--mode lock_tfd --procs 4 --threads 4` **0.0168 s（11,870 文件/s）**。

## 7. 证据

- `target/perf-diag/console.txt`（六项测量原始串口）
- `target/perf-lockfd/console.txt`（owner 数对照）
- `target/usr-hash-stress/console.txt`（修复前的全量 /usr 与 /usr/lib 超时、
  `Switching to a task…` 414 次、panic/alloc/OOM 均为 0）
- `target/usr-hash-fixed3/console.txt`（修复内核上的全量校验：31.66 s、
  `verify_ok=1`、matched 21,657）
- `evidence/2026-10-25-parallel-perf/fixcheck-markers.txt`、
  `append-interleave-observation.txt`
- `tools/x86/debian/perf_bench.py`、`parallel_perf_diag.sh`、
  `parallel_perf_lockfd.sh`
- 代码：`ostd/src/task/processor.rs`（`before_switching_to` 自旋）、
  `ostd/src/task/scheduler/mod.rs:594-599`（FIXME #1633）、
  `kernel/core/src/fs/file/flock.rs`（`wake_all`）
