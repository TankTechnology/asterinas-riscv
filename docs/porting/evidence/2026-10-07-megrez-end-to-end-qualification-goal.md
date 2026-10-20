# Megrez 真机端到端资格目标（2026-10-07）

## 总目标

在不丢失串口/SSH/U-Boot 恢复能力的前提下，让当前 Asterinas 候选在
Megrez 真机上形成一条可重复的日常使用链：

```text
RockOS/U-Boot 管理链
  -> Asterinas Basic 可恢复启动
  -> USB/GMAC/显示资源只读确认
  -> GMAC PHY、DMA/IRQ 和最小网络事务
  -> fb0/DRM/KMS 显示链
  -> Debian 桌面和基础软件
  -> Firefox 软件渲染
  -> 在真实 DRM/GPU 能力确认后再启用 GPU 加速
```

最终资格不是“某条命令执行过”，而是每一层都有自动化验收标记、资源和
错误计数、启动身份以及新的 U-Boot/RockOS 恢复证据。

## 阶段与验收标准

### G0：管理链和恢复底线

- 单一稳定 FTDI 串口 owner。
- 每次启动取得新的 nonce、boot-id 和 UID 0 认证结果。
- 不修改持久化 U-Boot 项；软件重启、watchdog、U-Boot 和 RockOS 恢复路径
  都可观察。

**状态：通过。** 当前 RockOS 可通过 SSH 和认证 sudo 控制。

### G1：最小 Asterinas 启动

- 只启用 UART、timer、MMC、Basic 和 watchdog。
- 记录启动标记、串口响应和恢复时间。

**状态：通过。** Basic 有多次成功且可恢复记录。

### G2：只读资源和安全外设

- 只读设备树、时钟、复位、IRQ、USB、GMAC、DRM/fb0 资源。
- DWC3-0、DWC3-1 分别只读 Probe；不启用 xHCI、枚举或 DMA。

**状态：通过。** 两个 DWC3 Probe 均有 watchdog 恢复证据。

### G3：GMAC 平台和 PHY

- GMAC-0、GMAC-1 只读 Probe 分开执行。
- Probe 先完成 GPIO/RGMII/HSP/时钟/端口 reset，再读取六个固定寄存器。
- 不启动 PHY/MDIO/DMA/IRQ/TX/RX。
- PHY-only 诊断只允许平台 glue 和有界 Clause-22 读取。

**状态：通过。** GMAC-1 修正版 Probe 和 PHY-only 真机周期均通过并恢复。

### G4：GMAC 正常注册和最小网络

- 正常注册只验证 `GMAC_DMA_CONTRACT`、`GMAC_READY`、DMA ring、IRQ/轮询线程。
- 记录 `tx_errors`、`rx_errors`、RX buffer 和 MTL 丢包计数。
- 通过后只做一次固定 fixture 单包/短事务；禁止长时间压测。

**状态：部分通过。** 当前候选正常注册和 QEMU TCP/UDP 已通过。gate 已将
板端地址冲突预检移动到确认新 U-Boot/OpenSBI 周期之后，避免仍在 RockOS 时
误判；修正后的单次真机尝试在缺少新 OpenSBI epoch 时于加载 Asterinas 前停止，
fixture 请求数为 0，因此没有把编排失败误记成 GMAC 失败。证据见
`gmac-network-cycle-20261007-r6-direct/result.json`。下一次真机尝试仍须先
满足新 U-Boot epoch，且只能单次运行。

### G5：显示链

- 只读确认 `/dev/fb0`、分辨率、stride、像素格式和校验和。
- 单帧小区域写入并恢复原内容。
- 再验证 DRM/KMS 原生翻页、提交完成和恢复。

**状态：已有历史证据，当前候选待复核。** 旧候选有 fb0 读写记录；不能直接
将其作为当前内核的 DRM 资格结论。

### G6：桌面和基础 Debian 软件

- 桌面 shell、文件管理器、终端、输入设备和基础软件逐项启动。
- 每项记录启动时延、退出状态、内存/线程异常和恢复状态。

**状态：当前候选在 QEMU 已定位前置问题，真机待复核。** 正确的
`desktop-m4` rootfs 已在 `target/debian-riscv/desktop-m4-explicit2/rootfs`
构建；使用与真机候选相同的 `target/osdk/aster-kernel-osdk-bin.Image` 时，
QEMU 能启动 systemd 并进入桌面服务，内核也已注册 firmware framebuffer，
但没有进入 M4 session/Xorg 标记；可写 root 镜像没有
`home/asterinas/desktop-m4-session.log`，说明服务尚未越过
`ExecStartPre=desktop-m4-device-access` 的设备节点等待。较早的 QEMU
尝试使用旧的 `virtio-network-init` 内核，同样不能作为桌面资格证据。当前
问题已从 framebuffer 注册收敛到桌面服务边界，尚未触及 DRM/GPU；真机仍不能
直接覆盖现有根文件系统。

### G7：Firefox 软件渲染和性能基线

- 先固定软件渲染后端，运行现成的页面/启动/Marionette 基准。
- 自动采集启动阶段、CPU、页错误、内存、帧提交和卡顿日志。
- 只在基线稳定后比较优化结果。

**状态：已有 QEMU 和旧真机资料，当前候选待复核。**

### G8：真实 DRM/GPU 加速

- 先确认 `/dev/dri`、KMS connector/plane、buffer 提交和 fence 完成。
- 只有真实硬件路径可读、可恢复、可计数时才打开 Firefox GPU 加速。
- GPU hang 必须能回到 U-Boot/RockOS；没有 HDMI 采集设备时，以内核和提交
  计数作为自动验收，不能仅凭肉眼声称通过。

**状态：未完成。** 当前系统尚未形成已验证的真机 DRM/GPU 加速路径。

## 自主推进方式

我会按 G0→G8 顺序自主完成代码、QEMU、真机、日志和结果归档。每个阶段结束
时只汇报一次阶段结论和证据链接；中间的小步调试不逐条请求审阅。阶段只能在
以下条件同时满足时晋级：

1. QEMU 或静态合约先通过；
2. 真机一次性达到阶段终止标记；
3. 无越权硬件副作用；
4. 新 U-Boot/RockOS 恢复周期和 UID 0/boot-id 验证通过。

## 统一停止规则

- 串口 owner、SSH、boot-id 或 U-Boot 观察失败：停止真机操作。
- 首个 MMIO/MDIO 读取超时：立即停止，不重试。
- 出现未知 reset 位变化、DMA/IRQ 异常、错误计数增长或网络副作用：停止并
  回到 QEMU 分析。
- 真机保持在 RockOS 才算阶段结束；不能留下 Asterinas、U-Boot 或未知状态。
- 所有结果保存 artifact/menu/kernel/DTB 哈希、串口摘要、计数器和恢复证据；
  不保存凭据。

## 当前下一阶段

先在满足新 U-Boot/OpenSBI 前置条件时完成一次最小真机 fixture 事务；同时在
QEMU 修正 framebuffer 内核/设备组合并取得 M4 证据。只有正确的 `desktop-m4`
rootfs 通过 QEMU 后，才安排一次带 watchdog 的真机桌面启动；安装前必须保留
当前 `ASTER_BROWSERWEB` RockOS 恢复路径。G4 未完成前不做 Firefox 网络负载，
G8 DRM/GPU 继续冻结。
