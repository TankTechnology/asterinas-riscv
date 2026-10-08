# QEMU 显示链验证：U-Boot bochs → simple-framebuffer → Asterinas 控制台

> 日期：2026-08-12。分支：`codex/megrez-usb-keyboard` @ `803786f99`（内核 Image 当日构建）。
> 结论：QEMU virt 上 RISC-V 图形显示链**全线打通**——固件上屏、DT 交接、内核接管渲染均验证通过。这是 AsterNixOS riscv64 跑 X（fbdev）的内核侧前提。

## 验证环境

- QEMU：`qemu-system-riscv64 -machine virt` + `-device bochs-display`（PCI 显示设备）
- U-Boot：`qemu-riscv64_smode_defconfig`（ece349ade），已含 `CONFIG_VIDEO=y`、`CONFIG_VIDEO_BOCHS=y`（1280x1024）、`CONFIG_VIDEO_SIMPLE=y`
- 内核：当前分支 HEAD，含 main 的固件 framebuffer 解析（PR #8）

## 关键事实与坑

1. **U-Boot 视频初始化开箱即用**：加 `-device bochs-display` 后 U-Boot 控制台/logo 直接上屏（screendump 验证）。
2. **`CONFIG_VIDEO_DT_SIMPLEFB` 对 bochs 无效**：该机制由各驱动自行实现（sunxi/meson/bcm2835），bochs 驱动没有，且依赖 `CONFIG_OF_BOARD_SETUP`。开启后无任何效果——死路，勿再走。
3. **可行交接方式 = U-Boot 脚本手动注入 DT 节点**（与真机路线一致：Megrez 上板本来就要 `fdtput` 改 DTB）：
   - 探 BAR：`pci display 0.1.0` → bochs BAR0 = `0x40000000`（16MB VRAM）
   - 注入（在 `fdt addr <dtb>` 和 `fdt resize` 之后、`booti` 之前）：

     ```
     fdt mknode / framebuffer@40000000
     fdt set /framebuffer@40000000 compatible "simple-framebuffer"
     fdt set /framebuffer@40000000 reg <0x0 0x40000000 0x0 0x1000000>
     fdt set /framebuffer@40000000 width <0x500>
     fdt set /framebuffer@40000000 height <0x400>
     fdt set /framebuffer@40000000 stride <0x1400>
     fdt set /framebuffer@40000000 format "x8r8g8b8"
     fdt set /framebuffer@40000000 status "okay"
     ```

4. **抓帧方法（无头环境）**：QEMU 加 `-monitor unix:<sock>,server,nowait`，`echo "screendump <path>.ppm" | socat - UNIX-CONNECT:<sock>`，与 `-display none` 不冲突。

## 验证结果

- 串口日志：`framebuffer: Registered firmware framebuffer: base=0x40000000, size=0x1000000, resolution=1280x1024, stride=5120, format=BgrReserved`
- 抓帧对比：U-Boot 阶段屏幕显示固件控制台；内核阶段屏幕被清黑、顶部出现内核渲染的 INFO 日志行——**内核 framebuffer 控制台已接管显示**。
- 内核启动到用户态 marker（`Hello from RISC-V userspace`）全程正常，无 panic。

## 对后续步骤的意义

- AsterNixOS riscv64 的 X 桌面走 `xf86-video-fbdev`（`/dev/fb0`），内核侧前提已满足。
- 上板（Megrez）时固件若已交接 simple-framebuffer 则无需注入；否则用同样的 DTB 修补手法。
