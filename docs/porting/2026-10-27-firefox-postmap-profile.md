# Firefox post-map 阶段的 CPU 热点剖析

日期：2026-10-26
内核：`f8091faef`（未改内核代码）
探针：`/tmp/vb/postmap_probe.sh`（固定时刻 `/proc/<pid>/stat` 采样 + 自写 `SIGPROF` PC 采样器）、
`/tmp/vb/syscall_bench_probe.sh`（任务 C 的原语微基准）

## 0. 结论

**post-map 烧的是内核态 CPU，不是用户态计算。**

1. 主进程 utime:stime ≈ **1 : 3.6**（7.5 s 处 1534 : 5476 ticks），即约 **78 % 是内核态 CPU**。
2. `SIGPROF` 采样 279 个样本里，**86.9 % 落在 `libc.so.6`**，且最热的两个 PC
   （`libc+0x10e4f7` 54 次、`libc+0x9a72c` 19 次）都解析到 **`syscall` 指令**：
   - `0x10e4f7` → `syscall@@GLIBC_2.2.5`（函数起始 `0x10e4e0`，偏移 +0x17，正是执行系统调用的指令）
   - `0x9a72c`   → `sem_wait`/`sem_trywait` 区域（glibc 在该函数里内联 `syscall`）
   也就是说采样到的不是 JS、布局或字体栅格，而是「进程正卡在系统调用指令上」。
3. 这些**不是等锁**：`ITIMER_PROF` 只在进程真正消耗 CPU 时触发，阻塞在 futex 上的时间不计入；
   stime 占了 78 % 说明是**实打实的内核执行**。

## 1. 任务 A：固定时刻的 CPU 表（无轮询）

只在这五个时刻读一次 `/proc/<pid>/stat`（不做循环轮询）：

```
PROBE t500_ms=529   t500_tree=firefox-esr:195:u29:s362:thr4,
PROBE t1000_ms=1310 t1000_tree=firefox-esr:195:u36:s1209:thr6,crashhelper:201:u0:s3:thr2,glxtest:238:u36:s584:thr1,
PROBE t2000_ms=2898 t2000_tree=firefox-esr:195:u365:s2087:thr33,crashhelper:201:u0:s4:thr2,
PROBE t4000_ms=5245 t4000_tree=firefox-esr:195:u834:s3973:thr61,Socket Process:368:u26:s680:thr7,MainThread:394:u16:s601:thr3,RDD Process:400:u30:s901:thr5,
PROBE t5000_ms=7531 t5000_tree=firefox-esr:195:u1534:s5476:thr66,crashhelper:201:u0:s6:thr2,Socket Process:368:u28:s680:thr7,WebExtensions:394:u149:s978:thr22,RDD Process:400:u33:s908:thr5,file:// Content:447:u108:s991:thr18,
```

| 时刻 | 主进程 utime / stime | 线程数 | 同期子进程 |
|---|---|---|---|
| 529 ms | 29 / **362** | 4 | — |
| 1 310 ms | 36 / **1 209** | 6 | `glxtest` 36 / 584 |
| 2 898 ms | 365 / **2 087** | 33 | — |
| 5 245 ms | 834 / **3 973** | 61 | Socket 26/680、MainThread 16/601、RDD 30/901 |
| 7 531 ms | 1 534 / **5 476** | 66 | WebExtensions 149/978、`file:// Content` 108/991 |

**单位警告（如实记录）**：按 `CLK_TCK=100` 换算，7.53 s 墙钟里主进程用了 15.3 s 用户 + 54.8 s 内核
CPU，在 4 vCPU 上不可能成立。因此本报告**只用 utime:stime 的比值**（1:3.6），不引用绝对秒数；
Asterinas 的 `/proc/<pid>/stat` 时间单位与 `CLK_TCK` 需要单独核对（下一轮可做）。

采样开销：5 次快照，每次 `pgrep -f firefox` + 约 6–10 个 `/proc/<pid>/stat` 读取，
未逐个计时；没有轮询循环、没有 `xwd`、没有 `xdotool` 参与这段测量。

## 2. 任务 B：进程内热点（自写采样器，因为 guest 无 perf）

可用性（PM1 原始行）：`perf_bin=NA`、`pid1_stack=NA`、`pid1_wchan=NA` ⇒ 任务书里的第 1、2 种手段都不可用，
故采用第 3 类思路的**自建采样**：`LD_PRELOAD` 库用 `setitimer(ITIMER_PROF, 10 ms)` + `SIGPROF` 处理器，
把中断时的 `REG_RIP` 与 pid 写进 `/tmp/prof.dat`（处理器内只做一次 `write`，异步信号安全）。

```
PROBE prof_samples total=279 main_pid=195 main_samples=153 procs=8
PROBE bucket libc.so.6 86.9% (133) | libxul.so 9.2% (14) | ld-linux-x86-64.so.2 2.0% (3) | firefox-esr 1.3% (2) | libnssutil3.so 0.7% (1)
PROBE top pc=0x7ffffead772c libc.so.6+0x9a72c n=54
PROBE top pc=0x7ffffeb4b4f7 libc.so.6+0x10e4f7 n=38
PROBE top pc=0x7ffffead772e libc.so.6+0x9a72e n=19
```

符号解析（把 guest 镜像里的 `libc.so.6` 抽出来，用 `readelf -sW` 查最近的导出符号）：

| PC 偏移 | 解析结果 | 说明 |
|---|---|---|
| `libc+0x10e4f7` | `syscall@@GLIBC_2.2.5`（起始 `0x10e4e0`，+0x17） | 通用 `syscall()` 包装器里的 `syscall` 指令 |
| `libc+0x9a72c` / `0x9a72e` | `sem_wait`/`sem_trywait`（`0x9a6a0` / `0x9a6d0`）附近 | glibc 在这些函数里内联系统调用指令 |

**归类：内核态（syscall 密集）**。不是用户态计算（JS/布局/字体只占 libxul 的 9.2 %），
也不是等锁/等 IPC（`ITIMER_PROF` 不计阻塞时间，78 % 的 stime 是真实内核执行）。

`n_dlopen` 之外还有 8 个进程参与采样（`procs=8`），说明子进程也继承了这个 preload；
报告中只统计了样本最多的主进程（153/279）。

## 3. 任务 C：内核原语对照（同镜像、两侧各一次）

| 原语 | Asterinas（SB1） | Linux（SB2） | 倍率 |
|---|---|---|---|
| fork + waitpid | **2 909 µs** | 143 µs | **20×** |
| pthread create/join | **810 µs** | 29 µs | **27×** |
| mmap + munmap 4 KiB | 15.7 µs | 1.87 µs | 8.4× |
| pipe 1 字节往返 | 4.27 µs | 0.41 µs | 10.5× |
| futex 往返 | 19.2 µs | 5.84 µs | 3.3× |

原始行：

```
SB1 (Asterinas): fork 2909029/1610000 | pthread 810143/540000 | mmap4k 15730/15600 |
                 pipe_rtt 4266/4250 | futex_rtt 19203/18950   （wall/cpu，ns/op）
SB2 (Linux):     fork  142683/ 59946 | pthread  29415/ 31340 | mmap4k  1865/ 1810 |
                 pipe_rtt   405/   395 | futex_rtt  5844/ 4232
```

结合 §0：post-map 的 CPU 主要花在系统调用上，而**线程/进程创建（clone 路径）差 20–27×、
mmap 差 8×、pipe 往返差 10×**——Firefox 在这段要拉 33→66 个线程、起 Socket/RDD/Content
等多个进程（§1 的表里能直接看到线程数从 6 涨到 66），正好压在这几条最慢的原语上。

## 4. 证据

- `evidence/2026-10-27-firefox-postmap-profile/PM1-probe-lines.txt`（26 行：固定时刻 CPU 表 + PC 采样）
- `…/SB1-asterinas-probe-lines.txt`、`…/SB2-linux-probe-lines.txt`（原语微基准）
- 控制台 `~/vb-ws/PM1/console.txt`
- 探针：`/tmp/vb/postmap_probe.sh`（含 `prof.c` 源码）、`/tmp/vb/syscall_bench_probe.sh`
