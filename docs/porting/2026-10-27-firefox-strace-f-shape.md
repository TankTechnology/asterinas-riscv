# Firefox 的多线程 syscall 形态（首次 `strace -f` 观测）

> ⚠️ **硬约束（先读这一段）**：`strace -f` 的开销是**数量级的**（每个 syscall 若干 µs 起，
> 且 `-f` 会对每个线程做停/续）。本轮**只看形态（谁调什么、调用比例）**，**绝不看绝对时间**，
> 也**不与任何历史性能数字比较**。下文所有输出都**带 strace 开销，不可用于性能结论**；
> **不能据此推断耗时**。

日期：2026-10-27
内核基线：`1936d59c9`（`PTRACE_O_TRACECLONE` 之后，`strace -f` 首次可用于多线程程序）
任务书：`/tmp/tp/STRACE_F_FIREFOX.md`

## 0. 结论（只谈形态）

- **`strace -f` 能跟住 Firefox 的多线程形态**：所有 `clone`/`clone3` 都返回正的 tid，
  **没有一次 `EOPNOTSUPP`**，子 tid 及其 `exit` 都出现在 trace 里（§2.3、§3.1）。
- **线程创建的形态**：`pthread_create` 走 **`clone3`**；进程创建走
  **`clone(SIGCHLD)`**（普通 fork）与 **`clone3(CLONE_VFORK|SIGCHLD)`**（posix_spawn 式，
  子进程随即 `execve`）。
- **8 s 窗口只覆盖启动早期的一小段**：2406 次 syscall、10–11 个 tid、10–13 次 clone/clone3；
  20 s 窗口涨到 6245 次 syscall、20 个 tid、19–29 次 clone/clone3（同一窗口的两次采样有波动，
  只报区间；数字随窗口增大而增长）。因此**这些 tid 数不是 Firefox 的线程总数**。
- **无 strace 的 `/proc` 对照**才是 Firefox 的线程规模：单个 `firefox-esr` 进程
  第 4 s **52** 线程、第 7 s **69**、第 10 s **70**、第 15 s **77**。
  strace 窗口里的 10–20 个 tid 说明的是「被拖慢的启动只走到了这么远」，
  不是「Firefox 只有 10–20 个线程」。
- **「每线程都做一遍」的系统调用**：`set_robust_list` **每个 tid 至少 1 次**
  （抽样 10 个 tid 共 11 次，主 tid 多 1 次；`-c` 汇总 14 次）；新线程栈 `mmap`
  **多数新线程 1 次**（个别线程 31 次），而库映射的 `mmap` 绝大部分集中在启动主线程
  （一个 tid 就 382 次）。
  `rseq` **不是每线程都做**：只有 3 个 tid 尝试，且全部 `-1 ENOSYS`（内核未实现），
  之后 glibc 不再尝试。
- **短命线程确实存在**：8 s 窗口内可算寿命的 5 个新线程中，2 个在 0.5 s 内退出
  （0.128 s、0.248 s），最长 1.557 s —— 但这是**被 strace 拖慢后的窗口内寿命**，
  **不能据此推断耗时**，也不能外推真实线程寿命。

## 1. 方法

- 门禁：`make test_x86_daily_use` + 自定义 checklist（不改内核、不跑 7 域矩阵）。
- 基座镜像 `target/ff-lag/daily.ext2`（自带 `firefox-esr` 140.15.0esr）；
  guest 内 `apt-get install -y strace` 得到 **strace 6.13**。
- Xorg 按门禁方式启动（`MESA_LOADER_DRIVER_OVERRIDE=kms_swrast`、`GALLIUM_DRIVER=softpipe`、
  `-config /etc/X11/xorg-noudev.conf`），`DISPLAY=:0`、`HOME=/tmp`，
  每次采样使用独立的 `--profile` 目录。
- 采样命令（`timeout` 放在 **strace 内部**，让 tracee 自行退出、strace 正常收尾写出 `-c` 表）：

  ```
  strace -f -c -o file timeout 8  firefox-esr --profile P about:blank      # 全局汇总
  strace -f -e trace=clone,clone3 -o file timeout 8  ...                    # 线程创建规模
  strace -f -e trace=rseq,set_robust_list,mmap,munmap -o file timeout 8 ... # 每线程调用
  strace -f -tt -e trace=clone,clone3,exit,exit_group -o file timeout 8 ... # 线程寿命
  同上四类各再来一份 20 s 版本（只保留 -c 与 clone 计数）
  ```
- 对照（**不挂 strace**）：正常启动 Firefox，在第 4/7/10/15 s 读
  `/proc/<pid>/status` 的 `Threads:` 并对所有 `firefox-esr` 进程求和。

## 2. 任务 A：数据

### 2.1 全局 `-c` 汇总

> `-c` 表里的 `% time` / `seconds` / `usecs/call` 三列是 strace 自身在 tracee 停止期间测的，
> **不能当耗时**；下表只取 `calls` / `errors` 两列。

8 s 窗口：**总调用 2406 次，其中 370 次返回错误**（78 行汇总，见 §5 原文）。

按调用次数 top10（8 s）：

| # | syscall | calls | errors |
|---|---|---|---|
| 1 | mmap | 465 | 0 |
| 2 | recvmsg | 291 | 221 |
| 3 | openat | 200 | 40 |
| 4 | close | 188 | 0 |
| 5 | futex | 157 | 12 |
| 6 | poll | 141 | 0 |
| 7 | fstat | 141 | 0 |
| 8 | read | 132 | 3 |
| 9 | mprotect | 99 | 0 |
| 10 | writev | 70 | 0 |

20 s 窗口：**总调用 6245 次，799 次错误**。同口径 top10（换成 20 s 窗口后
`read`/`openat`/`access` 上升，`recvmsg` 仍以 309 次错误居错误数之首）：

| # | syscall | calls | errors |
|---|---|---|---|
| 1 | mmap | 916 | 0 |
| 2 | openat | 611 | 197 |
| 3 | futex | 543 | 42 |
| 4 | read | 534 | 1 |
| 5 | close | 450 | 0 |
| 6 | recvmsg | 438 | 309 |
| 7 | fstat | 304 | 0 |
| 8 | poll | 266 | 8 |
| 9 | access | 233 | 42 |
| 10 | mprotect | 220 | 0 |

两个窗口的共同点（这是形态，不是时间）：**内存映射、文件探测（openat/access/readlink）、
close、futex/poll 是启动期的主要调用族**；`recvmsg` 是错误数最多的调用
（20 s 内 438 次中 309 次错误，符合非阻塞 socket 在启动期的轮询探测形态）。

### 2.2 出现过多少个线程（tid）

| 观测 | 8 s | 20 s | 无 strace 对照（`/proc` Threads 求和） |
|---|---|---|---|
| 出现在 trace 里的不同 tid | 10（`-e trace=rseq,...`）/ 11（`-e trace=clone,clone3`） | 20 | 4 s: **52**；7 s: 69；10 s: 70；15 s: **77** |
| 进程数 | 1 个主进程 + 少量子进程 | 同 | 1 个 `firefox-esr` 进程 |

结论：strace 侧只能给出「窗口内出现过的 tid」，随窗口增大而增长（10 → 20），
**不能当作线程总数**；Firefox 的实际线程规模以 `/proc` 对照为准（≈52–77）。

### 2.3 `clone`/`clone3` 次数与返回值

| 窗口/采样 | clone | clone3 | 成功返回 | 失败 | 作为调用者的不同 tid |
|---|---|---|---|---|---|
| 8 s，`-c` 汇总 | 4 | 9 | 13 | 0 | — |
| 8 s，`-e trace=clone,clone3` | 4 | 6 | 10 | 0 | 11 |
| 20 s，`-c` 汇总 | 5 | 24 | 29 | 0 | — |
| 20 s，`-e trace=clone,clone3` | 5 | 14 | 19 | 0 | 20 |

同一 8 s 窗口的两次采样分别是 13 次与 10 次 —— 在 strace 拖慢下启动进度本身有波动，
**只能报区间/量级，不能报精确值**。

原始行（console 采集时按 210 字符截断，`…` 处为截断点；完整的 `= <tid>` 返回值在
不带 `-tt` 的计数里核对：成功次数与克隆出的不同 tid 数一致，失败 0 次）：

```
445   clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID|SIGCHLD, child_tidptr=0x7ffffedc4a10) = 446
446   clone3({flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM|CLONE_SETTLS|CLONE_PARENT_SETTID|CLONE_CHILD_CLEARTID, child_tid=0x7ffffe4ff990, parent_tid=0x7ffffe4ff990, exit_signal…
446   clone3({flags=CLONE_VM|CLONE_VFORK|CLONE_CLEAR_SIGHAND, exit_signal=SIGCHLD, stack=0x7ffff14d3000, stack_size=0x9000}, 88) = 449
449   clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID|SIGCHLD, child_tidptr=0x7ffffea3fad0) = 450
```

## 3. 任务 B：四个问题

| 问题 | 数据 | 回答 |
|---|---|---|
| **Firefox 启动涉及多少线程/syscall** | 8 s：2406 syscall / 10–11 tid；20 s：6245 syscall / 20 tid；`/proc`：52/69/70/77 线程 @4/7/10/15 s | syscall 总量**受窗口限制**，只能说明「8 s 的 strace 窗口内 2406 次」；线程规模以 `/proc` 为准（50–80 量级）。**不能据此推断耗时** |
| **哪类 syscall 调用次数最多** | §2.1 两张 top10 表 | `mmap`、`openat`/`access`/`readlink`、`close`、`futex`/`poll`、`read` 为主；错误数最多的是 `recvmsg` |
| **是否存在「每线程都做一遍」的系统调用** | 8 s 抽样 10 个 tid | `set_robust_list`：**每个 tid 至少 1 次**（10 个 tid 共 11 次，主 tid 额外 1 次；`-c` 汇总 14 次）；线程栈 `mmap`：**多数新线程 1 次**，个别 31 次；主 tid 382 次是库映射，不代表每线程；`rseq`：**只有 3 个 tid 尝试且全 ENOSYS**，不是每线程都做 |
| **多少线程短暂存在** | 8 s `-tt` 采样：9 次成功 clone、6 次 exit 事件、5 个可算寿命 | <100 ms：0；<500 ms：**2**（0.128 s、0.248 s）；其余 0.603 s / 1.334 s / 1.557 s。**寿命是 strace 拖慢后的窗口内值，不能外推** |

### 3.1 两个 tid 抽样（原文，已删其余）

```
s1 485   mmap(NULL, 8192, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0) = 0x7ffffefc0000
s1 485   mmap(NULL, 2047568, PROT_READ, MAP_PRIVATE|MAP_DENYWRITE, 3, 0) = 0x7ffffedc7000
s1 485   mmap(0x7ffffef52000, 352256, PROT_READ, MAP_PRIVATE|MAP_FIXED|MAP_DENYWRITE, 3, 0x18b000) = 0x7ffffef52000
s2 487   set_robust_list(0x7ffffe4ff9a0, 24) = 0
s2 487   mmap(NULL, 16384, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0) = 0x7ffffefbc000
s2 487   munmap(0x7ffffefbc000, 16384)     = 0
s2 487   +++ exited with 0 +++
```

`s1` 是启动期主线程（大量库映射），`s2` 是一个短命线程：`set_robust_list` → 一次
16 KiB 栈 mmap → 立即 `munmap` → `exit`。

## 4. 边界与观察（未改内核）

- **未跑 7 域矩阵**：本轮不改内核代码。
- **窗口只覆盖早期启动**：8 s 2406 次、20 s 6245 次 syscall，而 Firefox 完整启动的
  syscall 量远大于此（本轮未测，不下结论）。所有比例只代表**被观测窗口内**的形态。
- **观察（未归因）**：若把 `timeout` 放在 strace **外面**、由 `timeout` 给 strace 发
  `SIGINT`，strace 会以 `strace: wait4(__WALL): No child processes` 退出并且**不写** `-c`
  统计表；把 `timeout` 作为 tracee 后一切正常。Linux 上 Ctrl-C 能打印 `-c` 表，
  此处行为不同。可能与我们的 `wait4(__WALL)` / detach 路径有关，**本轮只记录，不声称原因**。
- **`readahead` 全部报错**：8 s 与 20 s 都是 11 次调用 11 次错误（未查 errno）。
- **`rseq` 未实现**：3 次调用全部 `-1 ENOSYS`，glibc 失败后不再尝试；这解释了
  「为什么不是每个线程都调 `rseq`」。
- 本轮**不**从任何数字推断耗时，也不与历史性能结论（例如「Firefox 8 秒里 CPU 1.9%」）比较。

## 5. 证据

- `evidence/2026-10-27-strace-f/strace-c-summary.txt`：8 s `-c` 汇总原文（78 行）
- `evidence/2026-10-27-strace-f/strace-c-20s.txt`：20 s `-c` 汇总原文（94 行）
- `evidence/2026-10-27-strace-f/clone-lines.txt`：`clone`/`clone3` 原始行
- `evidence/2026-10-27-strace-f/tid-samples.txt`：2 个 tid 的抽样（其余 trace 已删）
- 主机工作区：`target/ff-strace/console.txt`（8 s 四组采样 + `/proc` 对照）、
  `target/ff-strace20/console.txt`（20 s 采样 + `/proc` 对照）
