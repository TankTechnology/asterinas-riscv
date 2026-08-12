# 在 QEMU 上运行 Asterinas RISC-V 桌面（framebuffer 显示链）

> 目标：在 QEMU virt 上通过 U-Boot 启动 Asterinas RISC-V 内核，让固件 framebuffer
> 通过 `simple-framebuffer` DT 节点交接给内核，最终在 QEMU 显示窗口中看到内核
> VT 控制台的渲染画面（Asterinas 当前的内核侧桌面 = VT 文本控制台；`/dev/fb0`
> 已就绪，可在此基础上跑 Xorg fbdev 桌面）。

## 显示链（已全线验证）

```
QEMU bochs-display (PCI, BAR0=0x40000000, 16MiB VRAM, 1280x1024)
  → U-Boot (CONFIG_VIDEO_BOCHS=y) 初始化显示器
  → U-Boot 在 booti 前向 DTB 注入 simple-framebuffer 节点
  → Asterinas 解析 DTB 节点（ostd/src/arch/riscv/boot/simple_framebuffer.rs）
  → framebuffer 组件登记（kernel/comps/framebuffer）
  → VT 控制台接管渲染（kernel/src/device/tty/vt）
```

## 前置条件

- 本地构建环境（见记忆 megrez-preflight-local-setup）：`riscv64-linux-gnu-gcc`、
  `qemu-system-riscv64`、`VDSO_LIBRARY_DIR`、`/usr/local/bin/nix-build` stub、
  `rust-objcopy`（符号链接到 `~/.local/bin`）。
- QEMU ≥ 11，带 `gtk`/`sdl` display backend。

## 构建步骤

### 1. 构建 Sv39 内核（关键！）

generic-sv39 profile 的 QEMU CPU 禁用 Sv48，**内核必须以 Sv39 模式编译**，
否则线性映射基址（0xffff800000000000）在 Sv39 下是非规范地址，启动即页错误：

```bash
VDSO_LIBRARY_DIR=$HOME/.local/share/linux_vdso \
  make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode
```

### 2. 构建真实 initramfs

`make kernel` 产生的 initramfs 是 nix-build stub 的空壳，不能引导到用户态。
用仓库工具生成带 `/init` 的真实 initramfs（启动后会打印
`>>> Hello from RISC-V userspace on Asterinas! <<<`）：

```bash
python3 tools/riscv/make_qemu_uboot_initramfs.py target/qemu-uboot/initramfs.cpio.gz
```

### 3. 构建 U-Boot 并生成 boot 磁盘

```bash
ASTERINAS_RISCV_BOOTI=$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image \
ASTERINAS_INITRAMFS=$PWD/target/qemu-uboot/initramfs.cpio.gz \
QEMU_UBOOT_PROFILE=generic-sv39 \
QEMU_UBOOT_OUT_DIR=$PWD/target/qemu-uboot/current \
QEMU_UBOOT_BUILD_DIR=$PWD/target/qemu-uboot/cache/u-boot-build \
  tools/riscv/prepare_qemu_uboot_booti.sh prepare
```

脚本会 clone 并构建固定 commit（`ece349ade`）的 U-Boot。该 commit 的
`qemu-riscv64_smode_defconfig` 已含 `CONFIG_VIDEO=y`、`CONFIG_VIDEO_BOCHS=y`
（1280x1024）、`CONFIG_VIDEO_SIMPLE=y`，无需额外配置。

## 运行

```bash
# 无头模式：启动后抓帧保存到 /tmp/asterinas-desktop.ppm
python3 tools/riscv/qemu_desktop_boot.py

# 窗口模式：在显示器上弹出窗口，直接看到桌面画面
python3 tools/riscv/qemu_desktop_boot.py --display-gtk
```

脚本驱动 U-Boot 依次执行：

1. 从 boot 磁盘加载内核 Image、DTB、initramfs；
2. `pci display 0.1.0` 确认 bochs BAR0 = 0x40000000；
3. `fdt resize` 后注入 `framebuffer@40000000` 节点
   （`compatible="simple-framebuffer"`、`reg=<0x0 0x40000000 0x0 0x1000000>`、
   `width=0x500`、`height=0x400`、`stride=0x1400`、`format="x8r8g8b8"`）；
4. `setenv initrd_size ${filesize}`（漏掉这一步内核报 `no initramfs found`）；
5. `booti`。

## 验证信号

- 串口日志出现：
  `framebuffer: Registered firmware framebuffer: base=0x40000000, size=0x1000000, resolution=1280x1024, stride=5120, format=BgrReserved`
- 串口日志出现：`>>> Hello from RISC-V userspace on Asterinas! <<<`
- 串口日志出现：`Virtual terminal keyboard handler connected to device: QEMU Virtio Keyboard`
- 抓帧非纯黑（有白字 + ANSI 彩色日志像素）。

## 踩过的坑

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| 启动页错误 `TVAL ffff8000fde8d000` | 内核按 Sv48 编译但 CPU 只支持 Sv39，线性基址非规范 | `FEATURES=riscv_sv39_mode` 重建 |
| `WARN: framebuffer: Framebuffer not found` | U-Boot 未注入 simple-framebuffer 节点（`fdt mknode` 未执行成功） | 检查 `fdt print /framebuffer@40000000` |
| panic `no initramfs found` | booti 缺 `setenv initrd_size ${filesize}` | 加入该步骤 |
| 抓帧全黑 | VT 尚未接管 framebuffer（内核早于 2.4s panic，或截图时机太早） | 先确认内核启动到用户态再抓帧 |
| QEMU 串口输出为空（Python PIPE） | QEMU `-serial stdio` 对管道输出少于 4096 字节会阻塞读 | 用非阻塞读（`os.set_blocking` + 分块 `os.read`） |

## 下一步（可选）

- **X 桌面**：AsterNixOS riscv64 走 `xf86-video-fbdev` + `/dev/fb0`（相关 NixOS
  配置在 `codex/megrez-usb-keyboard` 分支的 `distro/etc_nixos/configuration-xmin.nix`）。
  内核侧前提（`/dev/fb0`）已满足。
- **鼠标指针**：QEMU 加 `-device virtio-tablet-device`，内核 virtio-input 已支持。
- **真实板卡（Milk-V Megrez）**：固件若已交接 simple-framebuffer 则无需注入；
  否则用同样的 DTB 修补手法（`fdtput`）。
