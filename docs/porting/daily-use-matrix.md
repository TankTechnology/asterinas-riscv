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

## 结果（2026-10-22，x86_64 + KVM，kernel `73b3a662e` + 音频提交；2026-10-23 追加 L1 包生态；2026-10-24 追加 L4 源码包构建、L5 从零 rootfs 与 L6 跨版本升级）

| 域 | 结果 | 详情 |
|---|---|---|
| 桌面（Xorg + DRI3 + 应用） | **ok** | Xorg + DRI3，xterm 正常启动（`/dev/ptmx` 已由 devtmpfs 提供） |
| 应用 | ok | xterm 起得来、伪终端可用（见 `2026-10-22-pty-ptmx.md`） |
| apt 装小包 | ok | `apt-get install -y xclip xterm alsa-utils` 成功 |
| Debian 包生态（L1） | **ok** | popcon top-50 安装 50/50；smoke ok 31/50（cli 3 + lib 19）；0 失败。L1b 新装 40/40（pre=absent 40/40），命令级 smoke 38/40（+1 cli；iptables 因缺 `NETLINK_NETFILTER` 失败）；见 `docs/porting/debian-package-compat.md` |
| systemd 健康（L2） | **ok** | systemd 257.13，`is-system-running=running`，失败 unit **0/156**；运行中 service 9；启动 **3.416 s**（kernel 0.303 + userspace 3.112）；3 个关键 unit verify rc=0；boot error 3 条（DRM uevent EIO、`/proc/kmsg` 缺失）；见 `docs/porting/debian-systemd-health.md` |
| syscall 一致性（L3） | **ok（基线 34/40）** | LTP 20260930 子集 40 case：**34 pass / 6 fail / 0 skip**（85%）；6 个失败已逐条归因（fd 上限未强制 ×3、O_NOATIME 权限检查 ×1、`/proc/maps` 展示 ×1、测试 fd 复用 ×1）；整轮 179 s；见 `docs/porting/debian-ltp-baseline.md` |
| 源码包构建（L4） | **ok（2/3）** | guest 内 `apt-get build-dep` → `apt-get source` → `dpkg-buildpackage -b -us -uc`：`hello` 2.10-5 产出 2 个 `.deb`（360 s）、`python3-minimal`（源码包 `python3-defaults` 3.13.5-1）产出 19 个 `.deb`（383 s）；`coreutils` 9.7-3 在 `debian/rules clean` 被 SIGSEGV（255，2 次一致），根因是 `DPKG_EXPORT_BUILDFLAGS=1` 导出的递归 `$(shell dpkg-buildflags)` 变量让 make 失控；基线另暴露并规避了 `splice` ENOSYS、SysV IPC 缺失、ext4 日志单事务 253 块上限；见 `docs/porting/debian-source-build.md`。**ebig-fix 后复测（内核 `14b2d352a`）**：仍 **2/3**，`coreutils` 仍 rc=255 signal 11（make 崩溃，非 E2BIG） |
| 从零 rootfs（L5） | **ok（ebig-fix 后 ext2/tmpfs 均三阶段全过）** | `debootstrap` 1.0.141 / trixie minbase：ext2 `/target` install/foreign/second = **0/0/1**（19.09 / 46.46 / 2.68 s），second 在 `/sbin/ldconfig` 写缓存时报 E2BIG（ext4 日志单事务 253 块），关机后 `/target` 未落盘；tmpfs `/tmp/target` = **0/0/0**（18.29 / 38.05 / 24.28 s），得到完整 trixie rootfs（`debian_version=13.7`、`/bin/sh→dash`、214 MiB）；`chroot`/`mount -t proc`/`umount`/`unshare -m` 探针全 rc=0；见 `docs/porting/debian-debootstrap.md`。**ebig-fix 后复测（内核 `14b2d352a`）**：ext2 `/target` 也 **0/0/0**（18.73 / 43.84 / **418.89 s**），rootfs 完整且 `/target` 关机后仍在 |
| 跨版本升级（L6） | **fail（ebig-fix 后改为 40 分钟超时；已到 forky/sid 且可启动）** | 基础 `target/systemd-health/daily.ext2`（sha `450ebe3b…`）复制到 `target/debian-upgrade/`，base/copy sha 相同；trixie→forky 切源与 `apt-get update` 正常（297 可升级）；`full-upgrade` 下载 **341** 包后在第 9 个包 `linux-libc-dev` 处 `msync`/`fsync` 报 E2BIG（ext4 日志 253 块上限），rc=100、143 s，实际解包 **8**/配置 **4**（含 glibc 2.43）；二次启动成功、串口可登录、systemd **running**，`debian_version` 仍 **13.7**；见 `docs/porting/debian-full-upgrade.md`。**ebig-fix 后复测（内核 `14b2d352a`）**：内核 E2BIG **0 条**，升级推进到 `debian_version=forky/sid`，但 **40 分钟超时**（rc=124；解包 227/341、配置 78，止于 `git-man`），副本仍可启动（systemd running、可登录） |
| 剪贴板 | ok | `xclip` 写读往返一致 |
| 桌面应用（x11-apps 类：xclock/xcalc/xeyes） | **ok** | 7 包闭包注入镜像后三个 GUI 程序均起窗（`window=8388618/8388628`）；pcmanfm/eog 等 GTK 应用需 118 包闭包，不在本镜像范围 |
| 输入法 | **ok（受限）** | XIM 服务已注册、`inputmethod/` 10 个 IM（wbpy 等）、fcitx5 存活且 D-Bus 调用成功；端到端键入受测试环境限制（无可用 WM：matchbox 未接管焦点、twm 启动即退出，`allowSendEvents` 直投也无按键落盘）；见 `2026-10-22-input-method.md` |
| 音频 | ok | `/dev/snd/pcmC0D0p` + `aplay -D hw:0,0` 播放成功（见 `2026-10-22-alsa-aplay.md`） |
| U 盘热插拔 | **ok** | 枚举 → `/sys/bus/usb/devices/1-1` → `/dev/sda` → `mount -t ext2 -o ro` → 读 `hello.txt` → `umount`；initramfs 门禁 10/10 通过；Debian daily 负载下多次出现 `can't read superblock`（1–2/3），重试链完整（控制器端点恢复 + ClearFeature(HALT)/BOT class reset + 重发）；Debian daily 10 连跑：有结果 9/9 ok（3 轮由恢复链救回），另 1 轮为夹具丢 marker；原始成功率 6/9、恢复后 9/9，见 `2026-10-22-usb-storage-blocker.md` |
| 性能基线（Q4） | **ok** | 写 770 MB/s、读 **3.3 GB/s**（O_DIRECT）；`xclock` 1.37/1.58/1.46 s；suspend→resume ≈1.6–2.0 s；apt 重装 hello **15.81 s**（须在清单最开头测，否则撞 dpkg db 锁）；见 `2026-10-23-performance-baseline.md` |
| 稳定性（Q3） | **ok** | 30 分钟负载无 panic/OOM；suspend/resume **3/3**（唤醒后 xclock 起窗）；U 盘热插拔 ok（本轮 1 次 + 既有 10 连跑）；见 `2026-10-23-stability.md` |
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
