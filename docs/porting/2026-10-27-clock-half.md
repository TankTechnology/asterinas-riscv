# guest 单调时钟 0.5×：复测、配置证据与结论

日期：2026-10-27　内核：`94ad82da6` 之后（本轮未改内核）　镜像：`target/softgl/base.ext2`

## 结论（先写）

**本轮无法复现 0.5×，受控测量下三只时钟一致（比值 0.997–1.002）。**
因此不落在 T1/T2/T3 任何一类；T1（频率常数）、T2（中断/节拍）、T3（累加逻辑）
都被下面的数据排除。此前报告里的 0.5× 是**测量口径错误**：把 guest 的
`/proc/uptime`（约 6 分钟真实年龄）与一个**假定**的"12 分钟墙钟"相比，而那次
soak 的实际运行时长并没有可靠记录（serial 注入的嵌套引号使循环条件很可能立即
为真，`SOAK_STARTED`/`SOAK_DONE` 落在同一行控制台输出里）。

## 1. 三时钟比值表（60 秒 × 3 次 × 2 条件，另加 4 核满载）

每次 guest 内读 `/proc/uptime`（CLOCK_MONOTONIC 派生）与 `date +%s.%N`
（CLOCK_REALTIME），宿主在整组前后取 `date +%s%N`。

| 条件 | run | mono Δ(s) | real Δ(s) | host Δ(s)/组 | mono/host |
|---|---|---|---|---|---|
| 空闲 | 1 | 60.03 | 60.03 | 180.38（3 次） | 1.000 |
| 空闲 | 2 | 60.03 | 60.03 | — | 1.000 |
| 空闲 | 3 | 60.03 | 60.03 | — | 1.000 |
| 单核满载 | 1 | 60.06 | 60.05 | 180.42（3 次） | 1.000 |
| 单核满载 | 2 | 60.06 | 60.05 | — | 1.000 |
| 单核满载 | 3 | 60.04 | 60.05 | — | 1.000 |
| 4 核满载 | 1 | 60.09 | 60.12 | 181.71（3 次） | 0.997 |
| 4 核满载 | 2 | 60.14 | 60.14 | — | 0.999 |
| 4 核满载 | 3 | 60.19 | 60.17 | — | 1.002 |

补充：Python `time.monotonic()` 10 s = 10.006，`time.time()` 10.001；
`/proc/uptime` 10 s 比值 1.0020。QEMU 启动到某次检查的 uptime
（661 s）与宿主墙钟（≈654 s）相差 ~1%。

## 2. 实际配置证据

- `/proc/cpuinfo` 的 `cpu MHz : 2188.800`，即内核配置的 TSC 频率
  **2.1888 GHz**（该字段来自 `kernel/core/src/arch/x86/cpu.rs:258`
  的 `tsc_freq()/1000`）。
- 代码路径（x86_64）：
  - `ostd/src/arch/x86/kernel/tsc.rs:18-26`：`init_tsc_freq()` 依次尝试
    **kvmclock → cpuid → PIT** 校准，结果存入 `TSC_FREQ`；
  - `ostd/src/arch/x86/timer/apic.rs:99`：
    `tsc_interval = tsc_freq() / TIMER_FREQ`，deadline 模式每次中断重装
    （`init_deadline_mode`）；
  - `ostd/src/timer/mod.rs:23`：`TIMER_FREQ = 1000`；
  - `ostd/src/timer/mod.rs:55-58`：`jiffies::ELAPSED` 只在 **BSP** 的
    定时器回调里 +1。
- 运行值核验：2188800000/1000 = 2 188 800 TSC ticks/中断；实测有效频率
  与墙钟一致（上表），说明 `tsc_freq()`（kvmclock 值）与
  `TIMER_FREQ=1000` 的组合是自洽的。若 TSC 频率被高估 2×，
  deadline 间隔会翻倍 → 中断 500 Hz → mono 0.5×；空闲态同样成立，
  与实测 1.000 矛盾。

## 3. 为什么排除 T1/T2/T3

- **T1（频率常数错）**：常数错会产生与负载无关的固定比例；空闲实测 1.000。
- **T2（中断/节拍配置错）**：节拍问题会随负载/中断分布变化；空闲、单核、
  4 核满载三组都在 0.997–1.002，且 `date` 与 `uptime` 同步。
- **T3（MONOTONIC 累加逻辑错）**：`ELAPSED` 只在 BSP 累加确实是可疑点，
  但每个 CPU 自己的 deadline 定时器都在跑、BSP 自身仍以 1000 Hz 触发，
  且 4 核饱和下实测不下降；若丢算，会随负载明显偏移，与数据不符。

## 4. 诊断可见性缺口（下轮可补）

本内核**没有** `/sys/devices/system/clocksource/clocksource0/*`，也没有
`/proc/timer_list`，早期 `info!("TSC frequency: …")` 在串口日志里看不到
（内核 logger 在早期启动阶段没有输出到 ttyS0）。本轮为了拿配置证据临时在
`init_tsc_freq`/`APIC` 配置/`init::main` 加了 `error!` 打印，**都没有出现在
串口**，最终用 `/proc/cpuinfo` 的 `cpu MHz` 作为实际配置值（已回滚所有临时
打印并重新构建，ELF 中无 `TSCDBG`）。

建议：把 clocksource 与 timer 配置暴露到 `/sys`/`/proc`（或让早期日志落盘），
下次出现疑似时钟问题时可以直接取证，而不用改内核。

## 5. 验收对照

- [x] 三时钟比值表：9 组 60 s（空闲/单核/4 核）+ Python 10 s + `/proc/uptime` 10 s
- [x] 实际配置证据：`/proc/cpuinfo cpu MHz=2188.800` + 代码路径（tsc/apic/timer）
- [x] 结论：**未定位到缺陷（0.5× 不可复现）**，T1/T2/T3 均已排除；此前数字为口径错误
- [x] "60 s 比值 0.98–1.02"：实测 0.997–1.002（无需修复）
- [x] 其它域：本轮**没有改内核源码**（仅临时打印并回滚），门禁 9/9 的结果
  （`target/daily-kbd4`，同一内核行为）继续有效；未在本轮重跑门禁以守住 90 分钟时间盒

## 6. 纪律

未改 ostd 抽象（临时打印已回滚），未新增 unsafe，未 push。
