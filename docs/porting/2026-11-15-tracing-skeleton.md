# trace 骨架（阶段 1，2026-11-15）

ftrace 形状的跟踪基础设施：per-CPU 环形缓冲、事件注册表、开关、以及对外的
`trace` / `trace_pipe` / `events/*/enable` 接口。阶段 2 的埋点直接往这套东西里加。

## 1. 接口（为什么挂在 `/proc/trace`）

Linux 把这些文件放在 tracefs（`/sys/kernel/tracing`）。我们还没有 tracefs，
所以先挂在 procfs 下，**文件名、读写语义、记录格式都与 ftrace 一致**，只有挂载点不同：

```
/proc/trace/trace                           缓冲区文本（一次快照）
/proc/trace/trace_pipe                      同上，读完即清空
/proc/trace/tracing_on                      0 / 1，全局开关（默认 1）
/proc/trace/available_events                subsystem:event，每行一个
/proc/trace/trace_marker                    写一行字进缓冲区（地标）
/proc/trace/events/<sub>/<ev>/enable        单事件开关（默认 0）
/proc/trace/events/<sub>/<ev>/format        事件字段格式
```

记录格式（ftrace 风格）：

```
   42.826142 [  2] 641/641 tracing:trace_marker: visible-marker
   43.862329 [  3] 58/58 syscall:sys_enter: nr=291
```

## 2. 实现要点

- **环形缓冲**（`kernel/src/trace/ring.rs`）：每 CPU 512 个定长记录槽（约 60 KiB/CPU），
  写满就覆盖最旧的（Linux 的 overwrite 模式）。锁是 `SpinLock<_, LocalIrqDisabled>`：
  中断上下文里的埋点也安全。
- **事件注册表**（`event.rs` + `events/*`）：事件是静态量，带 id / subsystem / name /
  format / 原子开关；`trace_event!` 宏先查开关再格式化——关闭时只有一次 relaxed 读加
  一个分支，不分配、不加锁。
- **读取快照**：从 offset 0 开始的读会**钉住各 CPU 的 head**，后面按这个边界渲染。
  否则生产者（比如 `cat` 自己的系统调用）一直追加，文件永远没有结尾，`cat` 会挂死
  ——第一版就是这样把会话卡住的。
- **批量拷贝**：读的时候锁内只把 32 条记录拷到栈上，锁外再格式化和写用户内存。
  见下面第 (1) 个坑。

## 3. 踩到的三个坑

**(1) 持信号量写用户内存 → TLB 死锁 panic。** 第一版在环形缓冲锁内直接格式化并
`write` 到调用者的用户缓冲。写用户页可能触发缺页与页表更新，进而在 IRQ 关闭的状态下
等待远端 TLB flush，`ostd` 直接 panic：

```
Uncaught panic: Waiting for remote flush with IRQs disabled
at ostd/src/mm/tlb.rs:151
```

修法：锁内只 `memcpy` 若干条记录到栈上（`BATCH = 32`），锁释放后再做格式化/写用户内存。
这条约束对阶段 2 的所有读接口都成立：**持 IRQ 关闭的锁时不要碰用户内存、不要分配**。

**(2) 读不完的文件。** 见上面的快照语义；快照在读完时释放，下一次 `cat` 重新取。

**(3) `trace_marker` 也要受 `tracing_on` 管。** 第一版 marker 直接 `ring::push`，
`tracing_on=0` 时照样记录。现在 marker 只受全局开关控制（不需要单事件 enable，
这正是它和普通事件的差别）。

## 4. 验收（板上，2026-11-15）

| 项 | 结果 |
| --- | --- |
| 打开 `sys_enter` 后跑负载 | `cat /proc/trace/trace` 正常结束（2127 行），每行带 时间/CPU/pid/tid |
| 关闭事件 | 缓冲区不再增长（2049 行封顶 = 1 行头 + 2048 条） |
| `tracing_on=0` | 连 `trace_marker` 都不记录（只剩头行） |
| `trace_pipe` | 读完自动清空（再读只剩头行） |
| 关闭事件时的开销 | 20 万次 `getpid`：8.1 µs/次（关闭） vs 10.3 µs/次（`sys_enter` 打开）；关闭时没有可测量的额外开销 |
| 稳定性 | 之前会 panic 的负载连续跑不再触发；`--failed` 为空、桌面正常 |
| ktest | 环形缓冲 4 项（往返、覆盖、边界、清空）+ 事件开关/注册表 2 项，全部通过 |

## 5. 下一步（阶段 2）

事件注册表已经把“加一个事件”变成三行静态量；阶段 2 按家族补
`sched_switch/wakeup/migrate`、`irq_handler_*`、`block_rq_*`、`page_fault`、`net`。
其中 `sched_*` 需要先选定埋点位置（调度器的切换点，或 ostd 的任务切换处），
这是阶段 2 的第一件事。
