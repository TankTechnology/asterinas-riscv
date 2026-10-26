# pthread_join 的 228 µs 拆解 + 线程退出/入队源码嫌疑

日期：2026-10-26
内核：`258764dee`（未改内核代码）
探针：`/tmp/vb/join_breakdown_probe.sh`（N=200，两个阶段：子线程立即退出 vs 子线程 sleep 200 µs）

## 0. 结论

- join 的 228 µs 里，**75 % 发生在子线程已经跑完、记录了退出时刻之后**（唤醒 + 回收），
  而不是"等子线程执行"。父线程在这段时间里 **CPU 基本为 0**（阻塞等待，不是计算）。
- 每次迭代的内核账（Asterinas）是：`clone3` 298 µs、**`madvise` 89.7 µs**、`exit` 36.2 µs、
  `futex` 13.7 µs、`set_robust_list` 13.5 µs——`madvise` 这项（glibc 释放线程栈）之前没有被注意到，
  单次就要 90 µs。

## 1. 任务 A：join 三段（Asterinas，JN1）

打点：`j1` = `pthread_join` 调用前；`j2` = 子线程记录"即将返回"的时刻（用户态可观测的最近点）；
`j4` = `pthread_join` 返回。**`j3`（进入 futex 等待）在用户态无法观测，记 NA**。

```
JN fast_wait_ns  med=47847  p10=45207  p90=82388
JN fast_reap_ns  med=220583 p10=126134 p90=722825
JN fast_total_ns med=294256 p10=174451 p90=807155
JN fast_join_cpu_ns med=0
JN sleep_wait_ns med=541216 sleep_reap_ns med=197891 sleep_total_ns med=1020193
```

| 段 | fast（子线程立即退出） | sleep（子线程先睡 200 µs） |
|---|---|---|
| j2−j1 等子线程 | 47.8 µs | 541.2 µs（含子线程 200 µs sleep + 调度） |
| **j4−j2 唤醒+回收** | **220.6 µs（75 %）** | **197.9 µs** |
| j4−j1 合计 | 294.3 µs | 1 020.2 µs |
| 父线程 join 期间 CPU | **≈ 0** | **≈ 0** |

**最大段 = j4−j2（子线程已结束后到 join 返回）**，且父线程在此期间不烧 CPU ⇒ 这 200 µs 是
「退出清理 + futex 唤醒 + 被调度回来」的延迟，不是父线程在干活。

## 2. 每次迭代的内核系统调用账（Asterinas，`/proc/syscall_time`）

400 次迭代（2 阶段 × 200）累计：

```
sys_top6=435:400:119406733 | 28:400:35874110 | 60:400:14518917 | 202:401:5485414 | 273:402:5424864 | 230:200:4382876
```

| syscall | 次数 | 合计 | 单次 |
|---|---|---|---|
| `clone3` (435) | 400 | 119.4 ms | **298 µs** |
| `madvise` (28) | 400 | 35.9 ms | **89.7 µs** |
| `exit` (60) | 400 | 14.5 ms | **36.2 µs** |
| `futex` (202) | 401 | 5.5 ms CPU（阻塞 278.9 ms） | 13.7 µs |
| `set_robust_list` (273) | 402 | 5.4 ms | 13.5 µs |
| `clock_nanosleep` (230) | 200 | 4.4 ms | 21.9 µs |

对照上一轮的拆解：create 段（386 µs）≈ `clone3` 298 µs + `set_robust_list` 13.5 µs + 首次调度；
reap 段（220 µs）≈ `madvise` 89.7 µs + 任务/栈销毁 + `futex` 唤醒 + 调度回来。
**`madvise` 是从未被怀疑过的一块，单次 90 µs 占 reap 段的 40 %。**

## 3. Linux 对照（JN2，同程序同条件）

```
JN fast_wait_ns med=3062  fast_reap_ns med=5868  fast_total_ns med=8838  fast_join_cpu_ns med=10859
JN sleep_wait_ns med=266509 sleep_reap_ns med=17547 sleep_total_ns med=284925
```

| 段 | Asterinas | Linux | 倍率 |
|---|---|---|---|
| j2−j1（等子线程，fast） | 47.8 µs | 3.06 µs | 15.6× |
| **j4−j2（唤醒+回收，fast）** | **220.6 µs** | **5.87 µs** | **37.6×** |
| j4−j1（fast 合计） | 294.3 µs | 8.84 µs | 33× |
| sleep 阶段 reap | 197.9 µs | 17.5 µs | 11.3× |

差距集中在 **reap 段（37.6×）**：Linux 唤醒+回收只要 5.9 µs，我们要 220 µs。
注意 Linux 侧 `fast_join_cpu_ns` 中位 10.9 µs > 墙钟 8.8 µs（多核并行计入），
而 Asterinas 侧 join 期间 CPU ≈ 0，两者都是"阻塞等待"语义，只是恢复延迟差一个量级。

## 4. 任务 B：源码嫌疑（只读，已给 文件:行号）

| # | 位置 | 观察 |
|---|---|---|
| 1 | `kernel/core/src/process/posix_thread/futex.rs:164-171`、`:485-498` | WAKE 只锁**一个哈希桶**，`remove_and_wake_items` 对该桶做一次 `retain`；不是全局遍历。与实测 futex 3.3× 的量级一致，不是 join 的主因 |
| 2 | `kernel/core/src/process/posix_thread/exit.rs:57-83` | 退出时先 `posix_process.tasks().lock()`，再 `tasks.remove_exited(&current_task)`（**O(线程数) 扫描**） |
| 3 | `kernel/core/src/process/posix_thread/exit.rs:104-106` | `pid_table::pid_table_mut().remove_thread(tid)`；`PID_TABLE` 是**全局 `Mutex`**（`process/pid_table.rs:19`），每个线程退出都要拿它 |
| 4 | `kernel/core/src/process/posix_thread/exit.rs:145-156` | `wake_clear_ctid` → `write_val(clear_ctid, 0)` + `futex_wake(…, 1, Shared)`：这就是 pthread_join 等待的那次唤醒 |
| 5 | `kernel/core/src/process/posix_thread/exit.rs:108-124` | `drop_after!` 依次释放 file table、ns proxy、vmar（每项一次加锁） |
| 6 | `ostd/src/task/scheduler/mod.rs:509-517` | `run_new_task` → `scheduler_singleton().enqueue(...)`；`fifo_scheduler.rs:108-109` 可见队列是**每 CPU 的本地队列**，没有全局锁 |
| 7 | `kernel_stack.rs`（上一轮） | `KernelStack` 销毁要**逐段释放 32 个 4 页段并解映射 128 页**；这次没有直接测到它在哪一段里发生（标记为**未验证**） |

**归类**：join 的 220 µs 是「退出清理（含 `madvise` 90 µs、全局 pid 表锁、O(n) 线程集扫描、
栈/TCB 释放）+ 一次 futex 唤醒 + 重新调度」的组合，其中用户可见的前两项（`madvise`、`exit` 系统调用）
就占了约 126 µs。

## 5. 任务 C：未做（时间用在 A/B 与 Linux 对照上）

按 ROI 排序（供下一轮直接实施）：

1. **`madvise` 90 µs**：glibc 在 join 时用它归还线程栈。先确认我们的 `madvise(MADV_DONTNEED)`
   是否逐页 unmap/清 TLB；这一条最容易验证（单次 `madvise` 微基准）。
2. **全局 pid 表锁**（`pid_table.rs:19`）：每线程退出一次全局 `Mutex`，多核下会放大 reap 延迟。
3. **`tasks.remove_exited` 的 O(n) 扫描**：线程多的进程（Firefox 66 线程）会线性放大。
4. 内核栈池（上一轮 `258764dee` 的 §4 已给设计）。

## 6. 证据

- `evidence/2026-10-27-join-and-enqueue/JN1-asterinas-probe-lines.txt`（13 行）
- `…/JN2-linux-probe-lines.txt`（Linux 同程序对照）
- 控制台 `~/vb-ws/JN1/console.txt`、`~/vb-ws/JN2/console.txt`
- 探针 `/tmp/vb/join_breakdown_probe.sh`
