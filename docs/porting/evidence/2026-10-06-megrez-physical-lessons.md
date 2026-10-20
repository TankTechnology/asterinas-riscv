# Megrez 真机故障与执行流程复盘

日期：2026-10-06（Asia/Shanghai）。
本页汇总已经保留证据的故障，并区分观测、假设和待落实措施。
本页同时保留日志审查和已完成的有界真机收据；每个实验的具体动作以对应证据目录为准。

## 最近完成的边界

2026-10-06 的 Basic 冒烟验证证明了 firmware framebuffer 的有界读写：
前 4096 字节可读，分辨率为 1920×1080，stride 为 7680，格式为 BgrReserved。
`/dev/dri` 和 `/sys/class/drm` 均不存在。
最终像素周期修改左上角 64 字节（16 个像素），回读固定颜色后恢复原内容，
发出 `sync; reboot -f`，观察到新 OpenSBI/U-Boot，并选择 RockOS。
这是一轮成功冒烟，不构成长时间稳定性、HDMI 实际画面或 GPU 加速验收。

| 本地证据 | 摘要 |
| --- | --- |
| `target/megrez-physical-step1/basic-fb-readonly-20261006/result.json` | `passed=true`；4096 字节 SHA-256 为 `36950136b22cf8bc921e8c8d21e0e33e7601291996d0694a5af92dd0d404bbd9` |
| `target/megrez-physical-step1/basic-fb-readonly-20261006/serial.log` | SHA-256 为 `7aff4b13d10b28d1f3456f6f75235ad986ae4c28c8b0bd55e6a8ec370db35445` |
| `target/megrez-physical-step1/basic-fb-pixel-20261006-r5/result.json` | `passed=true`；原始与恢复的 64 字节 SHA-256 均为 `e4af27dae19a59cafa1d0d672c8771aa482e1946d7e49cb50127df2e624fe3b1` |
| `target/megrez-physical-step1/basic-fb-pixel-20261006-r5/serial.log` | SHA-256 为 `615a428f769b8367aa6b9b3f941b7faf456fcb3b179256252b7cc2564d9cdb82` |

本轮菜单 SHA-256 为 `81541106e888ebc6faa338ed47d6e45e756689a093707652dd886e829968a3d9`。
内核 SHA-256 为 `207284b1a488c02cfe5ccf6f6cbaafafbbadb4a87c4531703f416aa70e7895ce`，
Stage1 为 `7806b8348eb10456930388bdd250880205d198035b2da5cf7be17348a2a5f671`，
DTB 为 `465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`。
串口为 `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`。

最后一次 SSH 返回 UID 1000，boot-id 为 `ae7708e1-feae-430c-8cef-a93f115eba49`。
关闭再打开串口成功，但五秒被动读取为零字节。
因此必须纠正此前汇报：SSH 可访问已验证，**串口重新打开后的命令交互及 UID 0 未验证**。
这份记录不是开发板此刻仍在线的证明。

## 导致失联或可能导致整板停顿的问题

| 问题 | 已观察到的边界 | 原因确认程度 | 后续约束 |
| --- | --- | --- | --- |
| 只依赖软件 `reboot_after` | 2026-10-03 到 Basic shell、软件重启定时器已 armed，随后 UART/网络无输出，未看到新固件周期 | 未定位内核根因；软件定时器不能覆盖关中断、内核卡死或 SBI 复位失效 | 新硬件路径必须有启动前验证的独立复位保障；软件计时器只作补充 |
| 把“只读寄存器”当成不会卡死 | 2026-10-05 GMAC-1 已输出资源/DT 标记，首个寄存器读取未返回，watchdog 后恢复到固件 | 时钟/复位前置条件缺失是有证据支持的假设，尚未确认具体寄存器依赖 | 不直接读未确认可访问的 MMIO；禁止自动重试该 GMAC-1 Probe |
| 一次启动混入多个设备 | 旧 Basic 路径同时启动 GMAC、两个 USB host、MMC，最后日志无法区分触发者 | 设备并发是定位障碍，不能因此认定某个驱动是根因 | “Basic”表示用户态模式，不表示硬件最小集；必须检查实际 DT 和驱动启动日志 |
| 未保留独立管理通道 | 桌面标记、登录提示、代理端口或 FTDI 存在不能证明可执行命令 | 是验收方法缺陷；可能在真正失联前掩盖控制丢失 | 每个交接要求新 nonce、UID 0、boot identity；串口重连后再执行同一握手 |

详细冻结证据：

- [2026-10-03 Basic 失联事件](2026-10-03-megrez-basic-console-hard-hang.md)。
  当时的软件恢复失败需要人工复位；不能用后来成功的 Basic 运行覆盖该事故。
- [2026-10-05 GMAC-1 分析](2026-10-05-gmac1-clock-gate-analysis.md)。
  严格校验器记录 `status=timeout, recovered=true, read_offsets=[]`。
  watchdog 恢复成功与 GMAC-1 功能测试失败必须分别记录。
- [最小设备桌面 canary](2026-10-05-megrez-gmac-safe-desktop-canary.md)。
  root debug console 到达，但 desktop gate 依赖 USB/网络，因而未通过。
  直接恢复 RockOS 被旧观察器误报为 U-Boot 超时；串口交互仍未资格化。

## 这次执行中可确认的流程错误

以下错误应归于实验执行，不能算成内核硬挂或由开发板承担责任。

| 错误 | 本地证据/后果 | 必须落实的改进 |
| --- | --- | --- |
| 对仍在 RockOS 的板子等待 U-Boot | `basic-fb-pixel-20261006-r2` 等提示符超时；同 boot-id 的 SSH 仍可访问 | 显式区分 RockOS、关机、固件、Asterinas 和未知态；未知态不发送控制命令 |
| 重启之后才发现输出目录已存在 | 临时 r2 脚本 `mkdir(exist_ok=False)` 抛异常，板子已经被重启 | 所有路径、入口、权限、脚本导入和参数校验必须先于重启；监听进程就绪后才能重启 |
| 未预算长串口命令耗时 | `BoardSession.send` 每字节等待 20 ms；64 字节八进制文本加命令可消耗数秒；r3 恢复命令中 watchdog 到期，尾部落到 U-Boot | 把发送、启动、采集、恢复和裕量统一计入剩余 watchdog 时间；不要靠延长 watchdog 掩盖预算问题 |
| 十六进制串与内核日志交错 | 首次像素轮在写入前解析失败；恢复轮把日志时间戳 `341` 误当成 payload 前缀，错报恢复不一致 | 使用带 nonce/操作/长度/状态的结果协议；校验实际数据，拒绝回显、旧结果、截断、重复与污染 |
| 异常路径退出却没有完成恢复观察 | r3/r4 异常后释放串口，后续另开流程才看到 RockOS 或固件 | 测试失败后同一串口所有者继续有界恢复；结果区分测试失败、恢复成功和恢复未知 |
| 用临时脚本绕开正式审计链 | `/tmp` 脚本缺少完整的 state/latch、可重放失败测试和持久化日志机制 | 把必要能力收敛进仓库工具；模拟错误路径通过后再上板 |
| 连续修解析后重试真机 | 本轮出现多次宿主协议/预算错误后仍继续上板，增加了不必要的启动次数 | 下一轮若出现未知协议错误即停止实验，恢复 RockOS；在宿主日志重放修复后再安排新一轮 |
| 把重开串口当成交互验证 | 最后只成功 open，未收到命令响应；SSH UID 是 1000 | 先认证、再用新 nonce 验证 UID 0/boot-id，重开串口后重复；未执行项明确标 pending |

临时恢复轮的日志仍保留为失败证据，不覆盖或改写其 `passed=false`。
最终 r5 日志另有干净的原值、固定模式、恢复值输出及紧随其后的重启命令，
因此可以独立接受该次有界冒烟结果。

## 已有保护与仍待实现的保护

已有仓库能力包括稳定串口/独占锁、禁止用空回车获取 U-Boot 提示、
新固件周期识别、恢复失败 latch、artifact 身份验证和受限 watchdog 启动选项。
Basic 与只读 Probe 的 watchdog 已有真机恢复证据。
这些能力并不意味着所有临时脚本都使用了它们。

仍待落实：framebuffer 固定动作 helper、宿主协议重放、统一剩余时间预算、
所有异常路径的恢复观察、显示专用门禁，以及新的 root/串口重连收据。
不把本页列出的措施描述为已经部署。

独立 watchdog 能降低风险，但无法证明任意总线锁死、复位域故障或供电故障都可恢复。
宿主进程超时、SSH 超时和软件异常处理也不能取消一笔已经卡住的 MMIO 总线访问。
没有远程电源控制时，不能承诺永远不需要人工复位。
高风险新 MMIO/clock/reset/DMA 实验要求现场可复位，且须另行审核准入。

后续执行顺序见 [真机恢复与桌面推进计划](../../superpowers/plans/2026-10-06-megrez-recoverable-desktop.md)。

## 2026-10-06 晚间恢复链尝试（失败且状态未知）

这一轮只允许做一次 RockOS 软件恢复和登录握手，未发送 Asterinas、GPU、GMAC、DWC3 或
framebuffer 命令。宿主脚本在串口首个 `rockos-eswin login:` 提示符处等待超时，未生成
认证收据；随后 20 秒串口被动读取为零字节，等待 30 秒后 ping、SSH 仍不可达。串口监听
进程已退出且 `/dev/ttyUSB0` 无占用。因没有远程电源/复位证据，当前只能标记为
`recovery_state=unknown`，不能推断是 U-Boot、RockOS、Asterinas 或硬件停顿。

这次暴露了两个新的执行约束：

1. “首个串口提示符”本身不能作为恢复边界；启动前必须同时准备串口日志、网络探测和
   有界超时，并在超时后进入只读观察态。
2. SSH `reboot` 的主机端 stdout 不能先做无限阻塞读取；应先关闭/重定向输出，再由串口
   和 SSH 两条独立路径确认新 boot-id。否则宿主脚本可能卡住而无法执行后续恢复观察。

在现场恢复电源或确认 U-Boot/ RockOS 可见之前，禁止重复软件重启、选择启动项或做任何
MMIO 探测。本节只记录观测，不构成内核根因结论。

## 2026-10-06 管理地址核对更正

本页此前的晚间状态判断使用了错误的探测地址 `192.168.137.100`。历史控制链和仓库
配置确认的 RockOS 管理地址是 `10.100.19.200`。对错误地址的 ping/SSH 无响应不能作为
开发板失联证据，也不能触发复位。

使用正确地址的只读复核结果为：3/3 ping 成功，SSH 返回 UID `1000`、新的 boot-id
`c514f2be-5bed-40ea-93fd-f4a4e8887ef3`，并报告系统已运行约 1 小时 47 分钟；串口在
空闲 RockOS 状态下被动读取 8 秒为零字节，这本身是正常的，不能判定死机。因此，晚间
“开发板彻底挂死”的结论撤销，当前状态应记录为 **RockOS 可访问，串口无主动输出**。

后续状态门禁必须从仓库的板端地址配置读取 `10.100.19.200`，同时记录实际探测地址；
任何单一错误地址的超时都不得进入复位或故障结论。只有正确地址的 SSH、正确串口路径
和最新 boot-id 同时失败，才允许进入人工复位流程。

## 2026-10-06 真机 Probe 前的 RockOS 软件重启（未到达 U-Boot）

在使用已通过的 r2 菜单前，先完成了只读产物审计：菜单 SHA-256 为
`81541106e888ebc6faa338ed47d6e45e756689a093707652dd886e829968a3d9`，kernel、Stage1
和 DTB 的 SHA-256 均与 manifest 一致。板端当时由正确地址 `10.100.19.200` 的 SSH
返回 UID 1000 和 boot-id `c514f2be-5bed-40ea-93fd-f4a4e8887ef3`；串口独占检查成功，
但空闲 RockOS 没有主动输出。

随后只发送了一次经过认证的 RockOS `sudo reboot`。串口监听看到 RockOS 进入关机，
在 `systemd-timesyncd`/`plymouth-reboot` 阶段持续输出；但本次临时监听器没有把完整串口
日志持久化，因而不能证明它最终停在关机阶段。历史同类日志实际会继续到
`systemd-shutdown: Rebooting` 并出现新的 OpenSBI/U-Boot。`wait_for_uboot_prompt()` 在看到
U-Boot 倒计时时会发送空格中断自动启动；如果监听器随后退出而正式 cycle 尚未接管，板子
停在 U-Boot 提示符也会表现为网络、SSH 和被动串口均无响应。此后约两分钟内，正确地址
ping、SSH 和串口被动读取均无响应；没有发送 Asterinas、GPU、GMAC、DWC3 或 framebuffer
操作，也没有写存储或环境变量。本次结果应记录为 `recovery_state=unknown`，不能归因于
RockOS 关机服务或内核根因。

新的硬约束：从 RockOS 开始的真机周期不能用临时监听器裸发软件 reboot，再由另一个
进程猜测并接管 U-Boot。必须由同一个独占串口 owner 持有“重启、固件识别、提示符确认、
下一步命令”的整个交接；完整日志必须先落盘，任何交接失败都不得中断自动启动或留下
无人管理的 U-Boot 提示符。独立 watchdog 或现场可用的物理复位仍需作为第二道保障。

## 2026-10-06 状态假设的验证

随后使用正式的 `megrez_menu_board cycle --mode probe --hardware-watchdog` 接管同一串口，
没有重新执行 RockOS 软件重启。该 runner 立即识别到可用的 U-Boot 提示符，完成 watchdog
时钟、复位、组件类型和控制寄存器读回，然后启动只读 Probe；Probe 输出
`ASTERINAS_PROBE_DONE ... status=pass`，并自动回到新的 OpenSBI/U-Boot 周期。

证据 `target/megrez-physical-step1/probe-cycle-safe-20261006-r7/result.json` 报告
`passed=true`、`recovered=true`，总耗时 38.89 秒。这个结果强烈支持上一轮的主要解释：
板子在临时监听器退出后停在 U-Boot 提示符，网络和串口静默是该状态的正常表现，而不是
已经证实的硬件或 Asterinas 内核死机。由于上一轮没有保留完整监听日志，这仍是“最有证据
的解释”，不是对上一轮每个瞬间的直接观测。

在同一已验证的 U-Boot 周期上，随后只执行了一次带 watchdog 的 Basic 周期。Basic shell
返回 nonce 绑定的 `BASIC_OK`，执行一次 `sync; reboot -f` 后观察到新的 OpenSBI/U-Boot。
`target/megrez-physical-step1/basic-cycle-safe-20261006-r8/result.json` 报告
`passed=true`、`recovered=true`，总耗时 41.10 秒。这说明当前菜单、Stage1、内核的最小
启动和软件恢复链均可重复；板子目前再次停在已确认的 U-Boot 提示符，网络静默属于预期状态。

随后只增加 DWC3-0 这一变量，执行一次正式的硬件 watchdog 只读 Probe。结果
`target/megrez-physical-step1/probe-dwc3-0-safe-20261006-r9/result.json` 报告
`passed=true`、`recovered=true`；资源为 `0x50480000/0x10000`、IRQ 85，白名单寄存器
`0x0010=0x0220fe45`、`0xc110=0x00192004`、`0xc120=0x5533330b`，随后看到新的
OpenSBI/U-Boot。Probe 未启动 xHCI、未枚举 USB、未做 DMA；DWC3-1、GMAC 和桌面仍未触碰。

## 2026-10-06 DWC3-1 与 RockOS 管理链

在同一个已确认的 U-Boot 候选上，只执行了一次 DWC3-1 只读 Probe。证据
`target/megrez-physical-step1/probe-dwc3-1-safe-20261006-r10/result.json` 报告
`passed=true`、`recovered=true`；资源为 `0x50490000/0x10000`、IRQ 86，读取白名单
偏移 `0x0010=0x0220fe45`、`0xc110=0x00192004`、`0xc120=0x5533330b`。没有启用
xHCI、枚举设备或 DMA，watchdog 只使用一次，之后观察到新的 OpenSBI/U-Boot。

随后由同一串口 owner 直接选择 RockOS，没有从 RockOS 发软件重启，也没有改写启动项、
环境变量或文件系统。SSH 只读握手返回普通用户 UID 1000、root UID 0，两个 nonce 均绑定
同一个 boot-id `2230297f-5c5f-4405-855f-7ac807527732`。SSH 采集的只读快照确认：
设备型号为 Milk-V Megrez；存在 `card0`/`card0-HDMI-A-1` 和 `fb0=es_drmdrmfb`，模式
为 `2560x1600`；`end1` 为管理地址 `10.100.19.200`，`end0` 为 NO-CARRIER；DWC3-0/1
和 GMAC-0/1 的设备树状态均为 `okay`。完整收据为
`target/megrez-physical-step1/rockos-control-chain-20261006-r11/rockos-control-chain.json`，
只读快照为同目录的 `rockos-readonly.log`。

串口关闭后重新打开的认证收据本轮没有通过：首次被动读取无提示，后续一次状态机错误把
shell 命令回显当成了登录状态，随后又出现一次 `Sorry, try again.`；没有发送 reboot、
MMIO、网络流量或文件系统写入。相关原始日志保留在 r11 目录的四个 `serial-reopen*`
文件中。这个错误说明重开串口必须以“新观察到的提示符位置”为状态起点，不能使用旧日志中
残留的 `login:`/命令回显；在该门禁修复并通过宿主重放前，不继续做 GMAC 探测。

GMAC-0/1 因而延期到新的、明确确认的 U-Boot 周期：GMAC-0 先只读一次，成功后才运行
GMAC-1 一次；两者各自使用独立 watchdog，不做自动重试，也不进入网络流量测试。

## 2026-10-06 GMAC 分阶段 Probe

在 RockOS 上通过一次认证的软件重启进入新的 U-Boot 周期；串口 owner 在整个重启、固件
识别、Probe 和 watchdog 恢复期间保持不变。GMAC-0 只读 Probe 通过，且 watchdog 恢复
到新的 OpenSBI/U-Boot：

- [GMAC-0 收据](../../../target/megrez-physical-step1/gmac-0-safe-20261006-r12/result.json)
- 资源 `0x50400000/0x10000`、IRQ 61；读取白名单偏移为
  `0x0000, 0x00b0, 0x0110, 0x0120, 0x1000, 0x1008`。
- `passed=true`、`recovered=true`，未产生网络流量。

确认 GMAC-0 后，下一周期单独执行 GMAC-1。GMAC-1 没有输出任何
`ASTERINAS_PROBE_READ` 或 `ASTERINAS_PROBE_DONE`，只输出了资源
`0x50410000/0x10000`、IRQ 70、时钟/复位标记以及 PHY/PLL training 到 100%；随后硬件
watchdog 恢复。正式 runner 的收据为 `passed=false`、`recovery_observed=true`，但恢复阶段
自动启动了 RockOS，未在 U-Boot 倒计时处停住。一次只读 SSH 收据确认 RockOS 已恢复、UID
1000、boot-id 为 `2e2df8af-27b0-4b02-ae2c-c11d93f8d756`：

- [GMAC-1 原始收据](../../../target/megrez-physical-step1/gmac-1-safe-20261006-r13/result.json)
- [GMAC-1 恢复收据](../../../target/megrez-physical-step1/gmac-1-safe-20261006-r13/recovery-attestation.json)

GMAC-1 因而记录为 **Probe timeout/fail，但恢复到 RockOS 成功**。当前证据只能定位到
PHY/PLL 初始化之后、第一条白名单寄存器读取之前，不能把根因进一步归因到某一个寄存器。
按约束没有重试 GMAC-1，也没有进入真实网络流量测试。

本轮还发现宿主端输出风险：自定义包裹脚本把完整启动串口日志复制到 stdout，导致终端输出
过大时宿主收据可能延迟或丢失。后续必须使用正式 runner 的 stdout 重定向或只写文件，不能
让日志输出影响恢复观察；这属于宿主执行问题，不改变 GMAC-0/1 的板端结果。

## GMAC-1 只读 Probe 的 QEMU/宿主重放

QEMU 外部 EIC7700 模型在容器 `qemu-eic7700-builder-20261004` 中重复运行，9/9 个 qtest
通过，包括 GMAC identity/MDIO/reset、descriptor loopback、GMAC-1 clock-gated fail-closed
和平台序列。收据保存在
`target/megrez-qemu-hardware-probe-20261006-gmac1-replay/result.json`，没有使用网络后端。

宿主收据校验器新增 `ASTERINAS_PROBE_ATTEMPT`：它在每次 `read_once` 前输出偏移，因而
watchdog 只留下前缀时可以区分卡在映射、某一次 MMIO 读取还是已完成读取后的阶段。对旧的
GMAC-1 真机日志重放结果是 `unknown-before-first-read`，这是因为旧 kernel 没有该标记；用
同一资源/DT 收据和 `offset=0x0000` 的读前标记构造的 QEMU 超时重放结果是
`mmio-read:0x0000`。未知结果保持未知，不用日志顺序猜测 PHY 原因。

当前 Asterinas GMAC Probe 入口位于 `kernel/comps/dwmac/src/lib.rs` 的硬件探测分支，调用
`arch::probe_read_only`，不会进入 `prepare`/`program_platform`；该路径只做严格 DT/资源
校验、`IoMem::acquire` 和固定寄存器 `read_once`，不写时钟、复位、GPIO、RGMII、MDIO，
也不注册 DMA、TX/RX 或网络设备。读前标记与宿主测试已加入。

## 2026-10-06 修正后的 GMAC-1 单次验证

RISC-V 内核重新构建成功，内核 SHA-256 前缀为 `bd9e0bbe3f84`；不可变候选菜单的 SHA-256
为 `6e3ad5a9c0ae0d081e576e17d3164e93ae489b2e87532d02e7e36ea75908099d`。候选通过 manifest、
文件哈希、DTB 检查和 44 个硬件 Probe/菜单单元测试后发布到 RockOS `/boot`，旧菜单和旧
产物未删除。发布阶段曾两次错误地把串口登录提示当成已认证 shell，发布脚本没有执行；
随后在明确看到 `login:` 后完成一次登录并发布，板子安全回到 U-Boot 提示符。

从该已观测的 U-Boot 提示符只执行了一次修正后的 GMAC-1 Probe，收据为
`target/megrez-physical-step1/gmac-1-safe-20261006-r15/result.json`。Asterinas 输出了：

```
ASTERINAS_PROBE_BEGIN device=gmac-1
ASTERINAS_PROBE_RESOURCE device=gmac-1 base=0x50410000 size=0x10000 irq=70
ASTERINAS_PROBE_DT device=gmac-1 clocks=[3, 550, 3, 551, 3, 553] resets=[20, 7, 33554432]
ASTERINAS_PROBE_PHASE device=gmac-1 phase=mmio-map
ASTERINAS_PROBE_PHASE device=gmac-1 phase=mmio-read
ASTERINAS_PROBE_ATTEMPT device=gmac-1 offset=0x0000
```

此后没有 `ASTERINAS_PROBE_READ`、`PASS` 或 `DONE`。硬件 watchdog 复位后自动进入 RockOS；
正式 runner 未在 U-Boot 倒计时窗口捕获提示符，故收据的形式状态为 `passed=false`，但这
不是新的失联：一次不重启的串口只读登录确认 RockOS UID 1000、boot-id
`872ba41f-9a4a-46ae-ad8f-2f13dd8795ce`，详见
`target/megrez-physical-step1/gmac-1-safe-20261006-r15/recovery-attestation-r15.json`。

结合读前标记，超时现在可严格分类为 `mmio-read:0x0000`。它发生在只读路径的第一条
GMAC-1 MMIO 读取上；没有证据表明 Probe 启动 PHY、DMA、TX/RX 或网络流量。此前日志里的
PHY training 属于 watchdog 后 RockOS 启动阶段，不属于 Probe 区间。GMAC-1 仍记录为失败，
不再重试，也没有进行任何网络流量测试；后续应在 QEMU 模型中修复时钟门控/总线读超时语义，
再考虑下一次真机周期。
