# Megrez 启动恢复安全案（safety case）

日期：2026-11-11
背景：板子因启动参数选错（`--root-init=interactive` 与 p2 的
`ASTER_BROWSERWEB` 标签不匹配）落入 stage1 的 `fail_and_hold()`，且该次启动
没带 `asterinas.reboot_after`，任何软件通道都无法重启，只能物理复位。

本文把"不再出现板子卡死"拆成**可核对的不变量**，逐条给出代码位置、机器检查
或启动日志证据，并显式列出假设与残留风险。**"完全证明"在这里的含义是：在下面
列出的假设成立时，每一类已识别的卡死都有至少一层自动恢复；它不是对内核二进制
或整块 SoC 的形式化证明。**

## 1. 失效分类与恢复层

| 失效点 | 表现 | 恢复层 |
| --- | --- | --- |
| 我的启动工具选错模式/漏参数 | stage1 永久 hold | L0 启动闸门（宿主侧拒绝） |
| 内核正常、用户态卡死（本次事故） | 无 shell、无网络 | L2 软件恢复定时器 |
| 内核 panic | halt | L3 panic 重启 |
| 内核整体不再取定时器中断 | 全静默 | L1 硬件看门狗 |
| SBI 复位扩展缺失/调用失败 | 复位函数返回后 halt | L1 硬件看门狗（先停止喂狗） |
| 启动介质损坏到 initramfs 之前 | U-Boot 报错 | 默认 bootcmd + 复位 |

- **L1 硬件看门狗**：U-Boot 在 `bootcmd` 里武装 DW WDT
  （`mw.l 0x5080000c 0x76; mw.l 0x50800000 0x1f`，TORR=0xf）；内核
  `hardware_watchdog.rs` 在 `asterinas.hardware_watchdog_handoff=1` 时以 1 s
  周期喂狗。内核一旦不再跑定时器回调（IRQ 屏蔽、死锁、整体挂起），喂狗停止，
  SoC 在硬件超时（2^(16+15)/pclk，板上为数十秒量级）后复位。
- **L2 软件恢复定时器**：`kernel/src/boot_reboot.rs`，真机默认 900 s；systemd
  镜像在 `basic.target` 写 `/proc/sys/kernel/asterinas_reboot_watchdog=0` 解除。
- **L3 panic 重启**：同一策略函数在 fatal abort 时复用 L2 的武装状态或显式
  `asterinas.panic_reboot=1`。
- **L0 启动闸门**：`tools/riscv/board/megrez_console.py` 的 `boot` 子命令，
  发 `booti` 前检查（见 §2 I1）。

## 2. 不变量与证据

### I1（工具层）任何 ad-hoc 启动都带可恢复性前提，且硬件看门狗已武装并核验.

- `validate_boot_append()`：拒绝 `--root-init=interactive`、未知模式、缺失/为
  0/超 24 h 的 `asterinas.reboot_after`；要求恰好一个 `--`。
- **未在本板上到达过用户态的模式**（目前只有 `systemd` 算已证）需要显式
  `--allow-experimental-mode`；**任何超过 180 s 的 deadline** 需要显式
  `--allow-long-deadline`。也就是说，一条新命令行第一次上真机时，出错代价被
  限制在 3 分钟内，操作者还在现场。
- `basic` 模式必须用 `--initramfs-cpio` 提供板上将要加载的同一份 initramfs，
  并解析 cpio 确认存在 `/bin/busybox`（部署版没有，这正是 2026-11-11 第二次
  卡死的原因）。
- `arm_hardware_watchdog()`：先写 `0x51828444` 打开看门狗时钟，读
  `0x508000fc` 必须等于 `0x44570120`，执行与 `bootcmd` 相同的武装序列，读回
  `0x50800000` 必须 bit0=1，否则拒绝启动。
- `printenv bootcmd` 必须仍是含 `booti` 的默认启动项（一次断电总能回来）。
- 证据：`tools/riscv/tests/test_megrez_console_boot_gate.py`（31 例，含对
  模式×分隔符×deadline 写法的穷举"接受集 ⊆ 可恢复集"检查，以及 console
  marker、看门狗、cpio 解析的独立用例）。

### I2（决策层）真机上命令行为空时，两层恢复网都会被武装；关闭需要显式 `=0`.

- `kernel/libs/boot-recovery`：`resolve()` 是纯函数。
- Kani 证明 5 条（`cargo kani -p boot-recovery`）：
  1. 真机 + 两个开关都缺省 → deadline=900 且 panic 重启；
  2. 真机上"一个恢复网都没有"只可能来自显式请求；
  3. 显式值永不被默认值覆盖；
  4. 真机上 deadline=0 只可能是显式 0；
  5. QEMU 缺省时两网都不武装（有意的设计，单独证明）。

### I3（武装时机）恢复定时器在第一个用户任务之前注册，且每次启动可核验.

- `kernel/src/init.rs` 在 `component::init_all` 之后、`init()` 之前调用
  `boot_reboot::arm_if_requested()`。
- 启动日志：`ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=… source=default|cmdline`
  或 `ASTERINAS_SOFTWARE_REBOOT_DISABLED`；启动助手在继续任何操作前等待这一行。

### I4（触发与升级）内核健康时，即使用户态全部阻塞，定时器中断仍会被取走，并在 +10 s 内升级为强制重启.

- 定时器回调注册在全局高分辨率回调表，RISC-V 上由持有该 one-shot 定时器中断
  的 hart 执行；用户态阻塞不影响内核取中断。
- `timer_action()` 的 graceful→emergency 升级由 kernel ktest 覆盖
  （`orderly_reboot_retains_an_emergency_deadline`、`stale_timer_action_is_ignored_after_disarm` 等）。
- 工作队列提交失败时直接走 `emergency_reboot_now()`。

### I5（复位尝试）软件复位前先停止喂硬件看门狗.

- `reboot_now()` / `emergency_reboot_now()` 先调用
  `hardware_watchdog::stop_kicking_for_reset()`，再 `power::restart` /
  `power::emergency_restart`（SBI `system_reset(ColdReboot)`）。
- 若固件没有 SRST 或调用失败，`power` 会 halt；此时看门狗已不再被喂，
  SoC 在硬件超时后复位。**这条关闭了"复位函数失败 → 永远 halt"的洞。**

### I6（最后防线）内核整体不再取中断时，无人喂狗的硬件看门狗完成复位.

- 喂狗本身是定时器回调；回调不运行 ⇒ 不再喂 ⇒ 硬件复位。
- 该层不依赖 SBI、不依赖调度器、不依赖用户态。

## 3. 假设（不成立时恢复承诺失效）

- **A1**：SoC 的看门狗计数器使用独立时钟，且复位信号在 CPU 停滞时仍有效。
- **A2**：U-Boot 的武装序列在目标板上成功（I1 每次启动核验）。
- **A3**：启动介质至少完好到能把 initramfs/内核读进内存；介质在 U-Boot 阶段
  就损坏时，软件层无角色，只能依赖反复复位与默认启动项。
- **A4**：板上不存在能同时停住 SBI 复位与看门狗复位的硬件故障。

## 4. 残留风险（明确不在本承诺内）

- **R1 启动完成后的用户态卡死**：`basic.target` 解除 L2 后，内核仍健康并持续
  喂狗，L1/L2 都不触发。表现为"系统在线但桌面/某个服务卡住"，不是"整板失联"；
  由服务级看门狗（`asterinas_net_watchdog`、`megrez-safe-reboot`）处理，
  必要时仍需一次主动复位。
- **R2 硬件级故障**：SoC/供电/看门狗时钟故障，软件无法覆盖。
- **R3 QEMU 语义不同**：虚拟平台默认两网关闭，这是为了让"预期失败"的测试
  能干净地停下；QEMU 不需要人手复位。
- **R4 恢复后状态**：L1/L2 都会重启而不是保留现场；串口日志是唯一的现场证据。

## 5. 验证状态与待办

已做：
- Kani 5 条证明；boot-recovery crate 4 个单元测试；闸门 21 个单元测试；
  kernel ktest（定时器状态机）。
- release 镜像确定性构建（同一树两次构建 sha256 一致）。
- **L2 端到端复现（2026-11-11，QEMU virt）**：用与事故完全相同的失效条件
  启动（`--root-init=interactive`，rootfs 无匹配标签），命令行显式
  `asterinas.reboot_after=15`。串口记录到
  `ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=15 source=cmdline`，15 s 后
  `SYNC_START`/`SYNC_COMPLETE`，随后 OpenSBI 重新启动，第二轮再次武装并再次
  卡住——**同一失效条件下板子现在会自己回来**。复现命令：

  ```sh
  tools/riscv/debian/rootfs/build_stage1.sh /tmp/stage1-repro.cpio
  timeout 150 qemu-system-riscv64 -machine virt -m 2G -smp 4 -nographic \
    -kernel target/osdk/aster-kernel-osdk-bin.Image \
    -initrd /tmp/stage1-repro.cpio \
    -append "console=ttyS0 loglevel=warn init=/init \
             asterinas.reboot_after=15 -- --root-init=interactive"
  # 日志里 "ASTERINAS_SOFTWARE_REBOOT_ARMED" 出现两次 = 自动重启成功
  ```

待做（板子复位后，按顺序）：
1. **验证 L1**：启动后在内核里制造一次"长时间关中断"（诊断命令或临时镜像），
   确认硬件看门狗在数十秒内复位。
2. **验证 I5 顺序**：观察日志顺序，确认"先停喂狗再请求复位"；在 QEMU 上可用
   不带 SRST 的模拟，或直接检查板上一次软复位。
3. **验证板上 L2**：以 `asterinas.reboot_after=20` 启动后不进入用户态，确认
   板上也能自行重启（QEMU 已证，真机待证）。
4. 常规启动：确认日志出现 `ASTERINAS_SOFTWARE_REBOOT_ARMED … source=default`
   且 systemd 在 `basic.target` 打印解除。

## 6. 如何运行这些检查

```sh
# 决策表的形式化证明
cargo kani -p boot-recovery
python3 tools/riscv/concurrency-models/run.py --kani --only boot-recovery

# 启动闸门与看门狗核验的回归
python3 -m unittest tools.riscv.tests.test_megrez_console_boot_gate

# 真机 ad-hoc 启动（走闸门，自动核验并武装看门狗）
tools/riscv/board/megrez_console.py boot \
  --kernel /root/kernel-rel.img \
  --append "console=ttyS0 init=/init asterinas.reboot_after=1800 -- --root-init=basic" \
  --expect-kernel-size "$(stat -c %s target/osdk/aster-kernel-osdk-bin.Image)"
```
