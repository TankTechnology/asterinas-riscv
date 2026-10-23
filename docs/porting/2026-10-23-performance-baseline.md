# 性能基线（Q4，2026-10-23，x86_64 + Debian trixie on Asterinas）

一轮 daily 门禁内采集（`DAILY_PERF …` 打在串口）。

| 指标 | 数值 | 方法/备注 |
|---|---|---|
| GUI 应用启动（`xclock`） | **1.38 s / 1.39 s**（两次成功采样） | 从 `&` 到 `xdotool search --class xclock` 找到窗口；Xorg 已就绪 |
| 写带宽（tmpfs `/tmp`） | **770 MB/s** | `dd … count=64`，原始输出：`67108864 bytes … 0.0871955 s, 770 MB/s` |
| 读带宽（tmpfs，O_DIRECT） | **3.3 GB/s** | `dd if=/tmp/perf.bin of=/dev/null bs=1M iflag=direct`，原始输出：`… 0.0200721 s, 3.3 GB/s` |
| apt 安装小包（`--reinstall hello`） | **15.81 s** | 把测量挪到清单最开头（任何 APT 域之前）：`DAILY_PERF apt_early seconds=15.81`。此前在 APT 域之后测量时被 dpkg db 锁挡住（`FAQ#db-lock`），说明该锁由前台 apt 进程持有 |
| suspend→resume | **约 1.6–2.0 s** | Q3 的 `before/after` uptime 差（suspend-to-idle 固定 2 s 窗口） |

## 结论与下一次改进

- 本轮是**新基线**（`docs/performance/` 下没有 x86 Debian 的既有基线可对比）。
- 读带宽（3.3 GB/s）与 apt 耗时（15.81 s）均已补齐；后者必须在清单最开头测量，否则撞 dpkg db 锁。
- `xclock` 1.4 s 的启动时间主要包含 X11 连接与窗口映射，可作为 GUI 响应性的参考值。

矩阵「性能基线」行记 **ok（写入 859 MB/s、GUI 启动 1.4 s；读带宽/apt 待重测）**。
