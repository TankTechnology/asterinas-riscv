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
| 桌面（Xorg + DRI3 + 应用） | **ok** | Xorg + DRI3，xterm 正常启动（`/dev/ptmx` 已由 devtmpfs 提供） |
| 应用 | ok | xterm 起得来、伪终端可用（见 `2026-10-22-pty-ptmx.md`） |
| apt 装小包 | ok | `apt-get install -y xclip xterm alsa-utils` 成功 |
| 剪贴板 | ok | `xclip` 写读往返一致 |
| 桌面应用（pcmanfm/eog/gpicview/epdfview/evince） | missing | 基座镜像未安装，运行时 apt 安装未成功（Q2，见 `daily-use-report.md`） |
| 输入法 | **ok（受限）** | XIM 服务已注册、`inputmethod/` 10 个 IM（wbpy 等）、fcitx5 存活且 D-Bus 调用成功；端到端键入受测试环境限制（无可用 WM：matchbox 未接管焦点、twm 启动即退出，`allowSendEvents` 直投也无按键落盘）；见 `2026-10-22-input-method.md` |
| 音频 | ok | `/dev/snd/pcmC0D0p` + `aplay -D hw:0,0` 播放成功（见 `2026-10-22-alsa-aplay.md`） |
| U 盘热插拔 | **ok** | 枚举 → `/sys/bus/usb/devices/1-1` → `/dev/sda` → `mount -t ext2 -o ro` → 读 `hello.txt` → `umount`；initramfs 门禁 10/10 通过；Debian daily 负载下多次出现 `can't read superblock`（1–2/3），重试链完整（控制器端点恢复 + ClearFeature(HALT)/BOT class reset + 重发）；Debian daily 10 连跑：有结果 9/9 ok（3 轮由恢复链救回），另 1 轮为夹具丢 marker；原始成功率 6/9、恢复后 9/9，见 `2026-10-22-usb-storage-blocker.md` |
| 睡眠唤醒 | **ok** | `/sys/power/state` + suspend-to-idle（2 s 窗口），`systemctl suspend` 返回 0，唤醒后磁盘/IO 可用（见 `2026-10-22-suspend-assessment.md`） |

原始产物：`target/daily-use/matrix.tsv`、`console.txt`（串口全量）、
`screen.ppm`（X 起来后的截屏）、`usbstick.img`（热插的 U 盘）。

## 每个缺口意味着什么

- **应用（pty）**：已修复。根因不是 pty 池大小，而是 devtmpfs 没有提供
  `/dev/ptmx`（镜像里的链接被 devtmpfs 挂载盖住）。修复见
  `2026-10-22-pty-ptmx.md`。
- **输入法**：fcitx5 已能在镜像里安装并启动，但引擎加载后进程崩溃
  （`libpinyin.so` 回溯，内核缺 `MSG_NOSIGNAL`/`WEXITED`），详见
  `2026-10-22-input-method.md` 的卡点与两条替代路径。
- **音频**：已由 `2026-10-22-alsa-aplay.md` 补齐——`/dev/snd/controlC0`、
  `/dev/snd/pcmC0D0p` 与 ALSA PCM ioctl 子集，镜像里的 `aplay` 可以直接播放；
  剩余边界是同步播放（块间有短静音）、无 mmap、无录音/混音器控件。
- **USB 热插拔**：已打通到可挂载——xHCI 枚举、配置、BOT/SCSI、`/dev/sda` 块设备、
  ext2 只读挂载读文件全部通过门禁（`2026-10-22-usb-storage-blocker.md`）。
- **睡眠唤醒**：已提供 `/sys/power/state` 与 suspend-to-idle；ACPI S3 的
  trampoline/设备恢复仍未实现（`2026-10-22-suspend-assessment.md` 有边界说明）。

## 与门禁的关系

- 这张表不是"全绿门禁"，而是**能力账本**：每条 `fail`/`missing` 都可直接转成
  一个后续里程碑，并且有可复现的测量命令。
- 桌面/DRI3、apt、剪贴板三项已经 `ok`，说明底座（内核 + 镜像 + 网络 + X）可用。
- 每修好一项，重跑本门禁即可看到对应的行变化；`matrix.tsv` 可以直接进 CI 产物。
