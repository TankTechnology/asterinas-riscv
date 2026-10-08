# Megrez 上板就绪报告（模拟验证矩阵）

> 日期：2026-08-10。分支：`codex/megrez-usb-keyboard`。
> 目的：上板前在模拟环境把能验证的全部验证到，尽量一次成功、不卡死板子。

## 验证矩阵

| # | 验证项 | 结果 | 证据 |
|---|---|---|---|
| 1 | **Megrez Sv48 契约模拟**（U-Boot booti → OpenSBI → 内核 → 用户态） | ✅ PASS（`marker_seen=yes`，`>>> Hello from RISC-V userspace` 输出） | `target/qemu-uboot/current-megrez/result.json`（classification: PASS）+ serial.log |
| 2 | **键盘链路回归**（QEMU virt + usb-kbd + 最新构建） | ✅ 回显 `a`、`b`，零 panic | QEMU monitor sendkey 实验 |
| 3 | **DWC3 选择失败安全**（DTB `asterinas,usb-host` 指向无效节点） | ✅ `WARN: failed to resolve USB host` + 跳过 + PCI 路径继续 + 零 panic | QEMU 实验日志 |
| 4 | **产物身份**（当前分支 booti Image） | ✅ SHA-256 `a53044a0...`（12:47 构建，与 qemu_elf 同步） | `target/qemu-uboot/current-megrez/result.json` artifacts |
| 5 | **内核 ktest**（aster-uart 全量） | ✅ 80/80 通过（含 dw-apb 配置校验） | qemu-serial.log（12:02 运行） |
| 6 | generic-sv39 profile | ⚠️ 失败（Load page fault @ `ffff8000...`）——**Sv48 内核布局**，sv39 profile 不适用；**Megrez 是 Sv48 目标且 PASS**（预期，非回归） | current/result.json（已废弃目录） |

## 模拟边界（诚实声明）

- QEMU 模拟的是 **Megrez 启动契约**（CPU 4 hart / Sv48 / Svade / U-Boot booti / 用户态里程碑），**不模拟**板载时钟、复位、缓存控制器、DWC3 硬件本身（README 明确）。
- **DWC3 硬件交互只能在真机验证**——但失败路径已验证安全（选择失败 → warn + 跳过 + 系统继续）。
- **安全网**：内核参数 `asterinas.reboot_after=400`（400 秒自动重启回 U-Boot）+ 外部物理 reset（操作员确认）——即使真机 DWC3 有问题，板子也会自动恢复，**不会卡死**。

## 上板前的剩余步骤（必做）

1. **DTB 修补**：真机 RockOS DTB 无 `/chosen/asterinas,usb-host`——用 `fdtput` 添加，指向 Megrez DWC3 节点（路径从板端 DTB audit 确认；MMIO `0x5048_0000`/`0x5049_0000`）——更新 DTB CRC 门禁。
2. **构建基线**：上板当天用当前分支重新构建（Image + initramfs），跑第 1 项模拟确认后冻结产物（记录 SHA-256/CRC32）。
3. **按 checklist 第 3 节执行**（串口独占 → RockOS → 传输 → 内存门禁 → booti → 验证）。

## 结论

模拟边界内**全部通过**：Megrez Sv48 启动链 + 键盘链路 + 失败路径安全。真机 DWC3 硬件交互是唯一无法模拟的环节，但有 `reboot_after=400` 安全网兜底。**可以按剩余步骤推进上板。**
