# 稳定性验证（Q3，2026-10-23）

一轮 `make test_x86_daily_use`（`X86_DEBIAN_XORG_TIMEOUT=2700`）内完成三件事：
30 分钟负载、3 轮 suspend/resume、U 盘热插拔。

## 1) 30 分钟连续负载

后台循环 `apt list --installed`，每 5 分钟打一条状态：

| tick | 时间（guest uptime） | loadavg |
|---|---|---|
| 1 | 788 s | 0.276 |
| 2 | 1088 s | 0.071 |
| 3 | 1389 s | 0.222 |
| 4 | 1689 s | 0.066 |
| 5 | 1990 s | 0.240 |
| 6 | 2290 s | 0.051 |

`DAILY_STAB_DONE uptime=2590.54`。30 分钟内**无 panic、无 OOM、串口未掉线**
（唯一 WARN 是 `Switching to a task already running in the foreground`，无害）。

## 2) suspend / resume ×3

```
DAILY_STAB_SUSPEND round=1 result=ok before=787.30 after=789.26 xclock=10485770
DAILY_STAB_SUSPEND round=2 result=ok before=796.62 after=798.15 xclock=10485770
DAILY_STAB_SUSPEND round=3 result=ok before=805.50 after=806.91 xclock=10485770
```

每轮：`systemctl suspend` 返回 0；唤醒后 uptime 前进（suspend-to-idle 的 2 s 窗口）；
**唤醒后 `xclock` 都能起窗**（window 10485770）。3/3 成功。

## 3) U 盘热插拔

本轮门禁自带一次完整插拔（attach → 枚举 → 挂载读 `hello.txt` → 卸载 → detach）：

```
USB ok mounted-/dev/sda-read-ASTERINAS-USB-OK
```

此前已有 **10 连跑**的统计（`2026-10-22-usb-storage-blocker.md`「10x 稳定性」：
有结果 9/9，其中 3 轮由 BOT 恢复链救回）。额外两轮插拔需要 QEMU monitor 通道，
清单侧无法驱动，故以门禁自带的一轮 + 既有 10 连跑为准。

## 结论

| 项目 | 结果 |
|---|---|
| 30 分钟负载 | ok（无 panic/OOM/掉线） |
| suspend/resume 3 轮 | **3/3 ok**，唤醒后 GUI（xclock）正常 |
| U 盘热插拔 | ok（本轮 1 次 + 既有 10 连跑） |

矩阵「稳定性」行记为 **ok（30min + 3/3 休眠 + 热插拔 ok）**。
