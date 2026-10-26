# 干净的系统调用账：clone 24.6 ms、poll/futex 掉出榜外——旧账整体作废

日期：2026-10-26
内核基线：`a966812e2`
测量方法：**只用 `/proc/syscall_time` 前后快照**；启动 Firefox 后固定 `sleep 8` 再读；
每轮开始前 `reset`，所以「after 快照」就是该轮账；Firefox 进程 `stat` 每轮**只读一次**。
**没有** xwd 轮询、`/proc/<pid>/stat` 循环、ps、top、strace。

## 0. 结论

1. **旧账不可信，已整体替换**：clone 850 ms → **24.6 ms**（35×）、poll 420 ms → **未进榜（<1.8 ms，
   >230×）**、futex 240 ms → **未进榜（>130×）**。
2. **任务 C 的判断**：poll 与 futex 都**不是**目标（干净账里连 top36 都进不去），
   所以「poll 仍 >200 ms 就攻 poll」的前提不成立。
3. 内核态占比仍然高：主进程 `stime:utime` 中位 **135 : 32 ticks ≈ 81 % 内核态**（>40 %），
   所以仍应看内核侧——但**不是**这三个 syscall。
4. **新线索（重要）**：主进程 8 s 内消耗的内核 CPU 约 **1.35 s**，而全部 syscall 的 CPU 合计只有
   约 **0.1 s**（见 §1 表）——**syscall 只解释了 ~10 % 的内核时间**。剩下的内核时间不在
   `syscall_time` 的统计范围里（页错误、调度、中断）。下一轮应转向**缺页路径**，而不是继续在
   syscall 名单里找。

## 1. 任务 A：干净账（3 轮，取中位）

### 1.1 每轮进程 stat（只读一次）

```
run1_ff_stat pid=182 utime=37  stime=388  rss_pages=22644
run2_ff_stat pid=216 utime=32  stime=135  rss_pages=22644
run3_ff_stat pid=250 utime=31  stime=112  rss_pages=22645
```

（run1 是冷轮，stime 388 ticks 明显偏高；中位取 run2/run3。）

### 1.2 top12（按 cpu_ns，三行同一 syscall 分别是 run1/2/3）

| syscall | cpu_ns（run1 / run2 / run3） | 中位 | 次数 | 单次 |
|---|---|---|---|---|
| `openat` (257) | 117.06 / 19.37 / 15.60 ms | **19.37 ms** | 537 | 36 µs |
| **`clone` (56)** | 27.46 / 24.61 / 21.21 ms | **24.61 ms** | 5 | **4.92 ms** |
| `execve` (59) | 18.97 / 7.42 / 7.06 ms | 7.42 ms | 15 | 494 µs |
| `mmap` (9) | 18.84 / 15.17 / 13.33 ms | 15.17 ms | 707 | 21.5 µs |
| `read` (0) | 17.56 / 2.69 / 2.37 ms | 2.69 ms | 342 | 7.8 µs |
| `mprotect` (10) | 8.00 / 6.49 / 6.00 ms | 6.49 ms | 151 | 43 µs |
| `exit_group` (231) | 7.52 / 6.30 / 5.74 ms | 6.30 ms | 10 | 630 µs |
| `newfstatat` (262) | 6.26 / NA / NA | 6.26 ms | 64 | 98 µs |
| `writev` (20) | 5.06 / 4.00 / 3.93 ms | 4.00 ms | 861 | 4.6 µs |
| `wait4` (61) | 3.05 / 2.69 / 1.91 ms | 2.69 ms | 18 | 149 µs |
| `access` (21) | 2.82 / NA / NA | 2.82 ms | 65 | 43 µs |
| `unlinkat`? (83) | 3.45 / NA / NA | 3.45 ms | 13 | 266 µs |
| `faccessat2` (439) | 2.08 / NA / NA | 2.08 ms | 1 | 2.08 ms |
| `clone3` (435) | 1.84 / NA / NA | 1.84 ms | 6 | 307 µs |

`poll`(7)、`futex`(202) **在 36 行里一次都没出现**（榜尾的门槛是 cpu_ns≈1.8 ms）。

### 1.3 与旧账逐项对比

| syscall | 旧账 | 干净账（中位） | 偏差 |
|---|---|---|---|
| clone | **850 ms**（15 次，56.5 ms/次） | **24.6 ms**（5 次，4.92 ms/次） | **35× 偏大** |
| poll | **420 ms** | < 1.8 ms（未进榜） | **>230× 偏大** |
| futex | **240 ms** | < 1.8 ms（未进榜） | **>130× 偏大** |

旧账的问题来源（已定位）：那些轮次里同时跑着 `xwd` 轮询、`/proc/<pid>/stat` 采样循环、
`xdotool` 等，**探针自己**制造了大量 `poll`/`futex`/`clone` 负载，并且逐 syscall 计时放大了开销。

## 2. 任务 B：内核态 vs 用户态

| 轮次 | utime | stime | 内核占比 |
|---|---|---|---|
| run1（冷） | 37 | 388 | 91 % |
| run2 | 32 | 135 | **81 %** |
| run3 | 31 | 112 | 78 % |

中位内核占比 ≈ **81 %**（> 40 %）⇒ 瓶颈仍在内核侧。

**内核态 top3（按中位 cpu_ns）**：`clone` 24.6 ms → `openat` 19.4 ms → `mmap` 15.2 ms。

**但注意量级**：主进程 8 s 内内核 CPU ≈ 1.35 s（135 ticks），而 top 表里所有 syscall 的 CPU 加起来
约 0.10 s ⇒ **syscall 只解释了 ~7–10 % 的内核时间**。其余在内核里但不在 syscall 入口
（缺页处理、调度、中断）。

## 3. 任务 C：推荐（只基于本报告数据）

1. **不要攻 poll**：干净账里 < 1.8 ms（旧账 420 ms 是探针自造负载）。
2. **不要攻 futex**：同上。
3. **也不要只盯着 clone**：24.6 ms/8 s，占内核 CPU 的 ~2 %。
4. **应该转向缺页路径**：内核态 81 % 里只有约 10 % 落在 syscall 上；
   上一轮 `SIGPROF` 采样显示 86.9 % 的样本停在 `syscall` 指令，说明这些 syscall 的**内核态内部工作**
   （如 `read`/`mmap` 引发的缺页与拷贝）被计在 syscall 之外或分散在多个调用里。
   下一步建议：直接给 **缺页处理**（`vmar.handle_page_fault` 路径）加轻量计数/计时，
   而不是继续在 syscall 名单里找。

## 4. 证据

- `evidence/2026-10-27-clean-syscall/CS1-clean-account.txt`（42 行：3 轮 stat + top36 原始行）
- 控制台 `~/vb-ws/CS1/console.txt`
- 探针 `/tmp/vb/clean_syscall_probe.sh`
