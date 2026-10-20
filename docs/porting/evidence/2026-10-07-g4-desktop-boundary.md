# G4 网络与桌面前置边界（2026-10-07）

本记录对应“一、二”收尾：修正 GMAC 网络 gate 的安全编排，并确认桌面真机
启动前的根文件系统和 QEMU framebuffer 前置条件。DRM/GPU 不在本记录范围内。

## 已完成

- `tools/riscv/megrez_gmac_gate.py` 将物理地址冲突检查放在打开板卡并确认新的
  U-Boot/OpenSBI 周期之后。这样 RockOS 正常在线时不会被误认为是一次可启动的
  Asterinas 网络周期；如果没有新 firmware epoch，gate 在加载 Asterinas 前停止。
- 33 个 GMAC gate 单元测试通过；连同 rootfs 合约测试共 226 个通过。
- 真机单次尝试
  `target/megrez-physical-step1/gmac-network-cycle-20261007-r6-direct/result.json`
  以 `fresh U-Boot epoch is missing OpenSBI` 停止，fixture `request_count=0`，
  没有启动 Asterinas、没有发送网络包，也没有重试。这是安全的编排前置失败，
  不是 GMAC-1 寄存器故障。
- RockOS 仍可通过 SSH 读取 UID、boot-id 和分区信息；当前根分区标签为
  `ASTER_BROWSERWEB`，因此它不是 desktop-m4 根文件系统。没有覆盖真机分区。

## QEMU 桌面边界

已构建签名的 `desktop-m4` rootfs：
`target/debian-riscv/desktop-m4-explicit2/rootfs`。
使用当前真机候选同源的 `target/osdk/aster-kernel-osdk-bin.Image` 运行一次有界
QEMU gate 后，内核已注册 firmware framebuffer：
`base=0x40000000, size=0x1000000, resolution=1280x1024, stride=5120,
format=BgrReserved`。systemd、udev、logind 和 `asterinas-desktop-m4.service`
均启动，但没有产生 M4 session/Xorg 标记；可写 root 镜像中也没有
`home/asterinas/desktop-m4-session.log`，说明服务尚未越过
`ExecStartPre=desktop-m4-device-access` 的设备节点等待。此前
使用旧的 `target/virtio-network-init-osdk/aster-kernel-osdk-bin.Image` 的结果
也没有形成通过证据。问题已收敛到桌面服务边界，而不是 framebuffer 注册；
它没有触发 DRM、GPU、PHY、DMA 或真实网络流量。

## 晋级条件

1. 在 QEMU 使用包含 framebuffer 组件的内核和经过验证的 `simple-framebuffer`
   DTB，取得完整 M4 marker 和 framebuffer 校验和。
2. 保留真机当前 `ASTER_BROWSERWEB` 恢复链，准备独立、可回滚的 desktop-m4
   安装介质；安装前不得修改持久 U-Boot 项。
3. 真机只运行一次带 watchdog 的桌面启动，完成新 boot-id、串口重开、SSH UID
   和 RockOS 恢复验证。任何 framebuffer/串口超时都停止，不自动重试。
