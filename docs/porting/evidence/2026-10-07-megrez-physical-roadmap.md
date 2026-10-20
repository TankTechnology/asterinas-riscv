# Megrez 真机推进路线（2026-10-07）

这份路线以“始终可恢复”为最高优先级。每个阶段只增加一种硬件行为；
阶段完成后保存串口日志、启动参数、artifact/menu 哈希、boot-id 和恢复结果，
再进入下一阶段。QEMU 可以重复运行，真机默认每阶段只运行一次；发生首个
超时或缺失终止标记时立即停止，不自动重试。

## 当前已完成

- RockOS 管理链可用：稳定 FTDI 串口、SSH、认证后的 UID 0、boot-id。
- U-Boot 菜单候选可复现，启动后有硬件 watchdog。
- QEMU EIC7700 GMAC 门控模型和 15 项 qtest 全部通过。
- 真机 GMAC-1 只读 Probe 已通过：平台初始化后六个固定寄存器均可读取，
  没有 PHY、MDIO、DMA、IRQ、TX/RX 或网络副作用，随后已恢复 RockOS。

## 阶段 0：管理链基线

1. 只从已知 U-Boot 菜单选择 RockOS，不修改持久化启动项。
2. 由单一串口 owner 采集新的 nonce、boot-id 和启动日志。
3. 重新打开串口并通过 SSH 验证；用认证 sudo 做一次 UID 0 检查。
4. 若串口、SSH 或 boot-id 不一致，停止，不启动 Asterinas。

## 阶段 1：Asterinas 最小启动

只启用 UART、timer、MMC、Basic 和 watchdog，不启用 GMAC、DWC3、桌面或
Firefox。验证启动标记、串口响应和 watchdog/恢复链。确认能回到 U-Boot 和
RockOS 后才允许外设 Probe。

## 阶段 2：只读外设资源确认

在 Asterinas Basic 中只读取设备树、时钟、复位、IRQ、DRM/fb0、USB 和网络
资源描述，不写寄存器、不枚举设备、不启动 DMA。记录资源与 RockOS DTB 的
对应关系；任何资源不一致都停在 QEMU 修正。

## 阶段 3：DWC3 只读 Probe

分别对 DWC3-0、DWC3-1 做单次 Probe：只读固定身份/状态寄存器，不启用 xHCI、
不枚举 USB 设备、不做 DMA。成功或超时后都等待 watchdog 恢复到 RockOS；
超时不重试。

## 阶段 4：GMAC 只读 Probe

按 GMAC-0、GMAC-1 分开运行。Probe 必须先调用选定端口的
`program_platform()`，只配置 GPIO、RGMII、HSP、时钟和该端口 reset，然后
读取六个固定寄存器。不得初始化 PHY/MDIO、DMA、IRQ、队列、TX/RX 或网络设备。

GMAC-1 的修正版首轮已经通过；后续若需要复验，必须重新通过阶段 0，且仍然
一次只选 `gmac-1`。若第一条 MMIO 读取没有返回，立即停止并保留证据。

## 阶段 5：PHY 与网络数据路径（尚未开始）

只有阶段 4 通过后，才可单独运行 PHY 诊断：允许平台 glue 和有界 Clause-22
读取，不注册网络设备。PHY 通过后，再单独启动一次正常 GMAC 注册，只验证
IRQ、DMA ring 创建和退出，不发送外部流量。最后才做受控 loopback 或单包测试；
禁止直接进入长时间 iperf、桌面网络或浏览器流量。

## 阶段 6：显示链路（尚未开始）

1. 只读确认 `/dev/fb0`、分辨率、stride、像素格式和前 4 KiB 校验和。
2. 只写 framebuffer 左上角小区域，写固定颜色后恢复原内容。
3. 验证系统响应、软件重启和 watchdog 恢复。
4. 再逐步启用 DRM/KMS 原生翻页；每次只改变一个显示环节。

没有采集设备时，画面“看起来正常”不能作为自动验证；必须依赖内核日志、
framebuffer 校验、页翻转完成和恢复链证据。

## 阶段 7：桌面与 Firefox（尚未开始）

先验证桌面 shell、文件管理器和基础 Debian 软件，再验证 Firefox 软件渲染。
只有真实 DRM/KMS、稳定 framebuffer 和可测的提交/完成事件都通过后，才打开
Firefox GPU 加速。GPU 加速的真机验证应记录渲染后端、帧提交延迟、卡顿/恢复
计数和退出后的恢复状态；任何 GPU hang 都必须能由 watchdog 回到 RockOS。

## 统一停止规则

- 串口出现多个 owner、SSH/boot-id 认证失败，停止所有真机操作。
- U-Boot 菜单、Asterinas 启动标记或 watchdog 恢复证据缺失，停止。
- 首个 MMIO 读取超时、异常 reset 位变化、DMA/IRQ/网络副作用，停止且不重试。
- 不通过真机结果推断 QEMU 或桌面性能；每个阶段单独记录证据。

真机当前应保持在 RockOS。下一次真机操作从阶段 0 基线开始，优先推进尚未
完成的 DWC3/GMAC 分阶段只读验证，不直接进入 PHY、网络流量或 GPU 加速。
