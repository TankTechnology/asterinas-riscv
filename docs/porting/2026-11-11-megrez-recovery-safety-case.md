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
- **L5 串口 SysRq**：`kernel/comps/uart/src/sysrq.rs` + UART 驱动。
  break 之后 2 s 内：`b` 立即重启，`o`/`h` 关机，其余字节按普通输入放行。
  命令字节被消费、不会进入 tty；动作走 `ostd::power::emergency_restart` /
  `emergency_poweroff`（为中断上下文设计，不记日志、不协调其他 CPU）。
  板子的 DW APB 驱动在 RLSI 中断里先抓 LSR.BI（读 LSR 会清除它），并把 break
  的 0x00 字节丢掉；NS16550A 同样处理并打开 ELSI。主机侧
  `megrez_console.py sysrq b` 用 `tcsendbreak` 产生真实 break。
  这是"用户态已经没有可执行进程、L2/L3 都已解除"时的最后通道——2026-11-11
  的控制台事故正是这个状态。
- **L0 启动闸门**：`tools/riscv/board/megrez_console.py` 的 `boot` 子命令，
  发 `booti` 前检查（见 §2 I1）。

## 2. 不变量与证据

### I1（工具层）任何 ad-hoc 启动都带可恢复性前提，且硬件看门狗已武装并核验.

- `validate_boot_append()`：拒绝 `--root-init=interactive`、未知模式、缺失/为
  0/超 24 h 的 `asterinas.reboot_after`；要求恰好一个 `--`；**`--` 之后的
  init 选项按 stage1 的解析器做白名单校验**（未知、重复、`--debug-console`/
  `--volatile-home` 搭配非 systemd 模式都拒绝）——2026-11-05 的
  `root-init-argument` 死机就来自一个位置错误的调试选项。
- **未在本板上到达过用户态的模式**（目前只有 `systemd` 算已证）需要显式
  `--allow-experimental-mode`；**任何超过 360 s 的 deadline** 需要显式
  `--allow-long-deadline`。也就是说，一条新命令行第一次上真机时，出错代价被
  限制在数分钟内，操作者还在现场。
- **kernel deadline 必须大于 180 s**：rootfs 里的用户态看门狗
  （`megrez-safe-reboot`）在内核硬 deadline 前 180 s 提前重启；deadline ≤180 s
  会被它折叠成立即重启，反而打断启动（2026-11-11 S2 实测）。闸门现在直接拒绝
  该区间；脚本本身也改为"预算不足就拒绝"，不再推导出 1 秒。
  这条是**跨层交互**：内核、用户态看门狗、启动闸门各自的规则都正确，组合起来
  才暴露出问题，所以它现在同时被三处的测试覆盖。
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
  - 2026-11-11 的实例：用 `megrez_console.py push`（`cat > file`）向规范模式
    tty 推二进制 cpio，0x04 提前结束 `cat`，其余字节成为 shell 输入，debug
    console 的 bash 失去读端；tty 仍在回显，而两条看门狗都已按设计解除，
    板子无法从任何软件通道挽回。根因是"把二进制喂给规范模式 tty"，不是
    内核缺陷。
  - 已实现的主机侧防护（`test_megrez_console_push.py`）：
    `push` 只对纯文本走 `cat`；含控制字节的 payload 自动改走 base64 文本
    + 板上 `sha256sum` 校验；任何预计超过 240 s 的串口传输直接拒绝（回到
    "超过四分钟必须走网络"的规则）。debug console 的 bashrc 增加 `set +m`，
    关闭作业控制后 bash 不会把终端交给前台作业组，也不会被
    SIGTSTP/SIGTTIN/SIGTTOU 停住。
  - 残留：这两项要等下一次刷 stage1/rootfs 才在板上生效。在此之前，
    "控制台失去读端"仍然只能物理复位；桌面 ready 之后保留一条只覆盖
    控制通道的长 deadline 仍未设计（见 §5 待办）。
- **R2 硬件级故障**：SoC/供电/看门狗时钟故障，软件无法覆盖。
- **R3 QEMU 语义不同**：虚拟平台默认两网关闭，这是为了让"预期失败"的测试
  能干净地停下；QEMU 不需要人手复位。
- **R4 恢复后状态**：L1/L2 都会重启而不是保留现场；串口日志是唯一的现场证据。

## 5. 验证状态与待办

已做：
- Kani 5 条证明；boot-recovery crate 4 个单元测试；闸门 31 个单元测试；
  kernel ktest（定时器状态机）。
- release 镜像确定性构建（同一树两次构建 sha256 一致）。
- **启动失效模拟套件**（`rt recovery-sim`，`tools/riscv/qemu/recovery_sim.py`）：
  用 release 内核在 QEMU 上逐类制造失效，统计启动次数与恢复标记。

  | 用例 | 结果 |
  | --- | --- |
  | stage1 `interactive` 标签不匹配（原始事故） | 4 次启动、4 次 `ARMED` → 反复自愈 |
  | stage1 `basic` 无 busybox（第二次事故） | 4 次启动、4 次 `ARMED` → 反复自愈 |
  | stage1 未知 init 选项（11-05 `root-init-argument` 事故） | 3 次启动、3 次 `ARMED` |
  | `--root-init` 放在 `--` 之前（无参数 → 默认 interactive） | 3 次启动、3 次 `ARMED` |
  | 纯挂起 userspace（init 只 `pause()`） | 5 次启动、5 次 `ARMED` |
  | panic 策略与 deadline 同时开启（11-10 回归：策略抢占后定时器不 arm） | `PANIC_REBOOT_ARMED` 与 `SYNC_START` 同时出现，deadline 触发了重启 |
  | panic + `panic_reboot=1`（deadline=0） | 47 次重启 → panic 策略单独驱动 |
  | panic + `panic_reboot=0` | 1 次启动后 QEMU 退出 → 基线未变 |
  | QEMU 无 deadline（虚拟平台默认关闭） | 1 次启动、`DISABLED` → 设计如此 |
  | 闸门拒绝两次真实事故的命令行 | 均拒绝 |
  | 闸门拒绝未知 init 选项 / `--root-init` 位置错误 | 均拒绝 |

  套件拒绝使用大于 10 MB 的镜像：`make ktest` 会把 release 镜像覆盖成
  dev/ktest 构建，那会让所有用例超时并看起来像安全机制失效（第一次运行就
  踩到了）。
- **串口 SysRq 端到端（2026-11-11，QEMU virt，`rt sysrq-sim`）**：5/5 通过。
  QEMU 的 telnet 字符设备把 `IAC BRK`（0xff 0xf3）转成真实 break（LSR.BI），
  用例覆盖：break+`b` 后启动次数 1→2、break+`o` 后 QEMU 退出、break 过期 3 s
  后 `b` 不生效、没有 break 的 `b` 不生效、break+`x` 不生效。
  板子的 DW APB 驱动路径由 ktest 覆盖：aster-uart 89/89 通过，含模拟 LSR.BI
  的用例（0x00 不当输入、break 只上报一次）和 6 个 SysRq 状态机用例。
  这组测试在实现过程中抓到一个真实的 off-by-one：跳过 break 的 0x00 时仍占用
  缓冲区槽位，会把未写入的零交给 tty 并丢掉其后的第一个真实字节（板上表现
  为"break 后按 b 不生效"）。已改为独立写索引并回归通过。
  **DW APB 的 RLSI 路径另有一个会让板上 SysRq 必然失效的 bug**：RLSI ack 读
  LSR 会清 `LSR.BI`（真实硬件语义），随后的 receive 路径把这个 break 的零字节
  当普通数据交付（tty 收到 NUL），SysRq filter 又把 NUL 当作 batch 首字节取消
  2 s 窗口。修复是 ack 在 `LSR.BI|LSR.DR` 时记下 `break_byte_pending`，receive
  路径丢掉对应字节（ktest `dw_apb_line_status_ack_consumes_the_breaks_zero_byte`）。
- **Renode 平台上的 DW APB SysRq 端到端（2026-11-11，
  `tools/riscv/renode/renode_sysrq.py`）**：RLSI+`b`、RLSI+`o`、RDI+`b` 三组
  全部通过。Renode 的 16550 模型没有 `SendBreak`，测试用 monitor 的 `python`
  反射模型私有字段注入：塞入 break 的零字节、置 `LSR.BI`、选
  `ReceiverLineStatusIrq`、拉 IRQ，并用 `pause`/`start` 保证 guest 看不到中间
  状态。单次启动内验证：普通输入交付且无动作、break+未绑定键交付且无动作、
  break+命令键触发动作。
  平台还建模了复位契约：0x1000 处 16 字节 BootROM（设 a0/a1 后跳 OpenSBI）、
  DT 里的 `syscon`/`syscon-reboot`/`syscon-poweroff` 节点，以及一个记录写入
  的 Python 外设（`0xbad` 重启 / `0xdead` 关机）。因此测试能断言
  `sysrq b/o` 经过 SBI 真的到达平台复位寄存器——和板上 OpenSBI 1.5 报告的
  `Platform Reboot Device : eswin_eic770x_reset` 是同一契约。完整二次启动在
  Renode 里会停在 OpenSBI 的 `sbi_hsm_init`（generic 固件 + Renode 复位语义
  的产物），断言停在平台写入这一步。
  上述 RLSI bug 就是这个平台抓出来的；修复前后同一测试分别 FAIL/PASS。
  QEMU SysRq 5/5 与 recovery-sim 15/15 在修复后重跑，其它重启路径未受影响。
  真机验证待板子恢复后进行。
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
5. **控制通道看门狗（设计未定）**：桌面 ready 解除 L1/L2 之后，"控制台失去
   读端"（本次事故）没有任何软件自恢复。需要一条只覆盖控制通道的长 deadline
   或 console 单元的 `WatchdogSec=`，让"系统活着但没人能操作它"也能自己回来；
   先设计、先在 QEMU 里验，再上板。
6. 把 `set +m` 与 base64 push 带进下一次 stage1/rootfs 构建，刷板后重跑一次
   "二进制串口传输"演练，确认 console 不再可能被停住。

## 6. 如何运行这些检查

```sh
# 决策表的形式化证明
cargo kani -p boot-recovery
python3 tools/riscv/concurrency-models/run.py --kani --only boot-recovery

# 启动闸门与看门狗核验的回归
python3 -m unittest tools.riscv.tests.test_megrez_console_boot_gate

# 串口推送不再喂二进制 / 超预算拒绝的回归
python3 -m unittest tools.riscv.tests.test_megrez_console_push

# 串口 SysRq：break+b/o 及其防误触用例（真实 break，QEMU telnet 字符设备）
python3 -m tools.riscv.qemu.serial_sysrq_sim

# 串口 SysRq：DW APB 驱动路径（Renode 平台，RLSI/RDI 两种中断原因）
tools/riscv/renode/renode_sysrq.py --opensbi <opensbi>/fw_jump.bin --cause rlsi --key b

# 真机 ad-hoc 启动（走闸门，自动核验并武装看门狗）
tools/riscv/board/megrez_console.py boot \
  --kernel /root/kernel-rel.img \
  --append "console=ttyS0 init=/init asterinas.reboot_after=1800 -- --root-init=basic" \
  --expect-kernel-size "$(stat -c %s target/osdk/aster-kernel-osdk-bin.Image)"
```

## 7. 2026-11-14 复核：逐层在真机上重新确认

板子当天被重启多次（部署内核、SysRq 演练），每次启动的串口日志都留有证据。

| 层 | 证据（串口） | 结论 |
| --- | --- | --- |
| L0 启动闸门 | 本次启动走的是默认 `bootcmd`，未使用 ad-hoc `booti` | 不适用，未放松 |
| L1 硬件看门狗 | `ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=active period_seconds=1` | 在位（U-Boot 武装 → 内核接手喂狗） |
| L2 软件 deadline | `ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=900 source=cmdline` | 在位；用户态就绪后 `ASTERINAS_USERSPACE_REBOOT_DISARMED` |
| L3 panic 重启 | `ASTERINAS_PANIC_REBOOT_ARMED source=cmdline` | 在位 |
| L5 串口 SysRq | **首次真机验证**：`!SYSRQ b` → 整板重启，`uptime` 归零到 38 s，桌面重新 ready，门户会话未断 | 通过 |
| U-Boot 回退 | `boot.scr` 里 `as_kernel_backup=/root/kernel-rel.img.bak`，主镜像加载失败时自动加载备份 | 在位（未触发） |

L2 在板上仍未被"故意卡死"触发过（§5 待做 3），L1 也还没做过"故意停中断"
的强制复位演练——这两项都要牺牲一次启动，等有需要再排。

### 新增：部署前门禁 `tools/riscv/board/deploy_kernel.sh`

2026-11-14 当天差点把一个"QEMU 里全偏移 235–264 MiB/s、板上只有 7 MiB/s"
的 `__memcpy_fallible` 推上板；当时的部署脚本只校验 sha256，这类问题它拦不住。
现在部署分三步，任一步不过就不动板子：

1. `preflight_kernel.sh`：镜像大小必须落在 `0x80200000..0x88000000`
   （boot.scr 的 `as_kernel_addr`/`as_dtb_addr`，上限 126 MiB）、首指令必须是
   RISC-V 跳转（挡住 ELF/截断/错架构）、并**用该镜像在 QEMU 里跑到用户态**
   （`ASTERINAS_PREFLIGHT_USERSPACE_OK`）。负向对照已验证：shell 脚本和随机
   二进制都被拒绝。
2. 板上必须有 `/root/kernel-rel.img.bak`、`/root/repair.cpio.gz`、`/root/boot.scr`，
   且剩余空间 ≥ 3 倍镜像大小——回滚链不完整就拒绝部署。
3. 传到 `.new`，校验 sha256 与主机一致后才重命名覆盖，传输中断不改变任何东西
   （串口与网络都在传输途中死过）。

`--check-only` 只做 1、2 并报告板上当前镜像哈希，可随时当健康检查用。

已知盲区：QEMU 跑不出平台相关的性能/语义问题（非对齐访问 trap 就是例子），
所以门禁之外仍保留 `.bak` 与一条命令回滚：

```sh
cp /root/kernel-rel.img.bak /root/kernel-rel.img && sync && reboot -f
```

2026-11-14 复核时，从当前源码树重新构建出的镜像与板上运行的
`0fce3a3c…` 逐字节一致，说明"源码 ↔ 构建 ↔ 真机"三者对得上（确定性构建仍然成立）。

## 8. 2026-11-14 真机演练：L2 与 L1 首次在板上触发

§5 待做 1 与 3 在同一天补完，两次演练都不碰启动路径（bootcmd 与 boot.scr 未改）。

### L2：内核 900 s deadline 在板上自己把系统拉回来

做法：把三条解除路径全部停用（移走 `asterinas-watchdog-disarm.service` 与
`asterinas-safe-reboot.service`，mask `asterinas-desktop-ready.service`），
恢复脚本 `/root/watchdog-drill/restore.sh` 事先放好，然后正常重启。

- 该次启动只有 `ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=900 source=cmdline`，
  **没有** `USERSPACE_REBOOT_DISARMED`；
- 到点（15:25:14）打印 `ASTERINAS_SOFTWARE_REBOOT_SYNC_START` 与
  `SYNC_COMPLETE`，随后 OpenSBI 重启、U-Boot 按默认 bootcmd 起回桌面；
- 演练后三个单元已恢复（`enabled` / `enabled-runtime` / `enabled`），
  下一次开机 `ARMED → DISARMED` 正常。

这条证明的是：**用户态没有任何进展时，RISC-V 定时器中断仍被内核取走**，
这是整个 L2 层成立的前提。

### L1：停止喂狗后，DesignWare 看门狗真的复位了 SoC

L1 依赖一个从未在真机上验证过的假设（§3 A1/A2）：U-Boot 武装的 DW 看门狗
在没人喂的时候会复位 SoC。健康板子上永远不会出现"没人喂"，所以加了一个
受控入口：`/proc/sys/kernel/asterinas_watchdog_drill`（写 1 即停止心跳，
需 `CAP_SYS_ADMIN`，读回 0 表示已停）。

- 15:31:24 写入后文件立即变为 `0`；SSH 会话随之中断；
- 45 s 后（15:32:09）U-Boot banner 重新出现，板子按默认路径起回桌面，
  `uptime` 归零，三层看门狗重新武装。

这关闭了安全案里最大的未知：L1 不再是"设计上应该能救"而是实测能救。
演练钩子不会在正常启动时生效（只由这个 procfs 写触发），代价是它给 root
留了一个"把板子拖到下一次看门狗复位"的开关——调试设施，已在文件头注明。

## 9. 控制通道看门狗（§5 待做 5）：已实现并在板上验证

事故现场是"系统活着、端口在回显、但没人能操作它"：二进制经 `cat` 推进规范
模式 tty，shell 被 SIGTTIN 停住或丢掉读端。此时 `Restart=always` 完全无效
——进程没退出，只是不读了——而 L1/L2 早已在桌面就绪时被解除。

新增 `asterinas-console-watchdog.service`（`tools/riscv/debian/rootfs/`），
每 60 s 读一次 `asterinas-debug-console.service` 的 MainPID：

| 观察到的状态 | 判据 | 动作 |
| --- | --- | --- |
| `R/S/D/I` | 正常在跑 | 什么都不做 |
| `T/t`（SIGTTIN 停止） | `/proc/<pid>/stat` 第 3 字段 | 连续 2 次 → 重启控制台单元 |
| `Z` / 进程不存在 / MainPID 为 0 | 通道已没有读者 | 同上 |
| 重启超过 3 次仍不恢复 | — | `sync` + `reboot -f`（其它层都已解除） |

板上实测（2026-11-14，两次）：

1. 第一次用**部署的 60 s × 2 次**配置，对一个真实控制台 shell `kill -STOP`：
   两个周期后控制台单元被重启，`ASTERINAS_DEBUG_CONSOLE_READY` 重新出现
   （t=378 s），控制台恢复可用。（该次的标记行被单元重启时的 tty 重置吞掉，
   所以改用下面的方式留证。）
2. 第二次把脚本当作普通进程跑（间隔 5 s），输出落到文件，再次 `kill -STOP`
   控制台 shell，得到完整证据：

   ```
   ASTERINAS_CONSOLE_WATCHDOG state=unusable detail=stopped failures=1
   ASTERINAS_CONSOLE_WATCHDOG state=restart attempt=1 detail=stopped
   ASTERINAS_CONSOLE_WATCHDOG state=recovered restarts=1
   ```

   控制台 MainPID 713 → 950，`/dev/ttyS0` 上的新 shell 可正常执行命令。

主机侧回归：`tools/riscv/debian/rootfs/test_console_watchdog.sh` 用 stub 的
`systemctl`/`reboot` 与假 `/proc` 覆盖 5 个用例（健康、S 状态不误杀、T 停止、
进程消失、重启无效后升级为重启），全部通过。

**顺带修掉一个运维陷阱**：从控制台 shell 里 `dropbear ... &` 启动的 SSH 属于
控制台单元的 cgroup，控制台一重启 SSH 就跟着消失（第一次演练正是这样丢了
SSH）。已改为 `systemctl enable --now asterinas-dropbear.service`（端口 22），
SSH 现在开机自启且与控制台互不牵连。

**仍未做**：`/run/asterinas-debug-console.bashrc` 里的 `set +m`（预防 SIGTTIN）
要等下一次 stage1 重建，而 stage1 在 p1，当前无写入手段；看门狗已经覆盖了
这条失效，所以不阻塞。

### 9.1 冷启动验收暴露的 target 陷阱

第一次部署后做冷启动验收，`asterinas-console-watchdog` **inactive**：单元写的是
`WantedBy=multi-user.target`，而这个镜像**永远不激活 multi-user.target**
（开机驱动是 `asterinas-debug-console.target`），于是"enabled 却从不启动"。

同一个坑顺带查出**网络看门狗其实一直没跑过**：`asterinas-net-watchdog` 在板上
`enabled`、`inactive`，说明自从它加入镜像以来就没有真正运行。两个单元的
`[Install]` 都改为挂 `asterinas-debug-console.target`（net-watchdog 保留原来的
multi-user 条目，两者并存无害）。

改动后在板上 `reenable` 并做一次冷启动验收：

| 单元 | 冷启动后 |
| --- | --- |
| asterinas-debug-console | active |
| asterinas-dropbear | active（端口 22，无需手工启动） |
| asterinas-console-watchdog | active |
| asterinas-net-watchdog | active（此前从未运行） |
| asterinas-safe-reboot | active |
| 内核三层（L1/L2/L3） | `ARMED … seconds=900` → 桌面就绪后 `DISARMED` |

`dmesg` 无 I/O 错误。至此，"一块刚上电的板子"不再需要任何手工步骤就能同时
具备：硬件看门狗、内核 deadline、用户态 deadline、串口控制台、SSH、控制台
看门狗、网络看门狗七条通道。
