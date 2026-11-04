# Megrez 帧分配器：管道与 fork 的实测与优化

日期：2026-11-04
硬件：Megrez（EIC7700，4×RV64，16 GiB），内核 release 构建

## 基线（优化前，内核 9a2cfde2）

基准为本目录外的两个自包含程序（`tools/riscv/perf/syscall_bench_board.c`、
`pipe_create_bench.c`），板端静态编译或交叉编译后直接运行；每项打印
`BENCH <name> <ns/op>`，一轮 100k–2M 次调用取平均。

| 基准 | ns/op |
| --- | --- |
| clock_gettime (vDSO) | 398 |
| getpid / getppid | 3,266 / 3,696 |
| futex wake | 4,041 |
| pipe write+read 64B | 11,377 |
| stat | 15,579 |
| unix socket create+close | 19,966 |
| open+close | 28,059 |
| **pipe create+close** | **776,532** |
| **fork+wait** | **5,668,805** |

管道生命周期拆分：创建 287 µs，关读端 12 µs，关写端（释放 64 KiB 环）494 µs。

## 根因

管道的环缓冲是 64 KiB = 16 个物理连续帧：

- 每 CPU 帧缓存只覆盖 1–4 帧（`osdk/deps/frame-allocator/src/cache.rs` 的
  `_ => pools::alloc`），16 帧请求每次都进 buddy 池做加锁分裂；
- `Segment::drop` 逐帧调用 `dealloc(addr, PAGE_SIZE)`，一次管道关闭产生 16 次
  单帧归还，把容量 12 的 order-0 缓存打溢出，进一步把合并操作推到全局池。

## 改动

1. `ostd/src/mm/frame/segment.rs`：`Segment::drop` 逐帧递减引用计数，把释放的
   连续帧合并成区间，每段只调用一次 `dealloc`。被克隆片段共享的帧自然把区间
   切开，语义与逐帧释放一致。
2. `ostd/src/mm/frame/mod.rs`：新增 `Frame::release_ref`，只释放引用、不做归还，
   供上述合并路径使用。
3. `osdk/deps/frame-allocator/src/cache.rs`：每 CPU 缓存新增 16 帧（64 KiB）
   档位；批量填充失败时回退到单段分配，避免把缓存填充失败变成新的 ENOMEM。

## 结果

| 基准 | 优化前 | 优化后 |
| --- | --- | --- |
| pipe create+close | 776,532 | 27,316（28×） |
| fork+wait | 5,668,805 | 2,490,575（2.3×） |
| pipe write+read 64B | 11,377 | 8,555 |
| getpid / getppid | 3,266 / 3,696 | 2,340 / 2,463 |

管道生命周期：创建 287 → 57 µs，关写端 494 → 47 µs；单条 create+close 的稳态
已是 27 µs，剩余开销主要在两个匿名 file/inode 对象上（单文件的 unix socket
也要 20 µs），不再是缓冲分配。

## 验证

- QEMU：pipe/fork/mmap 子集 19/19；arch-riscv64 全套 140 pass / 1 fail / 17 conf
  / 0 crash，唯一失败是 `clock_gettime03` 的粗时钟容差抖动（此前也单次出现）。
- 板子：新内核经 RockOS/HTTP 传输并校验 SHA-256 后部署，实测数据如上。

## 后续测量：fork 与唤醒（2026-11-04 晚）

帧分配器改动之后，`fork` 仍约 2.5 ms。用 `fork_bench.c`、`wakeup_bench.c`、
`futex_wake_check.c` 拆分（同一块板、同一 release 内核）：

| 场景 | 结果 |
| --- | --- |
| fork + wait（未触碰内存） | 2.72 ms |
| fork 不等待（仅创建），n=30 | 1.36 ms/op |
| reap（子进程已退出） | 227 µs |
| vfork + wait | 2.90 ms |
| fork + wait（16 MB 已触碰） | 4.69 ms |
| fork + wait（64 MB 已触碰） | 15.0 ms |
| futex 唤醒→线程运行 | 42 µs |
| pthread create + join | 21 ms |

结论：`vfork`（不复制地址空间）与 `fork` 一样慢，说明基准成本不在页表复制；
页表复制按每页约 180–200 ns 线性增长（64 MB 时 15 ms）。单次 futex 唤醒 42 µs
是正常的，因此 `pthread_create+join` 的 21 ms 主要不在唤醒路径，而在线程栈
建立（musl 每次 mmap/mprotect 保护页）或任务栈分配（OSTD 默认 512 KiB，
每次走全局 buddy 池）。下一步要真正定位，需要内核内计时（在 fork/clone 各阶段
打点），而不是继续从外部猜。

调试中发现的两点值得记住：300 个同时存活的子进程会让 `fork` 不等待阶段卡住
（需要用增量进度和较小 n 复现）；`futex_wake_check` 证明"唤醒丢失"可以排除。

## 上传路径（本轮建立）

板子网络在 RockOS 下可用：U-Boot `sysboot mmc 1:1 …` 进 RockOS，门户登录后
从主机 HTTP 取内核，写入 p2 的 `/root/kernel-rel.img`（bootcmd 的固定路径），
重启即生效。Asterinas 自身的网络在门户会话有效时同样可直接下载，省掉 RockOS
往返。
