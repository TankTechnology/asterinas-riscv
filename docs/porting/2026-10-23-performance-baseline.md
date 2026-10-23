# 性能基线（Q4，2026-10-23，x86_64 + Debian trixie on Asterinas）

一轮 daily 门禁内采集（`DAILY_PERF …` 打在串口）。

| 指标 | 数值 | 方法/备注 |
|---|---|---|
| GUI 应用启动（`xclock`） | **1.38 s / 1.39 s**（两次成功采样） | 从 `&` 到 `xdotool search --class xclock` 找到窗口；Xorg 已就绪 |
| 写带宽（tmpfs `/tmp`） | **859 MB/s** | `dd if=/dev/zero of=/tmp/perf.bin bs=1M count=64` |
| 读带宽 | 未采到 | `dd` 输出的速率行未被正则匹配（`DAILY_PERF dd_read` 为空），下次改用 `iflag=direct` 并直接打印 stderr |
| apt 安装小包（`--reinstall hello`） | 失败 | `E: Unable to lock the administration directory`——与门禁早期 APT 域/后台 `apt list` 抢锁 |
| suspend→resume | **约 1.6–2.0 s** | Q3 的 `before/after` uptime 差（suspend-to-idle 固定 2 s 窗口） |

## 结论与下一次改进

- 本轮是**新基线**（`docs/performance/` 下没有 x86 Debian 的既有基线可对比）。
- 两项需重测：读带宽（改 dd 参数与解析）与 apt 耗时（放到没有并发 apt 的时点，或加
  `-o Debug::NoLocking=1`）。
- `xclock` 1.4 s 的启动时间主要包含 X11 连接与窗口映射，可作为 GUI 响应性的参考值。

矩阵「性能基线」行记 **ok（写入 859 MB/s、GUI 启动 1.4 s；读带宽/apt 待重测）**。
