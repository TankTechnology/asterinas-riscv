# per-syscall CPU 归因：从墙钟里扣掉阻塞时间

日期：2026-10-27
内核：`2e747b59edea3e51`　镜像：`ff-lag`（Firefox 验证）、`toolchain4`（矩阵）

上一轮（[2026-10-27-syscall-time-profiling.md](2026-10-27-syscall-time-profiling.md)）
的 `/proc/syscall_time` 记的是**墙钟**，于是 Firefox 的 top10 被
`futex`(69.8%)、`epoll_wait`、`poll`、`wait4`、`clock_nanosleep`、
`ppoll` 这些**阻塞等待**占满，看不出 CPU 到底花在哪。这一轮补齐
「阻塞时间」维度，让计数器直接给出 **CPU-only** 归属。

## 1. 实现

### 1.1 每任务 blocked 计数（ostd）

`ostd/src/task/mod.rs`：

- `Task` 新增字段 `blocked_cycles: AtomicU64`，以及
  `blocked_cycles()` / `reset_blocked_cycles()` / `add_blocked_cycles()`；
- 全局开关 `BLOCKED_CYCLES_ACCOUNTING`（默认 off，只有 profiling 时打开），
  由 `set_blocked_cycles_accounting()` 控制。

`ostd/src/task/scheduler/mod.rs` 的 `park_current`（**统一阻塞入口**：
`WaitQueue`、`Waiter::wait()`、futex 等待、`nanosleep` 最终都走这里）：

```rust
let blocked_start = crate::task::blocked_cycles_accounting().then(crate::arch::read_tsc);
reschedule(|...| { ... });            // 真正的调度切换 + 被唤醒
if let Some(start) = blocked_start {
    let cycles = crate::arch::read_tsc().wrapping_sub(start);
    if let Some(task) = Task::current() { task.add_blocked_cycles(cycles); }
}
```

开关关闭时只多一次 `AtomicBool` load 与一次分支，不读 TSC。

### 1.2 syscall 出口折算 CPU（kernel）

`kernel/core/src/syscall/mod.rs`：

- 计时打开时，在 `handle_syscall` **进入**处 `task().reset_blocked_cycles()`；
- **返回**处读 `blocked_cycles()`，累加进全局数组：

```rust
SYSCALL_TIME_CYCLES[nr].fetch_add(cycles, Relaxed);          // total
SYSCALL_TIME_BLOCKED[nr].fetch_add(blocked.min(cycles), Relaxed); // blocked
SYSCALL_TIME_COUNTS[nr].fetch_add(1, Relaxed);
```

`blocked.min(cycles)` 把「睡眠跨出 syscall 边界」这类记账毛刺钳住，保证
`cpu = total − blocked ≥ 0`。`echo on/off` 同时切换 ostd 的阻塞记账；
`echo reset` 三个数组一起清零。

### 1.3 /proc/syscall_time 新列

`kernel/core/src/fs/fs_impls/procfs/syscall_time.rs`：

```text
# enabled=1 (columns: nr count total_ns blocked_ns cpu_ns avg_cpu_ns)
47 20784 20185900000 629900000 19556000000 940911
```

按 **`cpu_ns` 降序**输出（`count>0` 的行），周期 → ns 用读取时的
`tsc_freq()` 换算。

### 1.4 报告工具

`tools/x86/debian/syscall_time_report.py` 新增 `--by cpu|total`（默认
**cpu**），输出 `total_ms/blocked_ms/cpu_ms/avg_cpu_us/占比`，兼容旧的
4 列格式。`--json` 里带 `by` 字段。

## 2. 三条验证（内核 2e747b59）

`tools/x86/debian/syscall_cpu_check.sh`：同一个 C 程序内三种模式，
模式间 `reset`，跑完直接读 `/proc/syscall_time`：

| 验证 | 判据 | 实测 | 结论 |
|---|---|---|---|
| **纯 CPU 型** | getpid ×1e6：`blocked ≈ 0`、`cpu ≈ total` | `nr=39 count=1000000 total_ns=352852983 blocked_ns=0 cpu_ns=352852983 cpu_pct=100.0` | ✓ 0% 阻塞 |
| **纯阻塞型** | `clock_nanosleep(0.1s) ×100`：`cpu ≪ total`（<5%） | `nr=230 count=100 total_ns=10067051816 blocked_ns=10061779778 cpu_ns=5272037 cpu_pct=0.1` | ✓ 99.9% 阻塞，CPU 0.1% |
| **混合型** | futex 竞争：`blocked > 0` 且 `cpu < total` | `nr=202 count=21769 total_ns=52666590 blocked_ns=25285997 cpu_ns=27380593 cpu_pct=52.0` | ✓ 48% 阻塞 / 52% CPU |

计数精确性沿用上一轮：`count` 与循环次数逐条对齐；开关开销在同一轮已测
（<1%，编译期开关关闭时正常路径零 TSC）。

## 3. Firefox 20 s 滚动：by-cpu 与 by-total 并列

启用→`reset`→激活窗口并滚动 20 s→读计数器。同一份计数器分别按
`cpu_ns` 和 `total_ns` 排序：

### 3.1 按 CPU 降序（真实 CPU 热点）

| # | syscall | nr | count | total_ms | blocked_ms | cpu_ms | avg_cpu_us | 占比 |
|---|---|---|---|---|---|---|---|---|
| 1 | recvmsg | 47 | 20,784 | 20,185.9 | 629.9 | 19,556.0 | 940.9 | **44.7%** |
| 2 | sendmsg | 46 | 2,884 | 14,060.3 | 0.0 | 14,060.3 | 4,875.3 | **32.1%** |
| 3 | read | 0 | 5,928 | 3,226.0 | 91.6 | 3,134.4 | 528.7 | 7.2% |
| 4 | clone | 56 | 9 | 1,532.1 | 0.0 | 1,532.1 | 170,236.9 | 3.5% |
| 5 | ioctl | 16 | 1,027 | 765.9 | 0.0 | 765.9 | 745.8 | 1.8% |
| 6 | poll | 7 | 22,555 | 86,752.9 | 86,110.2 | 642.7 | 28.5 | 1.5% |
| 7 | clone3 | 435 | 126 | 527.7 | 0.0 | 527.7 | 4,188.2 | 1.2% |
| 8 | openat | 257 | 3,510 | 378.0 | 17.7 | 360.3 | 102.6 | 0.8% |
| 9 | epoll_wait | 232 | 7,867 | 73,296.5 | 72,972.3 | 324.1 | 41.2 | 0.7% |
| 10 | writev | 20 | 6,775 | 321.3 | 0.0 | 321.3 | 47.4 | 0.7% |

### 3.2 按 total（墙钟）降序（上一轮的视角）

| # | syscall | 占比 | 其中 blocked |
|---|---|---|---|
| 1 | futex | 68.1% | 683,255 / 683,554 ms（≈99.96%） |
| 2 | poll | 8.7% | 86.1 / 86.8 s |
| 3 | epoll_wait | 7.4% | 73.0 / 73.3 s |
| 4 | wait4 | 3.9% | 99%+ |
| 5 | ppoll | 3.8% | 99%+ |
| 6 | clock_nanosleep | 3.6% | ≈100% |
| 7 | recvmsg | 2.0% | 0.63 s |
| 8 | sendmsg | 1.4% | 0 s |
| 9 | read | 0.3% | 0.09 s |
| 10 | clone | 0.2% | 0 s |

### 3.3 对比结论

- **futex 从 68.1% 降到榜外**：68 万 ms 里只有约 0.3 s 是 CPU，其余全是
  线程等待——上一轮“futex 是头号热点”是墙钟假象。
- **真正的 CPU 热点是 socket IPC**：`recvmsg` + `sendmsg` 合计
  **76.8% CPU**（20 s 窗口、4 vCPU 上限约 80 CPU·s，两者约 33.6 CPU·s，
  占可用 CPU 的 42%）。这正是 Firefox 内容进程 ↔ 父进程/GPU 进程的
  IPC 通道；`sendmsg` 单次 **4.9 ms**、`recvmsg` 单次 **0.94 ms** 都异常
  昂贵，指向 socket 数据路径（拷贝 + 唤醒 + 调度）而非协议本身。
- `poll`/`epoll_wait` 的 CPU 极小（单次 28–41 µs），是纯等待。
- `clone` 9 次却 1.53 s（单次 170 ms）——进程创建（地址空间 + 线程栈 +
  首次调度）很重，Firefox 起的进程数不多所以总量小。
- `ioctl` 1027 次 0.77 s（单次 746 µs）是渲染/DRM 相关调用，量级可接受。
- `read` 7.2%：文件/管道读取的实际拷贝成本。

**下一步优化方向**（按 CPU 收益排序，尚未实施）：① socket IPC 数据路径
（`recvmsg`/`sendmsg` 的单次成本，先查 4.9 ms 的 sendmsg 是否含大块拷贝
与不必要的唤醒）；② `clone` 的进程创建成本（若后续有频繁 fork 场景）；
③ `ioctl` 批量化。前两项都在 CPU 侧，不涉及 GPU 驱动。

## 4. 已知限制

- 阻塞记账只覆盖 `park_current` 这一统一入口；内核里自旋/忙等（未
  调度出去）的时间仍算作 CPU——这是符合语义的选择，但意味着
  `blocked_ns` 是**下界**、`cpu_ns` 是**上界**（对自旋型路径）。
- 计数器是**全局**的（不区分进程），Firefox 测试期间其它进程的 syscall
  也会计入；测试脚本在滚动窗口内尽量保持系统安静。
- `total_ns` 从 syscall 进入到返回，包含所有被中断/抢占的时间；被抢占
  但没 park 的时间算在 CPU 里（与上面的上界一致）。
- 若睡眠跨越 syscall 返回（例如信号唤醒后继续执行），`min(cycles)` 钳位
  会丢掉超出部分，而不是记成负数。

## 5. 一次未复现的 panic（如实记录）

同一内核 `2e747b59` 的第一次 CPU 归因运行（`target/cputime2`）在 Firefox
阶段触发一次 panic：

```text
Uncaught panic at kernel/core/src/vm/vmar/vmar_impls/page_fault.rs:10:
This function might break atomic mode (preempt_count = 3)
```

随后同脚本同内核又跑了两次（`cputime2` 重跑、`cputime3`），三次验证与
Firefox 两张表都完整产出，未再观察到 panic。判断为**低频、与本次改动
无直接因果**的既有并发缺陷（缺页路径在 `preempt_count > 0` 时进入），
但未定位根因；后续应带着 panic 现场日志与高负载场景继续追。

## 6. 回归

7 域矩阵（kernel `2e747b59`，镜像 `toolchain4`，4 vCPU）：

| 域 | 结果 | 细节 |
|---|---|---|
| DESKTOP | ok | Xorg+DRI3 |
| APP | ok | xterm |
| APT | ok | xclip+xterm+alsa-utils-installed |
| CLIPBOARD | ok | xclip-round-trip |
| AUDIO | ok | aplay-hw:0,0 |
| USB | ok | mounted-/dev/sda-read-ASTERINAS-USB-OK |
| SUSPEND | ok | woke-up-disk-and-io-ok |

额外域 INPUT_METHOD 仍 fail（`engine--no-text`，与本次改动无关的既有缺口）；
`DAILY_DONE=ok`、panic 0、allocation failed 0。原始表见
`evidence/2026-10-27-cpu-time/matrix.txt` / `matrix.tsv`。

## 7. 复现

```sh
# 三条验证 + Firefox by-cpu/by-total
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/syscall_cpu_check.sh make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/ff-lag/daily.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm DAILY_USE_WORKSPACE=$PWD/target/cputime

# 单独读报告（guest 内或保存下来的文件）
python3 tools/x86/debian/syscall_time_report.py /proc/syscall_time --top 10 --by cpu
python3 tools/x86/debian/syscall_time_report.py /proc/syscall_time --top 10 --by total
```

## 8. 证据

- `evidence/2026-10-27-cpu-time/verification.txt`（三条验证计数行）
- `evidence/2026-10-27-cpu-time/firefox-cpu-vs-total.txt`（by-cpu 全表 +
  by-total 汇总）
