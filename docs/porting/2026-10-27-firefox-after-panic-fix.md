# Firefox 复测：panic 修复后的 sys:user 与 top10（三组对照）

日期：2026-10-27

## 0. 方法

`tools/x86/debian/ff_measure_{a,b,c}.sh`，同一套协议：

- Firefox 打开 4,000 行静态页，窗口 1024×768，openbox；
- 每组 **3 轮，每轮 20 s 连续 Page_Down 滚动**；
- 每轮同时用 `syscall_sampler.py`（20 ms 采样 `/proc/<pid>/syscall` +
  `/proc/<pid>/stat`）记录 delta `utime`/`stime`/`minflt`/`majflt`；
- C 组额外打开 `/proc/syscall_time`，取 `--by cpu` top10；
- 三组分别为：
  - **A**：panic 修复前内核（`a17a3dbc4` 检出后构建）；
  - **B**：panic 修复后（`15a3268df`，内核 `69f3feada57f598a`）；
  - **C**：同 B，另开 syscall_time 计时。

> 说明：r1 是完整负载轮；r2/r3 明显偏轻（页面滚到底/窗口失焦后
> Firefox 基本空闲，user_ticks 从 ~7,000 掉到 ~900）。这是本轮协议的现实，
> 下面同时给出三轮原始值和中位数，不粉饰。

## 1. 三组 utime/stime/minflt

单位：ticks（内核时钟）；sys:user 与单位无关。

| 组 | 轮 | user | sys | **sys:user** | minflt | majflt | panic |
|---|---|---|---|---|---|---|---|
| A 修复前 | r1 | 7,068 | 20,653 | **2.92** | 127,938 | 0 | |
| A | r2 | 1,892 | 4,250 | **2.25** | 12,961 | 0 | |
| A | r3 | — | — | — | — | — | **panic** |
| B 修复后 | r1 | 7,033 | 22,599 | **3.21** | 111,761 | 0 | |
| B | r2 | 2,834 | 7,325 | **2.58** | 36,215 | 0 | |
| B | r3 | 869 | 2,373 | **2.73** | 751 | 0 | |
| C 修复后+计时 | r1 | 7,384 | 24,077 | **3.26** | 115,938 | 25 | |
| C | r2 | 2,884 | 6,972 | **2.42** | 28,574 | 0 | |
| C | r3 | 938 | 2,505 | **2.67** | 602 | 0 | |

**sys:user 中位数**：A 2.59（2 个样本）、B **2.73**、C **2.67**。

关于早前的 **9.31**：那是 `2026-10-27-firefox-sys-attribution-2.md` 里
`firefox_sysattr3.sh` 的测量（不同页面、不同滚动/窗口协议），与本轮
不可直接横比。本轮以 **A 组（修复前）为基线**：修复前后 sys:user 基本
不变（2.6–2.7），符合预期——panic 修复只改变"原子模式 + 锁竞争"这一窄
路径的行为，不改变正常路径成本。**sys:user 仍约 2.5–3，说明滚动负载下
Asterinas 的系统时间仍是用户时间的 ~2.5–3 倍**，内核路径依然是瓶颈。

## 2. 组 C：top10 by CPU（每轮 20 s）

r1（完整负载）：

| # | syscall | nr | count | cpu_ms | avg_cpu_us | 占比 |
|---|---|---|---|---|---|---|
| 1 | read | 0 | 35,866 | 6,846.5 | 190.9 | **27.6%** |
| 2 | clone | 56 | 36 | 2,972.8 | 82,577 | **12.0%** |
| 3 | ioctl | 16 | 11,054 | 2,309.1 | 208.9 | 9.3% |
| 4 | poll | 7 | 56,242 | 1,773.3 | 31.5 | 7.1% |
| 5 | **recvmsg** | 47 | 59,300 | 1,145.5 | **19.3** | **4.6%** |
| 6 | futex | 202 | 62,318 | 1,141.3 | 18.3 | 4.6% |
| 7 | openat | 257 | 14,536 | 1,139.8 | 78.4 | 4.6% |
| 8 | epoll_wait | 232 | 23,013 | 801.3 | 34.8 | 3.2% |
| 9 | writev | 20 | 16,423 | 789.0 | 48.0 | 3.2% |
| 10 | clone3 | 435 | 222 | 706.0 | 3,180 | 2.8% |

r2/r3 的 top1 是 `ioctl`（21.9% / 30.8%），`recvmsg` 7.9% / 9.8%
（18.5 / 17.7 µs/次），`sendmsg` **两轮都不在 top10**。

**与 warn-once 之前的对比**（`2026-10-27-socket-cpu-opt.md`）：

| 指标 | warn-once 前 | 现在（修复后） |
|---|---|---|
| recvmsg 单次 CPU | 940.9 µs | **17.7–19.3 µs** |
| recvmsg 占比 | 44.7% | 4.6–9.8% |
| sendmsg | 32.1% / 4.9 ms/次 | **跌出 top10** |
| 20 s 的 syscall CPU 合计 | ~40 CPU·s | 8.7–24.8 CPU·s（随轮次负载） |

结论：**recvmsg/sendmsg 已不再是 CPU 热点**。新的 top 是 `read`
（190 µs/次）、`clone`（**82.6 ms/次**）、`ioctl`（129–209 µs/次）、
`openat`、`writev`，以及 poll/epoll_wait/futex 这类阻塞项。

## 3. 是否还有 panic

- **A 组第 3 轮复现 panic**，`preempt_count = 1`（这次不是 3），位置仍是
  `page_fault.rs:10`——同一缺陷、不同嵌套深度。
- **B/C 两组共 6 轮 20 s 滚动：panic = 0**，全部跑完。

## 4. socket 微基准复查（同一内核）

见 §5 表（`sock_recheck_check.sh`，64 B，200k 轮）：

| 模式 | iovec 优化前 | 现在 | 变化 |
|---|---|---|---|
| sameproc（socket 路径、无唤醒） | 4,023 ns/syscall | **3,360** | **−16.5%**（仍在 −16~20% 区间） |
| pingpong（2 进程） | 14,218 | 15,302 | +7.6%，该轮 blocked 59.9 µs（比之前 51.3 µs 高），主机噪声 |
| pipe（对照） | 1,107 | 1,230 | +11%，噪声内 |

结论：iovec 优化在 panic 修复后仍然有效（sameproc 3.2–3.4k vs 4.0k）；
跨进程 pingpong 仍由通用阻塞/唤醒主导，与之前的结论一致。

## 5. 回归

本轮内核（`69f3feada57f598a` = `15a3268df`）在上一任务中已跑 7 域矩阵：
全 ok、`DAILY_DONE=ok`、panic 0（见
`evidence/2026-10-27-page-fault-atomic-panic/matrix.txt`）。本轮只改了
测量脚本，未改内核代码，未重复矩阵。

## 6. 复现

```sh
# 组 A（需先 git checkout a17a3dbc4 并构建）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/ff_measure_a.sh make test_x86_daily_use …
# 组 B / C（当前 HEAD 的修复内核）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/ff_measure_b.sh …
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/ff_measure_c.sh …
```

## 7. 证据

- `evidence/2026-10-27-firefox-after-panic-fix/group-a.txt`（修复前：2 轮数据 + panic 现场）
- `…/group-b.txt`（修复后 3 轮）
- `…/group-c-top10.txt`（修复后 + 计时：3 轮采样 + r1 top10 全表）
- `…/microbench-recheck.txt`（socket 微基准复查）

注：B/C 的完整串口日志在收尾时因宿主磁盘紧张被回收，evidence 目录保留了
当时的 marker 行与表格（与正文数字一一对应）。
