# ③ 睡眠唤醒：现状评估与两条路径

**状态：未实现，先记录卡点与路径（按 roadmap 的规则）。**

矩阵现状：`SUSPEND fail rc=1: Call to Suspend failed: No such file or directory`
——`/sys/power/state` 不存在，systemd 连入口都找不到。

## 为什么这一项不是“补一个文件”

QEMU 的 `system_wakeup` 唤醒的是 ACPI S3：固件/内核在睡眠时把唤醒向量写进 FACS，
CPU 从 `SLP_TYP=S3 + SLP_EN` 进入睡眠后，唤醒事件会让 CPU 从**实模式唤醒向量**
继续执行。Linux 为此在低内存放了一段 16 位 trampoline（`acpi_wakeup_address`），
负责恢复页表、GDT/IDT、栈，再跳回保护模式的内核恢复路径。我们的内核目前没有：
- 没有 `/sys/power/state`（power management 子系统）；
- 没有 ACPI S3 的设备保存/恢复序列（内存中的设备状态、MSI、virtio 队列）；
- 没有唤醒 trampoline（S5 关机已有 PM1 轮询链路，见 `x86-acpi-s5-power` 记录）。

## 两条路径

1. **真 S3（完整）**：ACPI 表解析（FADT/DSDT）→ 分配唤醒 trampoline（<1 MiB）
   → `_PTS` → 保存设备状态 → 写 FACS 唤醒向量 → `SLP_TYP=S3|SLP_EN`
   → 唤醒后从 trampoline 恢复 → `_WAK` → 恢复设备（virtio-blk/net 队列重建）。
   工作量大（1–2 周），且需要 virtio 驱动的 suspend/resume 回调。
2. **准睡眠（最小可用）**：先提供 `/sys/power/state`（`mem`），实现
   “冻结用户态 + 停设备队列 + 进入 WFI 等唤醒源”的内核挂起，不真正下电。
   QEMU 的 `system_wakeup` 在 S3 之外不会唤醒它，因此门禁要改成
   “`systemctl suspend` 返回 0 且唤醒后 I/O 正常”，由门禁侧用 RTC/键盘唤醒。
   这能让矩阵行变 ok，但语义上不是 ACPI S3，需要在文档里写明。

建议先做 2 的接口与冻结/恢复框架，再把 1 的 ACPI S3 接到同一个 `pm` 核心上。
