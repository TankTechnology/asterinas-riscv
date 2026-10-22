# 日常可用矩阵（x86_64 Debian 镜像）

一次可重复的"日常能力"快照。门禁只测量、不修功能：

```sh
make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/final-matrix/xwrap/image18.ext2
```

门禁做的事：启动 Debian 镜像 → 串口密码登录 → 运行
`tools/x86/debian/daily_use_checklist.sh`（注入到 `/usr/local/bin/daily-use-checklist`）
→ 宿主在收到 `DAILY_USB_WAIT` 时通过 QEMU monitor 热插 U 盘、在
`DAILY_SUSPEND_WAIT` 时发 `system_wakeup` → 汇总成 `matrix.tsv` 并打印。
`fail`/`missing` 不会让门禁失败；只有"清单没跑完"才算失败，因此表格是现状快照。

## 结果（2026-10-22，x86_64 + KVM，kernel `73b3a662e` + 音频提交）

| 域 | 结果 | 详情 |
|---|---|---|
| 桌面（Xorg + DRI3 + 应用） | **部分通过** | Xorg 起来且 `xdpyinfo` 有 DRI3；应用启动失败 |
| 应用 | fail | `xterm: Error 32, errno 2 ... Reason: get_pty: not enough ptys` |
| apt 装小包 | ok | `apt-get install -y xclip xterm alsa-utils` 成功 |
| 剪贴板 | ok | `xclip` 写读往返一致 |
| 输入法 | missing | 镜像里没有 fcitx5/ibus（也未安装） |
| 音频 | ok | `/dev/snd/pcmC0D0p` + `aplay -D hw:0,0` 播放成功（见 `2026-10-22-alsa-aplay.md`） |
| U 盘热插拔 | missing | 内核没有 `/sys/bus/usb`（无 USB 子系统，qemu-xhci 不被枚举） |
| 睡眠唤醒 | fail | `systemctl suspend` 返回 1：`Call to Suspend failed: No such file or directory`（缺 `/sys/power/state`） |

原始产物：`target/daily-use/matrix.tsv`、`console.txt`（串口全量）、
`screen.ppm`（X 起来后的截屏）、`usbstick.img`（热插的 U 盘）。

## 每个缺口意味着什么

- **应用（pty）**：xterm 打开伪终端失败——内核的 pty 数量上限或 devpts 分配在
  这个会话里不够用（演示环境里 systemd、Xorg、apt 已经占用了若干）。这是
  内核侧可修的缺口（增大 pty 池 / 检查 devpts 挂载），不是应用问题。
- **输入法**：镜像没有输入法框架；属于 rootfs profile 的内容，加上
  `fcitx5`（含 `fcitx5-chinese-addons`）与 XIM 才能测。
- **音频**：已由 `2026-10-22-alsa-aplay.md` 补齐——`/dev/snd/controlC0`、
  `/dev/snd/pcmC0D0p` 与 ALSA PCM ioctl 子集，镜像里的 `aplay` 可以直接播放；
  剩余边界是同步播放（块间有短静音）、无 mmap、无录音/混音器控件。
- **USB 热插拔**：内核完全没有 USB 子系统（xHCI + USB core + 存储类），
  这是排序里第二优先的能力（音频 > USB > Wi-Fi）。
- **睡眠唤醒**：缺 `/sys/power/state` 与 ACPI S3 支持；QEMU 侧 `system_wakeup`
  已经接好，等内核提供挂起入口即可复用。

## 与门禁的关系

- 这张表不是"全绿门禁"，而是**能力账本**：每条 `fail`/`missing` 都可直接转成
  一个后续里程碑，并且有可复现的测量命令。
- 桌面/DRI3、apt、剪贴板三项已经 `ok`，说明底座（内核 + 镜像 + 网络 + X）可用。
- 每修好一项，重跑本门禁即可看到对应的行变化；`matrix.tsv` 可以直接进 CI 产物。
