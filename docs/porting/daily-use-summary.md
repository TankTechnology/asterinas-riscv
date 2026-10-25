# 日常可用一页汇总（2026-10-23）

一条命令：`make test_x86_daily_use`（清单已覆盖桌面/GUI 应用/输入法/音频/U 盘/休眠/稳定性/性能）。

| 域 | 状态 | 关键数字 | 证据 |
|---|---|---|---|
| 桌面（Xorg + DRI3） | ok | `xdpyinfo` 有 DRI3 | `matrix.tsv` |
| 应用（xterm） | ok | 能开 pty | `2026-10-22-pty-ptmx.md` |
| x11-apps GUI | ok | xclock/xcalc/xeyes 均起窗（window 8388618/8388628） | `daily-use-report.md` Q2 |
| GTK 桌面应用（pcmanfm/eog…） | **ok（2026-10-25）** | `desktop-drm` 剖面补 `eog`+`sudo` 重建镜像（357 包）；guest 内 **pcmanfm window=0x200002**、**eog window=0x400002** | `2026-10-25-gtk-desktop-apps.md` |
| APT / 剪贴板 / 音频 | ok | apt 装包、xclip 往返、`aplay -D hw:0,0` | `matrix.tsv` |
| U 盘热插拔 | ok | n=10 有结果 9/9（3 轮由 BOT 恢复链救回）；initramfs 10/10 | `2026-10-22-usb-storage-blocker.md` |
| 睡眠唤醒 | ok | suspend-to-idle，3/3；唤醒后 xclock 起窗 | `2026-10-23-stability.md` |
| 输入法 | ok（受限） | XIM `@server=fcitx`、10 个 IM；端到端键入受测试环境限制 | `2026-10-22-input-method.md` |
| 稳定性 | ok | 30 min 无 panic/OOM；3/3 休眠；热插拔 ok | `2026-10-23-stability.md` |
| 性能基线 | ok | 写 859 MB/s；xclock 1.4 s；suspend→resume ≈2 s | `2026-10-23-performance-baseline.md` |

## 复现

```sh
make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/debian-x86-gtk2/rootfs/debian-root.ext2 \
  X86_DEBIAN_XORG_TIMEOUT=1800
```

门禁在任一必需域缺失时返回非零（`no result for: …`），可直接用于 CI。
