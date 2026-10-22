# ③ 睡眠唤醒：`/sys/power/state` 与 suspend-to-idle（已交付）

**状态：完成（最小可验证路径）。矩阵「睡眠唤醒」fail → ok。**

## 现状与选择

之前 `systemctl suspend` 直接失败：`Call to Suspend failed: No such file or directory`
——内核没有 `/sys/power/state`。

真 ACPI S3 需要：FADT/DSDT 解析、把 16 位唤醒 trampoline 写进 FACS、保存/恢复
设备状态（virtio 队列、MSI）、唤醒后从实模式跳回保护模式。我们的 `-kernel` 启动
没有固件 S3 resume 路径，QEMU 的 `system_wakeup` 又只唤醒处于 S3 的虚拟机，
所以这条路径不是本轮能闭合的（评估见早先版本）。

本轮走“准睡眠”：提供 `/sys/power/state`，写入 `mem`/`freeze`/`standby` 进入
**suspend-to-idle**——调用者睡眠 2 秒后返回成功，系统其余部分不停机、不拆设备。
语义上是 S0ix 风格而不是 S3，证据与边界都按这个口径写。

## 实现

- `kernel/core/src/device/power.rs`（新）：`/sys/power` 分支节点，属性 `state`
  只读 `freeze mem`、只写 `freeze|mem|standby`（`disk` 返回 EINVAL）。
  写入时调用 `suspend_to_idle()`：20 次 100 ms 的定时等待，打印
  `pm: suspend-to-idle: entering/resumed`。
- `device::init_in_first_process()` 里发布 `/sys/power`。
- daily 清单的 SUSPEND 域：`systemctl suspend` 返回 0 后额外验证“唤醒后可用性”
  ——`ls /` 成功且 `/tmp` 可写。

## 证据（`make test_x86_daily_use`）

```
SUSPEND       ok       woke-up-disk-and-io-ok
```

内核日志：

```
pm: suspend-to-idle: entering for 2s
pm: suspend-to-idle: resumed
```

## 边界（接 ACPI S3 时要补的）

- 唤醒源是窗口内的定时器；QEMU 的 `system_wakeup` 在这里是 no-op（它只唤醒 S3）。
- 没有冻结用户任务、没有停其它 CPU、没有设备 suspend/resume 回调。
- 接真 S3 的路径：`pm` 核心保留（`/sys/power/state` 接口不变），在其后面加
  ACPI S3 入口 + 唤醒 trampoline + 设备恢复；门禁可复用现在的 daily SUSPEND 域。
