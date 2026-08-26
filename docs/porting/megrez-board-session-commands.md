# Megrez 上板命令速查（人工执行，逐步验证）

> 配套：`megrez-board-handoff-checklist.md`（流程骨架）、`megrez-asterinas-boot-guide.md`（完整手册）、
> `megrez_patch_dtb.py`（主机 DTB 修补）、`verify_megrez_sim.sh`（上板前复验）。
> 原则：每步验证后才进下一步；**绝不 `saveenv`**；卡住等 `reboot_after=400` 自动恢复或外部断电。

## 0. 上板前（主机侧，板子不在场也能做）

```bash
# 复验（uboot-sim 容器内一键）——必须 PASS 才上板
docker run --rm -v $PWD:/root/asterinas -w /root/asterinas \
  asterinas-env:uboot-sim bash -c 'export PATH=/usr/local/qemu/bin:$PATH
  tools/riscv/verify_megrez_sim.sh'
# 输出：PASS + 产物身份（kernel/initrd/dtb 的 SHA-256 + CRC32）——记录

# 产物命名（带 commit + run id）
# asterinas-megrez-<commit>-<run-id>.booti
# rv-init-megrez-<commit>-<run-id>.cpio.gz
# megrez-<commit>-<run-id>.dtb（可选：主机修补版）
```

## 1. 串口与板子

```bash
# 串口独占检查（唯一 owner）
fuser /dev/ttyUSB0; lsof /dev/ttyUSB0; ls /tmp/*.lock 2>/dev/null
# 启动串口桥接（独占 + RX 日志 + paced TX，\x1d 退出）
python3 paced_serial_bridge.py /dev/ttyUSB0 /tmp/megrez-session.serial.log
# 上电 → U-Boot 提示符出现 → 记录 U-Boot 版本（2024.01-gdbb5f9e3）
```

## 2. 门禁检查（PRE-BOOTI-GATE 摘要）

- U-Boot `2024.01-gdbb5f9e3`、DRAM 16 GiB、SD=`mmc 1`（SR128）
- 确认可恢复：外部物理 reset 可用

## 3. 进入 RockOS 传输

```text
# U-Boot 中断 autoboot
sysboot mmc 1:1 any 0x88200000 /extlinux/extlinux.conf
# 选 RockOS 6.6.87
```

```bash
# 板端网络（10.100.19.200/21 示例）→ 主机 http 下载产物 → SHA-256 校验 → 安装到 /boot（新文件名）→ sync
# 具体命令见 runbook（board-runbook.md）；凭据不写文档
```

## 4. 重启回 U-Boot → 加载与校验

```text
# 每行命令逐字符节流发送（bridge 已 paced），提交前核对完整回显
ext4load mmc 1:1 0x80200000 /asterinas-megrez-<commit>-<run-id>.booti
printenv filesize
setenv aster_size ${filesize}
crc32 0x80200000 ${aster_size}        # 与主机记录的 CRC32 核对

ext4load mmc 1:1 0xf0000000 /megrez-<commit>-<run-id>.dtb
fdt addr 0xf0000000
fdt resize 0x1000

ext4load mmc 1:1 0x83000000 /rv-init-megrez-<commit>-<run-id>.cpio.gz
setenv initrd_size ${filesize}
```

## 5. DTB 修补（二选一）

**方案 A（推荐，主机修补）**：上板前用 `megrez_patch_dtb.py` 修补 DTB（探测 DWC3 + 加 `/chosen/asterinas,usb-host`）→ 记录修补后 DTB 的 CRC32 → 上板时加载修补版（步骤 4 用修补版）并核对 CRC。

**方案 B（U-Boot 内修补，后备）**：

```text
fdt set /chosen asterinas,usb-host \
  /soc/usb0@50480000/dwc3@50480000 \
  /soc/usb1@50490000/dwc3@50490000
fdt print /chosen asterinas,usb-host
```

该属性是最多两个节点路径组成的 DT string-list。2026-08-26 的真机验证中，
USB0（IRQ 85）连接 Logitech 键盘，USB1（IRQ 86）经 VIA hub 连接光电鼠标；
Asterinas 为两个控制器分别启动 xHCI HID worker。只需要一个控制器时仍可写
单个路径。

### 5.1 current-main HDMI framebuffer 候选

若本轮目标是复用 U-Boot 已初始化的 1920x1080 HDMI scanout，优先使用
`tools/riscv/megrez_board_session.py --firmware-framebuffer`，不要手工改写
环境。该选项只修改本次加载到 RAM 的 DTB，注入的冻结合同为：

- 地址 `0xfd800000`；可见长度 `0x7e9000`；
- width `1920`、height `1080`、stride `7680`；
- format `x8r8g8b8`、status `okay`；
- 运行 `fdt print /framebuffer@fd800000` 后才允许 `booti`。

current-main 目前只把第一个 `console=` 用作 `/dev/console`。要让 Debian
systemd/Xorg 的控制台路径真正落到 HDMI，bootargs 必须以
`console=tty0` 作为第一个 console。串口自动门禁应使用
`--final-profile firmware-framebuffer`，只把内核的
`Registered firmware framebuffer` 当作这一步的 PASS；它不把这个 PASS
外推成桌面已经启动。

## 6. bootargs 与 booti

```text
setenv bootargs "cpu_no_boost_1_6ghz loglevel=info init=/init asterinas.reboot_after=400"
fdt set /chosen bootargs "cpu_no_boost_1_6ghz loglevel=info init=/init asterinas.reboot_after=400"
printenv bootargs
fdt print /chosen
booti 0x80200000 0x83000000:${initrd_size} 0xf0000000
```

## 7. 验证里程碑（每到一个即记录时间戳）

| 里程碑 | 期望输出 |
|---|---|
| 内核进入 | `Enter riscv_boot` |
| 内核 banner | `Presented by the Asterinas developers` |
| 用户态 | `>>> Hello from RISC-V userspace on Asterinas! <<<` |
| **双 xHCI 输入** | 两个 `Starting DWC3 xHCI host`，随后键盘和鼠标分别注册 |
| **桌面输入** | `DEBIAN_DESKTOP_M3_INPUT keyboard=evdev pointer=evdev` |
| 自动恢复（验证安全网） | 若卡住：400 秒后自动重启回 U-Boot `=>` |

## 8. 安全结束

- **不 `saveenv`**
- 退出 bridge（`\x1d`）→ 确认无串口 owner/服务残留
- 归档：serial.raw.log + 记录到 `board-runs/<run-id>/RESULT.md`（SHA-256 全记录）
