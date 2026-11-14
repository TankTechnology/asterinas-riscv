# Megrez 上的 Asterinas Debian：现在能做什么、缺什么（2026-11-14）

这份清单只写**在这块板上验证过**的东西，现场取自 2026-11-14 22:5x 的那次启动
（`uptime` 19 分钟、无 failed unit、42 个 active unit）。

## 1. 一句话

**它已经是一台能日常用的桌面机**：开机 26 s 进桌面、有浏览器/文件管理器/终端/中文
输入、可以 SSH/VNC 远程、apt 能装包、gcc 能编译、eMMC 能读写、720p 视频能放。
它不是一台完整的 Linux：**没有音频、没有 USB 存储、没有 GPU 加速、1080p 视频不够流畅、
ext4 还是窄特性面**。

## 2. 能用的东西（按层）

### 2.1 启动与恢复

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| 冷启动到桌面 | ✅ **14.4–15.6 s**（kernel 2.1 s + userspace 12.3–13.4 s） | `systemd-analyze`，见 §4 |
| 软重启 | ✅ 14 s 内 SSH 回来 | 今晚两次部署后重启 |
| 内核部署闸门 | ✅ 结构检查 + QEMU 冒烟 + 回滚镜像 | `tools/riscv/board/{preflight,deploy}_kernel.sh` |
| 失控自恢复 | ✅ 硬件看门狗 + 900 s 内核 deadline + panic 重启 + SysRq | `docs/porting/2026-11-11-megrez-recovery-safety-case.md` |
| 崩溃一致性 | ✅ SIGKILL 断电 + e2fsck 门禁 | `docs/porting/...crash-consistency` |
| 内核回归门禁 | ✅ `cargo osdk test`（22 crate / 1091 用例）本次恢复可编译 | 见 2.7 |

### 2.2 桌面

`Xorg 1.21`（fbdev，1920×1080）+ `openbox` + `lxpanel` + `pcmanfm` + `xterm` +
`Firefox` + `x11vnc`，`fcitx5` 中文输入可用。桌面会话被 systemd 监管，开机自动拉起。

远程：`x11vnc` 在 5900 上跑，主机侧 RFB 认证 + 抓帧验证过
（`docs/porting/2026-11-14-remote-desktop.md`）。

视频：`tools/riscv/apps/xshm_play.c` 走 MIT-SHM，**720p 实时 30 fps**，1080p 18 fps
（`docs/porting/2026-11-14-video-playback.md`）。

### 2.3 网络

| 项 | 状态 |
| --- | --- |
| 有线口 + 门户认证 | ✅ 双向通（主机 curl/ssh 直达板子） |
| SSH / SCP | ✅ dropbear + OpenSSH 方案都跑通 |
| VNC | ✅ 5900 |
| ICMP / `ping` | ✅ |
| `curl` / DNS | ✅ |
| 吞吐 | 门户限速 ~2.3 MB/s；本地 unix socket 179 MB/s |
| `dig` / `wget` | ❌ 没装（apt 可装） |

### 2.4 存储

- `/dev/mmcblk0p1/p2/p3`：p2 根分区 ext2 rw（3.0 GB，**已用 80%**），
  p3 `/data` ext2 rw（115 GB，用了 2%），p1 是 U-Boot/kernel 的 FAT/ext 分区。
- MMC 写路径修过两个内核 bug 后 **6–15 MB/s**，非对齐写可用。
- 工具：`lsblk`、`iostat`、`/proc/diskstats`、`/proc/partitions` 都真。

### 2.5 工具链

Debian 13 用户态：`apt`/`dpkg` 可用（今晚装了 5 个 -dev 包）、`gcc 14`（修好
`libgcc-14-dev` 丢失后）、`python3 3.13`、`git`、`systemd 257`、`ffmpeg 7.1`、`mpv 0.40`、
`lscpu`/`lsblk`/`iostat`/`vmstat`/`ss`/`ip`/`strace`/`x11perf`。

### 2.6 内核可观测面

`/proc`：`cpuinfo`(含 uarch) `meminfo` `stat` `vmstat` `zoneinfo` `buddyinfo`
`pagetypeinfo` `diskstats` `partitions` `loadavg` `uptime` `mounts` `filesystems`
`syscall_profile` `sysrq-trigger` `interrupts` `cgroups` `pressure/{cpu,memory,io}`
`sysvipc/{shm,sem,msg}`。
`/sys/class`：`block` `graphics` `net` `tty`；`/sys/devices/system/cpu`（含 topology）；
`/sys/module`（17 个已初始化组件，含 `initstate`/`refcnt`）。

### 2.7 回归门禁

```
aster-kernel  (cargo osdk test)        432 passed, 0 failed
aster-dwmac   (gate_ktests.sh)          33 passed, 0 failed
aster-mmc     (gate_ktests.sh)          50 passed, 0 failed
aster-bigtcp  (gate_ktests.sh)          21 passed, 0 failed
```

内核用例在这次之前**根本编译不过**（`sysfs.rs` 里 6 处 ktest 调用了重构后已不存在的
`build_dev_node`/`build_class_node`），本次修好；同时一条 ext2 断言还停留在旧语义
（计数与位图不一致报 EIO），已按代码现在"以位图为准、自愈计数、报 ENOSPC"的行为更新。

osdk 的测试过滤器吃完整路径（`aster_kernel::fs::...::test_name`），子串会匹配不到。

## 3. 缺的东西（诚实清单）

| 缺口 | 影响 | 现状 |
| --- | --- | --- |
| **音频**（无 `/dev/snd`） | 放视频没声音、会议/音乐不可用 | x86 侧做过 HDA 最小路径，板上没接 |
| **USB 存储 / `/dev/bus/usb`** | U 盘插上没反应，`lsusb` 不可用 | 只有 HID（键盘鼠标） |
| **GPU 加速** | 全部软件渲染，Firefox 滚动、视频都吃 CPU | `/dev/dri` 有节点，但缺 `/sys/class/drm`、`/sys/devices/platform`，Mesa 走不到硬件 |
| ~~`/proc/interrupts`、`/sys/module`~~ | ~~工具退化~~ | **已实现**（2026-11-15 夜） |
| ~~`/proc/pressure`、`/proc/sysvipc`、`/proc/cgroups`~~ | ~~压力接口缺~~ | **已实现**（PSI 数值暂为 0） |
| **ext4 journal** | 根分区是 ext2；断电一致性靠 fsck | 窄特性面 ext4 有，journal 没做 |
| 根分区余量 | 只剩 576 MB，装几个 -dev 包就紧张 | 可把开发物移到 `/data` |
| 1080p 视频 | 18 fps（解码 32 ms/帧封顶） | 见视频文档 |
| `run-lock.mount` 7.7 s、时钟同步 5 s | 开机时间 | 见下节 |

## 4. 开机时间：42.7 → 14.4 s（两轮）

| 轮次 | 做了什么 | 结果 |
| --- | --- | ---: |
| 第一轮（`2026-11-14-boot-time-optimization.md` §2） | USB 键鼠发现不再互等 30 s；桌面单元从永不激活的 `basic.target` 挪走 | 42.7 → 25.6 s |
| 第二轮（同文档 §6） | 串口控制台预算；对时改成并行单元；桌面不再排在 `sysinit.target`（= `/data` 挂载）后面 | 25.6 → **14.4 s** |

第二轮查清的三个东西：

1. **`run-lock.mount` 那 7.7 s 不是挂载慢，是串口慢**。`LogTarget=journal-or-kmsg`
   在 journald 起来前把每条管理器消息写进 `/dev/kmsg`，内核控制台同步写到 ttyS0，
   一个事件约 290 ms。改成 `LogTarget=journal` + `ShowStatus=no` 单这一项省 8.3 s。
2. **对时挡在桌面前面 5 s**：板子没有 RTC，`megrez-clock-sync` 是桌面的
   `ExecStartPre`，而开机时网络没起。改成并行单元后 8.6 s 完成，桌面不等。
3. **`/data` 挂载 2.1 s**：ext2 mount 要把 917 个块组的块位图 + inode 位图各读一遍
   （1834 次 4 KiB 读，每次约 1.1 ms）。它是 `Before=sysinit.target`，桌面又排在
   sysinit 后面，于是替它等了。去掉两处排序后归零。

现在的近似时间线（两次冷启动 14.40 s / 15.56 s）：

```
2.1   kernel
4.4   local-fs.target
5.5-7 /data 挂载与 sysinit（与桌面并行）
~7    asterinas-desktop-m5 开始
8.6   时钟同步完成（并行，不再挡路）
10.2  Xorg 起
10.5  asterinas-desktop-ready 完成
```

还剩下的：桌面单元 3.4–5.5 s 的 ExecStartPre 主要花在冷启动 Python（`desktop-input-identity`）
上，换成 shell/C 还能省 1–2 s；Xorg 起到 socket 就绪 2.6 s。
ext2 挂载已从 2.1 s 降到 21 ms（`2026-11-15-ext2-lazy-bitmaps.md`）。
