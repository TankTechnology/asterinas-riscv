# Megrez 真机运行检查清单（板外保持，一次成功）

> 用途：板子不在手边时维护这份清单；上板时按顺序执行，尽量减少现场试错。
> 绑定文档：`megrez-asterinas-boot-guide.md`（完整手册）、`docs/porting/evidence/2026-07-20-megrez-pid1-recovery.md`（恢复证据）。

## 1. 资产地图（全部就位，勿删）

| 资产 | 位置 | 说明 |
|---|---|---|
| 串口桥接 | `.local-workspace/evidence/board-runs/megrez-3ef99e6bd153-20260719T173554Z/paced_serial_bridge.py` | 独占串口、115200 8N1、排他锁、RX 持久日志、paced TX（防 U-Boot 丢字符）、`\x1d` 退出 |
| 门禁检查 | 同上目录 `PRE-BOOTI-GATE.md` | U-Boot 环境/DRAM/SD/DTB 审计清单 |
| 运行手册 | `.local-workspace/evidence/raw-logs/porting/logs/megrez-v4-fbc9362253c0-20260712T152014Z/board-runbook.md` | 冻结产物的完整 runbook（绑定 hash） |
| 启动手册 | `docs/porting/megrez-asterinas-boot-guide.md` | 1624 行工程复盘（连接/传输/内存门禁） |
| 证据 | `.local-workspace/evidence/board-runs/megrez-3ef99e6bd153-20260719T173554Z/RESULT.md` | 最近一次成功运行（到 PID 1） |
| 产物 | `target/qemu-uboot/inputs-final-sv39-593d5bb19/asterinas.booti` + `initramfs.cpio.gz` | 7/17 构建（旧）；新代码需重新构建 |

## 2. 防卡死/恢复机制（上板前必须确认）

1. **软件定时重启（已验证）**：内核参数 `asterinas.reboot_after=<秒>`——**本分支已移植**（`kernel/src/boot_reboot.rs`）——QEMU 实测：20 秒定时 → SBI ColdReboot 重启成功；panic 场景 → 立即重启成功（`kernel/src/thread/oops.rs` + emergency_restart → SBI ColdReboot）。
2. **软件 panic 重启（已验证）**：armed 状态下 panic → `panic::abort()` → SBI ColdReboot——无需外部干预。
3. **无物理重启时的策略**：`reboot_after` 是唯一自动恢复手段——**上板 bootargs 必须带**（建议 `asterinas.reboot_after=120`，短于人工观察周期）。
4. **安全结束状态**：不 `saveenv`、不留串口 owner/bridge/传输服务、退出后板子回到 U-Boot `=>`。
5. 卡死后不慌：等 `reboot_after` 到期自动恢复；U-Boot 阶段卡住只能等 U-Boot 自身超时（WDT0 未验证——尽量避免 U-Boot 交互失误，命令逐字符节流 + 核对回显）。

## 3. 一次成功流程（顺序执行）

```text
[0] 检查串口独占：fuser/lsof + 锁文件；只有一个 owner（8.1）
[1] 构建（若换代码）：guide 7.1 → QEMU 门禁 7.3 → 产物身份 7.4（SHA-256/CRC32 记录）
[2] 上电进 U-Boot：确认 U-Boot 2024.01-gdbb5f9e3、DRAM 16GiB、SD=mmc 1（PRE-BOOTI-GATE）
[3] 先进入可恢复的 RockOS：sysboot mmc 1:1 any 0x88200000 /extlinux/extlinux.conf（8.2）
[4] 传输：全新文件名（asterinas-megrez-<commit>-<run-id>.booti）→ /boot → 板端 SHA-256 校验 → sync（8.3，不覆盖旧镜像）
[5] 重启回 U-Boot → 内存门禁（guide 9）：Image@0x80200000、DTB@0xf0000000、initramfs@0x83000000，CRC 全部核对
[6] booti：候选 booti → 观察 "Enter riscv_boot" → OSTD → PID 1 → 期望串口出现 hello
[7] 记录：RESULT.md + serial.raw.log 归档到 board-runs/<run-id>/
```

**U-Boot 命令必须逐字符节流发送**（bridge 已实现 paced TX），提交前核对完整回显。

## 4. 状态与下一步（更新至 2026-08-26）

**已解决**：
- **UART 可见性** ✅：dw-apb-uart 驱动（TX/RX/中断/Busy Detect）已实现并在 7/21 真机验证（`docs/porting/evidence/2026-07-21-megrez-dw-apb-uart-rx.md`）——下次上板串口应能看到 PID 1 hello。
- **双 xHCI 键鼠** ✅：USB0（`0x5048_0000`/IRQ 85）注册 Logitech
  boot keyboard；USB1（`0x5049_0000`/IRQ 86）经 VIA hub 注册 boot
  mouse。两个独立中断 worker 均启动，Debian/Xorg 报告 evdev keyboard 和
  pointer。证据见 `docs/porting/evidence/2026-08-26-megrez-dual-xhci-desktop.md`。
- **基础 Debian 桌面** ✅：真机进入 systemd graphical target，非 root
  `asterinas` 会话启动 Xorg fbdev、Matchbox 和 xterm；HDMI 已由操作员目视确认。

**下一步**：
1. 在 HDMI 上人工确认鼠标移动、左键和键盘输入的实际桌面交互；当前自动证据已到设备注册和 Xorg evdev 选择。
2. 把当前基础桌面会话从带 `asterinas.reboot_after=600` 的有界验证启动，升级为可长期使用的启动配置。
3. 在不依赖 DRM 加速的前提下加入 PCManFM 和 NetSurf；网络与硬件加速仍是后续独立里程碑。

## 5. 板子信息速查

- 串口：FTDI FT232R `AL02XYO2`，/dev/ttyUSB0，115200 8N1
- U-Boot：`2024.01-gdbb5f9e3`；DRAM `0x80000000..0x47fffffff`（16 GiB）
- SD：`mmc 1`（SR128, 119.1 GiB）
- RockOS：6.6.87（登录凭据不写入文档）
