# 日常矩阵补全：AUDIO / USB / SUSPEND 三域

日期：2026-10-25　内核：`923ca5c53ea46e02`（tmpfs 1777 + flock wake-one）
镜像：**新建** `target/debian-x86-toolchain4/rootfs/debian-root.ext2`
（`desktop-drm-toolchain` schema 10，2.1 GiB，packages.lock 420 行）
门禁：`tools/x86/debian/daily_use_checklist_regress.sh`，
QEMU/KVM、wrapper 自带 `intel-hda` + `hda-output` + `qemu-xhci`

## 结论摘要

- profile 追加 `alsa-utils`、`libasound2t64`、`alsa-ucm-conf`、
  `usbutils`、`usb-modeswitch` 后重建镜像成功（schema 9 → 10）。
- 日常矩阵 **7 个要求域全部 ok**：
  APT / DESKTOP / APP / CLIPBOARD / **AUDIO** / **USB** / **SUSPEND**。
- 额外的 `INPUT_METHOD` 域仍 fail（`engine--no-text`），它不在本次
  7 域要求内，且与镜像 IME 引擎配置有关，不是这三域的问题。
- 串口 `Uncaught panic` = 0、`allocation failed` = 0、`oom-killer` = 0。

## 1. profile 变更与新镜像

`tools/debian/rootfs/profiles.py` 的 `desktop-drm-toolchain`：

| 项 | 变更 |
|---|---|
| `schema_version` | 9 → **10**（使缓存失效） |
| requested / identity 追加 | `alsa-utils`, `libasound2t64`, `alsa-ucm-conf`, `usbutils`, `usb-modeswitch` |

- 第一次重建失败：`contract: error: gate package libasound2 must have
  exactly one amd64 or all lock row` —— trixie 中 `libasound2` 是虚拟包，
  真实包名是 **`libasound2t64`**；改名后重建通过。
- 重建命令：
  `sudo tools/riscv/debian/rootfs/build_rootfs.sh --architecture amd64
  --profile desktop-drm-toolchain --output-dir target/debian-x86-toolchain4/rootfs`
- 产物：`debian-root.ext2` **2.1 GiB**；`packages.lock` **420 行**；
  `alsa-utils 1.2.14-1`、`libasound2t64 1.2.14-1+deb13u1`、
  `usbutils 1:018-2`、`usb-modeswitch 2.6.1-4+b2` 均在锁文件里。

## 2. 7 域结果表

| 域 | 结果 | 第一条错误（若有） | 关键证据 |
|---|---|---|---|
| APT | **ok** | — | `xclip+xterm+alsa-utils-installed` |
| DESKTOP | **ok** | — | `Xorg+DRI3` |
| APP | **ok** | — | xterm 窗口 |
| CLIPBOARD | **ok** | — | `xclip-round-trip` |
| **AUDIO** | **ok** | 无（`aplay -l` 只打印表头，见 §3） | `aplay -D hw:0,0` rc=0：`Playing WAVE ... 48000 Hz, Stereo`；`/dev/snd/{controlC0,pcmC0D0p}` 存在 |
| **USB** | **ok** | 无（`lsusb` 无输出，见 §3） | hotplug 后挂载 `/dev/sda`，读到 `ASTERINAS-USB-OK` |
| **SUSPEND** | **ok** | — | `systemctl suspend` 后 monitor `system_wakeup`，唤醒后磁盘 + 写入正常（`woke-up-disk-and-io-ok`） |
| INPUT_METHOD（额外域） | fail | `engine--no-text:` | fcitx5 存在但未产生文本；不在 7 域要求内 |

## 3. 两个 Asterinas 侧的“枚举/上报”缺口（未修）

1. **`/proc/asound/{cards,pcm}` 不存在**。`/dev/snd` 节点与放音都正常
   （`aplay -D hw:0,0` rc=0），但 ALSA 的 `aplay -l` 依赖
   `/proc/asound/cards` 枚举声卡, 因此只打印表头、不列出设备。
   - 第一条真实错误：`cat: /proc/asound/cards: No such file or directory`
   - 影响：`aplay -l` / 依赖 procfs 声卡枚举的工具看不到设备；
     按设备名访问不受影响。
   - 归属：**Asterinas 侧（procfs 缺 ALSA 节点）**，属功能缺口，
     实现需要 ALSA proc 接口，未在本轮 90 分钟内改动。
2. **`/sys/bus/usb/devices` 不存在**。USB 存储 hotplug 与挂载可用，
   但 sysfs 没有 USB 总线拓扑；`lsusb`（usbutils 018）执行成功但输出为空。
   - 第一条真实错误：`ls /sys/bus/usb/devices: No such file or directory`
   - 归属：**Asterinas 侧（sysfs 未暴露 USB 总线/设备）**，同样是
     上报层缺口；本轮不修。

两处都不影响 7 域的“功能”判定（AUDIO 放音成功、USB 挂载读取成功），
但会影响依赖标准 procfs/sysfs 枚举的用户态工具。

## 4. 门禁脚本的调整（已记录，避免误判）

- `aplay` 原来是无限等待：无音频设备时会永久阻塞，导致 USB/SUSPEND
  永不执行。回归 checklist 改为 **`timeout 15 aplay`**，超时记为
  `aplay-timeout-15s`。
- 前置打印 `aplay -l` 与 `lsusb` 的输出，作为 AUDIO/USB 的验收证据。
- **跳过 30 分钟稳定段**（原 checklist 在 USB 与 SUSPEND 之间 sleep
  6×300 s）；长稳已由独立的 30 轮压力门禁覆盖。
- 这些修改只在 `daily_use_checklist_regress.sh`（回归专用副本），
  未改标准 checklist。

## 5. 复现

```sh
sudo tools/riscv/debian/rootfs/build_rootfs.sh --architecture amd64 \
  --profile desktop-drm-toolchain --output-dir target/debian-x86-toolchain4/rootfs

DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/daily_use_checklist_regress.sh \
make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/debian-x86-toolchain4/rootfs/debian-root.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm X86_DEBIAN_XORG_TIMEOUT=1800 \
  DAILY_USE_WORKSPACE=$PWD/target/matrix-full

# 硬件枚举探针（aplay -l / lsusb / /dev/snd）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/matrix_hw_diag.sh \
make test_x86_daily_use … X86_DEBIAN_XORG_BASE_ROOT=…/toolchain4/rootfs/debian-root.ext2 \
  DAILY_USE_WORKSPACE=$PWD/target/matrix-hw-diag
```

## 6. 证据

- `evidence/2026-10-25-matrix-full/`：7 域串口摘录、hwdiag 摘录、
  profile 变更、build 日志尾
- 全量串口：`target/matrix-full/console.txt`、
  `target/matrix-hw-diag/console.txt`
- 构建日志：`/tmp/toolchain4-build.log`（尾段落入证据目录）
