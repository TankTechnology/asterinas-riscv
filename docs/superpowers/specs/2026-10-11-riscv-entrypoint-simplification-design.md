# RISC-V 工具入口精简设计

## 目标

减少 Asterinas RISC-V 启动和桌面实验的公开入口，避免多个脚本分别实现
超时、串口占用、watchdog、恢复和收据逻辑。第一轮只整理入口，不改变启动
参数、串口协议、测试语义或现有证据。

## 主入口

主线只保留三组公开入口：

1. QEMU 启动：
   `tools/riscv/prepare_qemu_uboot_booti.sh` 负责准备，
   `python3 -m tools.riscv.qemu_uboot_booti` 负责运行。
2. 真机调试：
   `python3 -m tools.riscv.megrez_debug simulate|board` 负责模拟和受控上板。
3. 桌面验收：
   `python3 -m tools.riscv.megrez_desktop_boot` 负责桌面介质发布和启动，
   `python3 -m tools.riscv.megrez_display_gate` 负责最小 fb0 门禁。

`megrez_board_session.py`、`qemu_uboot_session.py` 及其 execution/io 模块
是内部实现，不作为新的用户入口。它们继续提供统一的串口所有权、超时、
收据和清理行为。

## 兼容路径

现有 shell wrapper 和旧 Python CLI 暂不删除，也不迁移目录。需要保留的旧
命令只转调主入口，并保持原有参数、退出码和输出目录约定。wrapper 不得
自行实现启动、恢复、watchdog 或收据解析。

第一轮优先处理 `verify_megrez_sim.sh`，因为它与当前 QEMU U-Boot runner
职责重叠最明显。`eic7700_isolation.sh`、SDHCI 构建脚本和 Firefox 检查
脚本先保持不变，直到完成引用和证据审计。

## 不在本轮处理

- 不删除测试、证据、旧实验目录或未提交文件。
- 不修改内核、设备树、rootfs、启动参数、watchdog 或串口协议。
- 不合并 `megrez_desktop.py`、`megrez_desktop_boot.py` 和
  `megrez_physical_graphics.py` 的内部实现；它们职责不同，先只收敛公开入口。
- 不访问真机，不发送串口数据，不重启开发板。

## 验收

每个兼容 wrapper 必须满足：

- 静态检查确认它只调用一个 canonical 入口；
- 旧参数仍能映射到同一 profile、artifact 和输出目录；
- 旧路径与 canonical 路径返回相同退出码；
- 收据 schema、serial tail hash 和 failure classification 不变；
- 现有 QEMU U-Boot 合同测试、恢复故障测试和 goal audit 测试全部通过。

只有上述验收通过后，才处理下一组入口。没有引用证据的脚本也先标记为
候选，不直接删除。
