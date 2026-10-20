# Megrez offline display canary contract

日期：2026-10-06（Asia/Shanghai）。

本轮把离线显示验证收敛为一个可自动判定的协议。真机仍保持在 RockOS，尚未使用这枚新 rootfs 启动；因此本文记录的是实现和宿主/QEMU 前置验证，不宣称真机显示 canary 已通过。

显示 profile 的 U-Boot 临时设备树会同时禁用两个 GMAC 和两个 EIC7700 USB wrapper，并移除 `chosen/asterinas,usb-host`。启动参数关闭网络、USB 输入和浏览器，只保留 UART、MMC、firmware framebuffer 与 Xorg fbdev。启动前仍要求新 OpenSBI/U-Boot 周期和独占串口。

`desktop_m4_framebuffer_probe.py` 在有界超时内完成以下动作：

1. 通过 `FBIOGET_VSCREENINFO` 和 `FBIOGET_FSCREENINFO` 校验 1920×1080、stride 7680、32-bit `BgrReserved` 布局；
2. 读取前 4096 字节并输出 SHA-256；
3. 读取完整 scanout，生成 `/run/asterinas-desktop-m4-framebuffer.png`，输出尺寸、大小和 SHA-256。

宿主 gate 要求这些记录位于 shell 与显示 client 记录之间，并拒绝缺失、重复、乱序或哈希格式错误。PNG 和 framebuffer 读取分别受 64 MiB、2 MiB 与 30 秒 probe 上限约束。

本地实现收据：

- `tools/riscv/debian/rootfs/desktop_m4_framebuffer_probe.py`；
- `tools/riscv/debian/rootfs/display_m4_dev_overlay.json`；
- `target/dev-overlays/display-m4-v2/rootfs/rootfs-manifest.json`，派生 rootfs SHA-256 为 `2dd4ed85674b6813fe0467485405cbafbe14ada1135bbc1bd403c230281b980e`。

宿主单元测试覆盖布局校验、PNG 有界性、动态 framebuffer 记录顺序和显示 gate。物理实验仍需在下一次可恢复的 Asterinas Basic/显示启动中完成；在部署 rootfs 前不会向板子发送新的启动命令。
