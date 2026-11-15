# 函数级跟踪（阶段 3，2026-11-15）

阶段 3 的目标是回答一个问题：`-Z instrument-mcount` 在我们这条 Rust + riscv64 链路上
能不能用。答案是**能用**，而且做出来了一个可用的函数跟踪器——代价是它只能当调试
内核用（关闭跟踪时仍慢 2.2×），日常内核不带插桩。

## 1. 验证结论（研究部分）

| 问题 | 结果 |
| --- | --- |
| `-Z instrument-mcount` 是否支持 riscv64 | ✅ 每个函数序言生成 `mv a0, ra; call mcount`（实测 11 601 个调用点） |
| `mcount` 自己会被插桩吗 | ✅ 会（普通 Rust 函数），**所以普通 Rust 实现会无限递归** |
| `#[naked]` 函数会被插桩吗 | ❌ 不会（没有序言），这是唯一安全的落脚点 |
| inline asm 里能用 `amoswap` 吗 | 需要 `.option arch, +zaamo`（目标基线特性里没有 Z(aamo)） |
| 谁把 RUSTFLAGS 吃掉了 | `cargo osdk build` 会用自己的列表**覆盖**环境里的 `RUSTFLAGS`，已在 `osdk/src/commands/build/mod.rs` 改为合并 |

## 2. 设计

```
函数序言:  mv a0, ra ; call mcount
mcount (naked asm):
    armed?             -> 否，直接 ret（一条 load + 分支）
    busy[cpu]?         -> 是，ret（重入保护）
    call mcount_slow(ra)
mcount_slow (Rust):
    ring::push_at(FUNCTION, pid, tid, ra-as-bytes, Jiffies::now())
```

要点：

- **重入保护必须是 per-CPU 且原子的**。全局标志让 4 个 CPU 抢同一条缓存行（系统直接
  失去响应）；非原子的 load/store 守卫又会让两个 CPU 同时进入，其中一个清标志后另一个
  递归进自己已持有的环形缓冲锁 → 死锁。现在用 `gp + offset` 指向 `cpu_local!` 里的
  每 CPU 标志，用 `amoswap` 做 test-and-set。
- **时间戳用 Jiffies**。`read_monotonic_time()` 会拿 clocksource 的读锁，而 mcount 会在
  **clocksource 自己的更新路径**里被调用（定时器中断里的每个函数都插桩），于是同一 CPU
  自锁。Jiffies 是无锁原子读，代价是函数记录只有 tick 级时间戳。
- **环形缓冲的锁内不能产生新记录**。读者/写者持锁时，`memcpy` 之类的普通函数调用也会
  触发 mcount → 同一 CPU 再次抢自己的环形缓冲锁 → 死锁。现在 `ring` 用 per-CPU 的
  `IN_RING` 标志：持锁期间产生的记录直接丢弃（对应 Linux 的 "recursion protection"）。
- **记录的是被调用函数**：用 mcount 自己的 `ra`（`call mcount` 的下一条指令，位于被调用
  函数体内），而不是编译器传进来的 `a0`（调用者的返回地址）。
- **`task_ids` 不能 panic**：函数跟踪会在进程销毁途中触发，此时 `Weak<Process>` 已无法
  upgrade；现在 pid 退化为 0，而不是把整机带走。

## 3. 使用

```sh
tools/riscv/ksym/build_kernel.sh --function-trace     # 构建带插桩的内核（约 2 分钟）
# 部署后：
echo 1 > /proc/trace/events/ftrace/function/enable
<跑你要观察的东西>
echo 0 > /proc/trace/events/ftrace/function/enable
grep ftrace:function /proc/trace/trace | head
```

输出（板上实测）：

```
   36.836000 [  0] 56/56 ftrace:function: func=<aster_kernel::fs::vfs::path::dentry::DentryChildren>::find+0x2e
   36.836000 [  0] 56/56 ftrace:function: func=memcmp+0x1e
   36.836000 [  3] 194/459 ftrace:function: func=<aster_kernel::time::core::timer::TimerManager>::process_expired_timers+0x2a
```

`.L` 开头的编译器本地标签已从符号表里过滤，避免"最近的符号是标签而不是函数"。

## 4. 开销与定位

| 内核 | `getpid` 往返 | 镜像大小 |
| --- | --- | --- |
| 普通构建 | 8.6 µs | 9.0 MB |
| `--function-trace`（**未开启跟踪**） | 18.6 µs | 10.5 MB |

关闭跟踪时每个函数调用仍多一条 `call mcount`——这就是 2.2× 的来源。Linux 靠 ftrace 的
动态补丁（未启用时把调用点改成 nop）避免这个代价，那是后续工作。**所以插桩内核只用于
调试，板子的默认内核保持普通构建。**

## 5. 验收

- 板上开 `ftrace/function` 后 2 048 条记录（环形缓冲上限），符号、pid/tid、CPU 全部正确；
- 与 `sched`/`mm`/`syscall` 事件同一时间轴：一次 `sh -c /bin/true` 产生 283 次缺页、
  42 次切换、29 次唤醒、403 次系统调用，可与函数记录交叉定位；
- 关闭事件后系统行为与普通内核一致（`--failed` 为空、桌面正常）。

## 6. 踩过的坑（每个都让板子挂死过一次）

1. 普通 Rust 的 `mcount` 无限递归（自身序言又调用 `mcount`）→ 必须 naked。
2. 全局守卫 → 4 CPU 争用一条缓存行，系统失去响应 → 必须 per-CPU。
3. 非原子守卫 → 两个 CPU 同时进入 + 清标志 → 递归进自己持有的锁 → 死锁。
4. `read_monotonic_time()` → clocksource 更新路径自锁 → 改用 Jiffies。
5. `PosixThread::process().unwrap()` → 进程销毁期 panic → `task_ids` 改为不会 panic。
6. 环形缓冲锁内调用被插桩的函数 → 自锁 → `IN_RING` 标志丢弃锁内记录。
7. `cargo osdk build` 覆盖 `RUSTFLAGS` → 补丁 osdk 合并环境变量。
