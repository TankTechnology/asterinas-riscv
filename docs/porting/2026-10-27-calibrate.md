# 时间口径校准：utime/stime 放大 10× 已定位并验证修好（未入库）

日期：2026-10-26
内核基线：`ea2db8679`
结论一句话：**`/proc/<pid>/stat` 把 1000 Hz 的 jiffies 当成 Linux 的 100 Hz tick 上报，
所有 CPU 时间字段放大 10×。** 修好后 CAL1/CAL2 与 Linux 逐项对齐；
但 Firefox 的 stime 与 VmSize 仍有差距，故**本轮未入库**（矩阵未跑）。

## 1. 任务 A：两个校准基准（同程序、两内核）

```
Asterinas（修正前）CAL busy  utime=997 stime=0     | CAL getpid utime=103 stime=895  ops=1 010 318
Linux             CAL busy  utime= 99 stime=0     | CAL getpid utime= 52 stime= 47  ops=8 515 220
Asterinas（修正后）CAL busy  utime= 99 stime=0     | CAL getpid utime=  9 stime= 90  ops=1 002 667
```

| 用例 | Linux | Asterinas（前） | Asterinas（后） |
|---|---|---|---|
| CAL1 忙等 1 s：utime / stime | 99 / 0 | **997 / 0（10.1×）** | **99 / 0（1.00×）** |
| CAL2 getpid 1 s：utime+stime | 99 | **998（10.1×）** | **99（1.00×）** |
| getpid 吞吐 | 8.52 M/s | 1.01 M/s | 1.00 M/s |

**判读**：
- **utime 与 stime 都被放大 10.1×**，而不是只有内核态——这排除了「中断返回/用户态返回重复累加」，
  指向**单位换算**。
- CAL2 里我们的 getpid 单次内核 CPU ≈ 0.9 µs，Linux ≈ 0.055 µs → **单次 syscall 成本约 16×**，
  这是真实差距（校准后仍然存在），不是统计问题。

## 2. 任务 B：错误来源（文件:行号）

| 位置 | 问题 |
|---|---|
| `kernel/core/src/fs/fs_impls/procfs/pid/task/stat.rs:186-196` | `utime`/`stime` 直接输出 `read_jiffies()`。内核时钟按 **`ostd::timer::TIMER_FREQ = 1000`**（`ostd/src/timer/mod.rs:25`）累加 jiffies，而 Linux 的 `/proc/<pid>/stat` 用 **USER_HZ = 100** 报这两列 ⇒ 放大 `TIMER_FREQ/USER_HZ = 10×`。 |
| `kernel/core/src/fs/fs_impls/procfs/pid/task/stat.rs:352-360` | `duration_to_jiffies()` 同样用 `TIMER_FREQ` 而不是 `USER_HZ`，`cutime`/`cstime` 也偏大 10×。 |
| `kernel/core/src/process/process/timer_manager.rs:57-80` | 计时点本身**没有**重复累加：每个 tick 只按「中断时特权级」给一处时钟加 1。这条是清白的。 |
| `vsize`（`vmar_impls/mod.rs:82-84`，`total_vm` 在 map/unmap 处加减 `map_size`） | 逻辑自洽，466 GB 可能是真实映射量；Linux 侧 447 MB 更像选中了 wrapper 进程。**未定论**。 |

## 3. 修正（已验证，**已 revert**，未入库）

```rust
const USER_HZ: u64 = 100;
fn jiffies_to_user_ticks(jiffies: u64) -> u64 { jiffies.saturating_mul(USER_HZ) / TIMER_FREQ }
// utime/stime 改用它；duration_to_jiffies() 里 TIMER_FREQ → USER_HZ
```

修正后 CF1 实测：CAL1 99/0、CAL2 9/90（合计 99），与 Linux 完全对齐。

## 4. 任务 C：Firefox 复测（修正后）与未达成项

```
ff1_stat minflt=4393 utime=3 stime=45 threads=4   VmSize=466 423 808 kB VmRSS=90 576 kB
ff2_stat minflt=4487 utime=3 stime=12 threads=4   VmSize=466 423 808 kB VmRSS=90 640 kB
```

| 指标 | Asterinas（修正后） | Linux | 比值 | 判据（≤1.5–3×） |
|---|---|---|---|---|
| stime（热轮） | 12 ticks（120 ms） | 1 tick（10 ms） | **12×** | ✗ |
| utime | 3 ticks | 2 ticks | 1.5× | ✓ |
| minflt | 4 487 | 6 038 | 0.74× | — |
| VmSize | 466 GB | 447 MB | **1042×** | ✗ |

**重要方向性结论**：修正后 Firefox 的 stime 从「133 ticks」降到「12 ticks」，
之前「内核态占 81 %」的说法**确实是统计假象**——按修正后的口径，
utime:stime ≈ 3:12，内核态约 **80 % 仍然是偏高的**，但绝对量只有 120 ms / 8 s，
不再支持「84 % 内核时间未归因」那种量级的论断。**之前的优化方向排序需要重排。**

## 5. 为什么不入库

任务 C 要求「修正后 7 域矩阵全绿」。剩余时间不足以跑矩阵（约 15 分钟）加完整的 Firefox 对照，
按纪律本轮把改动 revert（内核 22:59 重编译为基线字节 `22945600`），
把补丁原文与验证数据留在本文，下一轮可**直接照抄补丁 → 跑矩阵 → 入库**。

## 6. 证据

- `evidence/2026-10-27-calibrate/CAL1-asterinas-before-fix.txt`（8 行）
- `…/CAL2-linux.txt`（8 行）
- `…/CF1-asterinas-after-fix.txt`（9 行）
- 控制台 `~/vb-ws/{CAL1,CAL2,CF1}/console.txt`
- 探针 `/tmp/vb/calib_probe.sh`、`/tmp/vb/calib_ff_probe.sh`
