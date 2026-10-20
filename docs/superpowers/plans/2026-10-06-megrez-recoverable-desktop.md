# Megrez 可恢复桌面真机推进计划

> **For agentic workers:** Use `superpowers:executing-plans` to execute this plan sequentially.
> 每个真机周期分别检查证据；本计划不授权自动重试失败的硬件启动。

**Goal:** 保留可验证的恢复和 root 管理通道，逐步恢复离线桌面、本地 Firefox，
随后推进原生 DRM 显示与真实 GPU 渲染。

**Architecture:** 复用不可变 kernel/Stage1/DTB 和现有 RockOS 恢复项。
先在宿主日志重放及 QEMU 验证控制流程，再按显示、输入、浏览器、网络、DRM、GPU 的顺序扩大真机能力。
每轮只引入一个变量，测试结果和恢复结果分别记录。

**Tech Stack:** Python 宿主运行器、RISC-V Asterinas、BusyBox/Stage1、Debian Xorg/fbdev、
QEMU、EIC7700 watchdog 与 U-Boot/OpenSBI。

本页是后续路线，所有未勾选项均未实施。
本次只更新文档，不操作开发板，也不建立新工作副本。

## 起点与硬性停止条件

- [x] 保存 [故障与流程错误记录](../../porting/evidence/2026-10-06-megrez-physical-lessons.md)。
- [x] 核对当前 Basic framebuffer 4 KiB 读取、64 字节固定模式/恢复的冻结证据。
- [ ] 补齐最后一次 RockOS 恢复后的 root 串口握手；此前只验证了 SSH UID 1000 与串口 open。

一旦出现无新鲜命令响应、未知 MMIO 边界、panic、预算不足、协议污染或恢复超时，
不继续下一个功能，也不自动重启重试 Asterinas。
同一串口所有者进入有界恢复观察；只在确认的新 U-Boot 周期选择现有 RockOS 恢复项。
若已直接启动 RockOS，验证认证后的命令响应，不强求停留在 U-Boot 提示符。
无法证明恢复时保留失败 latch、停止发送操作命令，明确报告人工复位需求。

## 阶段 0：控制链与实验工具收尾

**涉及文件：** `tools/riscv/megrez_menu_board.py`、`megrez_board_session.py`、
`megrez_safety.py`、`megrez_rockos_attestation.py` 及对应 `tools/riscv/tests/`。
新增 framebuffer helper 与宿主协议时分别放在 `tools/riscv/`，不继续扩散 `/tmp` 真机脚本。

- [x] 增加纯主机侧控制链状态机、严格串口帧解析、输出目录预检和 watchdog 预算契约；
  实现在 `tools/riscv/megrez_control_chain.py`，测试在
  `tools/riscv/tests/test_megrez_control_chain.py`。它只接受带 nonce、UID 0、boot-id
  和 `status=pass` 的单帧，并要求重启后 boot-id 改变、串口重开和 SSH 使用同一新 boot-id。
- [ ] 在宿主重放本次串口日志，确认帧回显、时间戳粘连、截断、重复、旧 nonce、
  新固件周期和直接 RockOS 恢复均不会被当成功。
- [ ] 增加“输出目录已存在不得触发重启”“监听未就绪不得重启”的失败测试。
- [ ] 增加“测试失败仍等待恢复”“迟到用户态命令不得发送到 U-Boot”的状态机测试。
- [ ] 在发送前检查剩余时间：启动、发送、操作、恢复预留与裕量之和必须可容纳。
  校验 watchdog 的实际时钟/超时语义；Basic 的 60 秒软件计时器不是硬件 watchdog 的时长。
- [ ] 用 guest helper 一次完成保存、固定颜色写入、读回、恢复、复核。
  保存的是**同一次启动、写入前读取**的内容；只允许限定的 64 字节区域。
  任一写入或回读错误仍尝试恢复，恢复状态单独报告。
  命令保持短；协议含新 nonce、操作名、长度、状态和实际数据摘要，不能只输出预期摘要。
- [ ] 在项目持久 Docker 容器运行上述宿主测试；helper 的成功、短读/短写和恢复失败路径通过后，
  再用 QEMU framebuffer 验证真实 syscall 路径。
- [ ] 开发板只做 RockOS 新 nonce 的 UID 0/boot-id 验证，关闭并重开稳定串口，再重复命令握手。
  保留同 boot-id 的两份收据；不额外重启只为凑次数。

**准入结果：** 自动化工具能在发生本次全部宿主错误时安全停止并记录恢复，
且板子具有可验证的管理通道。
通过前不启动下一轮 Desktop，也不重做已通过的 framebuffer 实验。

## 阶段 1：离线显示专用 canary

**涉及文件：** `tools/riscv/megrez_desktop_boot.py`、
`tools/riscv/megrez_debug_desktop.py`、`tools/riscv/debian/rootfs/` 中的显示/Stage1 门禁及对应测试。

- [ ] 把 display-only 验收从现有要求 Firefox/USB/网络的桌面门禁中分离。
  最小 volatile DT profile 禁用 GMAC 和 DWC3，保留 UART、存储、firmware framebuffer。
  用实际驱动日志确认禁用生效，不能仅依赖 profile 名称。
- [ ] 在 QEMU 完成 root handoff、root console、Xorg/fbdev、窗口管理器和截图验证。
  收据包含 kernel/Stage1/rootfs/DTB 的摘要和输入设备集。
- [ ] 验证适用于该启动时长的 watchdog 方案：它不能在用户态就绪前过期，
  也不能为桌面启动而直接关闭保护。
  若没有已审核、满足时间预算的方案，本阶段真机启动暂停，先完成宿主/QEMU 工具工作。
- [ ] 真机只启动一次显示专用 canary，不启动 Firefox。
  验证 UID 0、boot-id、Xorg 存活、fb ioctl 的几何/像素布局、一次截图及可区分的画面更新。
- [ ] 最后 `sync; reboot -f`，观察新的固件或 RockOS 边界，再完成 root SSH/串口重连验证。

**准入结果：** 离线桌面启动、软件绘制和管理/恢复链通过。
软件截图和 fb 校验只能证明软件生成的图像及 framebuffer 内存更新；
没有 HDMI 采集设备时，实际显示器 scanout 验收标为未验证。

## 阶段 2：输入与本地 Firefox，分两个周期

**涉及文件：** 现有 USB/evdev 门禁、`tools/riscv/debian/rootfs/physical_graphics_gate.py`、
`browser_web_marionette_gate.py`、`desktop_display_provider.sh` 及对应测试。

- [ ] 输入周期只增加一个已审查的 USB host，验证真实/可核对的输入事件、固定窗口响应和恢复。
  不同时增加另一 USB host、GMAC 或浏览器负载。
- [ ] Firefox 周期使用离线本地页面，限定一次启动和少量固定交互。
  复用现有页面/Marionette 门禁，不新增长时间压力循环。
- [ ] 自动保存进程/线程 CPU、schedstat/等待、内存、page-fault、启动阶段、
  Xorg/Firefox renderer 信息和截图更新时间；只记录内核实际提供的指标，缺失项单独标记。
  以单调时钟标注 root handoff、X ready、首窗口、首内容和操作完成。
- [ ] 按日志先区分调度/等待、文件加载、软件绘制/复制和浏览器自身耗时。
  后续一次只改一个证据支持的瓶颈；只有要宣称性能提升时才做短的同负载前后验证。
- [ ] 每周期都验证 root 控制和同步软件重启，不以看到桌面替代交互/恢复收据。

**准入结果：** 可自动验收的本地 Firefox 使用；本阶段允许软件渲染。
不宣称硬件 GPU 加速，也不以不同页面或启动变体拼接性能倍数。

## 阶段 3：网络单独恢复，不挡住离线桌面

**涉及文件：** `kernel/comps/dwmac/src/arch/riscv.rs`、
`tools/riscv/megrez_gmac_gate.py`、`megrez_hardware_probe.py` 与现有模型/测试。

- [ ] 静态核对 GMAC-1 的 vendor 时钟、复位和平台前置条件，区分正常驱动路径与跳过初始化的 Probe。
  “正常驱动曾启动成功”不资格化“未初始化的 read-only Probe”。
- [x] 在 fake registers/QEMU 检查准确顺序、readback、缺失前置条件和中断/描述符错误路径。
  `target/eic7700-qemu/phase3b-20261006/result.json` 记录 9/9 bounded qtest 通过，
  包括 GMAC-1 clock-gated fail-closed 和 platform sequence；对应日志不包含真实板上
  MMIO 访问。
  QEMU 模型返回错误不能证明物理 MMIO 总线会返回。
- [ ] 新 clock/reset/MMIO 实验必须单独审批实验准入，具备现场复位和独立 watchdog，
  先无 DMA、无 PHY 流量地验证一个边界；不直接重试失败的 GMAC-1 只读访问。
- [ ] 独立网络边界通过后才做一次小的本地 fixture 传输，最后再放开外部网页。

**准入结果：** 网络和恢复有独立证据；失败时离线桌面路线仍可推进。

## 阶段 4：真机 DRM 显示接管

**涉及文件：** `kernel/src/device/` DRM 接口、`kernel/comps/` 显示后端、
硬件访问所需的 `ostd/` 抽象及 `tools/riscv/debian/rootfs/desktop_display_provider.sh`。
实现前先确定最小后端文件划分，并审查对应 hardware/development/security 指南。

- [ ] 第一个后端仅接管已知 firmware framebuffer，保留 fb0 回退。
  先实现用户态实际需要的 DRM/KMS 查询与缓冲区接口，不改变 HDMI 时序、clock/reset 或扫描地址。
- [ ] 用 QEMU 验证 dumb buffer、mmap、stride/格式和提交结果；检查真实像素而非只检查 ioctl 返回 0。
- [ ] 在有恢复保障的单帧真机周期核对 `/dev/dri/card*`、Xorg provider、像素结果和重启。
  此前的 `/dev/dri` 缺失是当前产物事实，不能用旧 QEMU virtio-gpu 结果替代。
- [ ] 原生显示控制器、模式切换、翻页和 vblank 作为后续独立变量逐项验证。

**准入结果：** 真机 DRM 显示路径有证据；DRM/KMS 支持并不等于 GPU 渲染加速。

## 阶段 5：真实 GPU 渲染

- [ ] 先在可认证的 RockOS 下只读核对现有 GPU 用户态驱动、固件、ABI、库依赖和 renderer，
  据此选择可复用的最小路径；先证明用户态与内核接口能够配对，再开发命令提交。
- [ ] 对固件、地址空间/缓冲区、submit、同步及超时回收分别做用户态协议/内核单元验证。
  QEMU virtio-gpu 或寄存器模型只验证相应虚拟设备/协议，不作为 PowerVR 渲染证明。
- [ ] 真机首次只提交一个固定色或三角形离屏任务，核对实际像素与 fence 完成。
  完成可恢复的独立像素测试后再接入桌面及 Firefox。
- [ ] Firefox 资格化同时要求 renderer/驱动身份、实际 GPU submit/fence、像素结果、
  root 控制与恢复，以及同一短交互负载的性能证据。

**准入结果：** 真实 GPU 执行并改善 Firefox 指标。
不以设备节点存在、GL 字符串或桌面能启动单独判定加速成功。

## 每轮记录与交接

所有操作指令仍维护在 `tools/riscv/README.md`；本计划不是可直接照抄的新启动命令。
新 helper/配置在模拟门禁通过后再写入命令页。
每次结果至少记录：

```text
run_id / 新 nonce / 日期 / 源码身份与 dirty 状态
kernel / Stage1 / rootfs / DTB / menu 的不可变摘要
串口稳定路径 / 所有者 / 实际启用设备
最后成功边界 / 第一缺失边界 / 唯一变量
watchdog readback / 时间预算 / 命令与返回状态
测试结果 / 恢复结果 / root 握手 / 串口重连握手
原始日志路径与摘要 / 未验证项 / 是否停止
```

历史已污染的收据保留，复审结果独立写出。
不重写失败收据，不覆盖旧输出目录，不因换 nonce 绕过恢复失败 latch。
所有实现按现有工作目录推进；已有无关修改不合并进本计划提交。
