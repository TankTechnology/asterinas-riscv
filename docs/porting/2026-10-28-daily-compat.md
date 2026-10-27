# 日用兼容：apt 冷启动验收与 /proc 时间口径（P2/P3）

日期：2026-10-28
镜像：`target/session/base.ext2`（Debian trixie，feat 内核
`kernel-proven.elf`）

## 1. P2：冷启动、不改任何配置的 apt

验收口径：冷启动 → 串口登录 → 直接 `apt-get update`、`apt-get install`、
`apt-get remove`，不碰 sources.list、不设代理、不手工改 DNS。

```
P2 update_rc=0  seconds=13
P2 install_rc=0 seconds=10
ii  sl  5.02-1+b1  amd64  Correct you if you type `sl' by mistake
P2 remove_rc=0  seconds=8
dpkg-query: no packages found matching sl
```

- 源是 `https://mirrors.tuna.tsinghua.edu.cn/debian trixie InRelease`，
  HTTPS 与证书链正常（`Hit:1`）。
- 安装后 `command -v sl` 命中，卸载后 `dpkg -l sl` 为空。
- 全过程的 DNS 解析由 slirp 的内置 DNS 完成；`getent hosts` 同时有 A/AAAA。
- 更重的路径已有既有证据：门禁 APT 域 `DAILY_APT=ok
  xclip+xterm+alsa-utils+xf86-input-evdev+yad`，以及 L1 popcon 50/50。

## 2. P3：/proc 时间口径

### 2.1 main 已经正确

main（`148b2db64`）的 `kernel/src/time/mod.rs` 定义 `USER_HZ=100`，
`/proc/stat` 走 `jiffies_to_clock_ticks`，`/proc/<pid>/stat` 走
`duration_to_clock_ticks`。运行时复核（main 内核 + 最小 initramfs，
2 vCPU，挂载 proc 后空转 10 s）：

```
P3 before=44 after=1995 delta_10s=1951   →  ~97.6 ticks/s/CPU
```

若按 1000 Hz 计数，应约为 20000。

### 2.2 跑 Debian 的 feat 内核：进程级已修，全局行仍 1000 Hz

同一份 Debian 镜像（4 vCPU）上：

```
P3 cpus=4 before=48128 after=87489 delta10s=39361 per_cpu_hz=984   (全局 cpu 行)
P3proc pid=111 ticks_5s=501                                        (单进程 5 s)
```

即：

- **`/proc/<pid>/stat` 的 utime/stime 已经是 USER_HZ=100**（5 s 忙循环 = 501
  ticks），feat 分支的 `0afe3b2ee` 生效；
- **`/proc/stat` 的全局/每 CPU 行仍是 1000 Hz**（约 984 ticks/s/CPU），feat 的
  `kernel/core/src/fs/fs_impls/procfs/stat.rs` 还没有做 `jiffies → USER_HZ` 的
  换算。

影响：读全局 cpu 行算 CPU 占用率的脚本会放大 10 倍；`top` 的进程列正常。
修复点与 main 的 `a73b65622` 相同（`jiffies_to_clock_ticks`），但这段代码只在
feat 的 `kernel/core` 里；main 的 `kernel/src` 不能引导 Debian 镜像（缺
devtmpfs 挂载与 root-init，见 `docs/porting/2026-10-28-sem-undo.md` §3 与
`main-kernel-cannot-boot-debian` 记录）。因此这条修复的落点取决于 Debian
内核何时回到 main：在 main 上已经正确，feat 上还差全局行的换算。

## 3. P4

xterm 的 Xlib XIM 吞键已修复并验证，见
`docs/porting/2026-10-28-desktop-session.md` §3.5。
