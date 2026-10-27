# 内核待办清单：fsync 固定成本、pty/TTY、真实键盘、syscall 小项

日期：2026-10-27　内核基线：`058ae1515`（本文各项依次叠加）

本轮按 `/goal` 的清单推进，先给出每一项的结论与可复现证据，再给出未完成项
（fork 页表整页复制被既有红线否决、XIM 客户端仍失败）的现状。

## 1. fsync 固定成本：跳过干净 inode（已提交 `2d6d80fa2`）

`fs.sync_all()` 原来会遍历每个块组的 inode 缓存，对**每一个** inode 调
`sync_all()`，于是"重复 fsync 一个小文件"仍要刷整个缓存里所有文件的数据页。
现在只有元数据脏（descriptor 脏、新分配块、xattr 块脏）的 inode 才会被刷：
它们的 descriptor/bitmap 才可能被本次事务发布，data=ordered 的前提不变；
其余 inode 的数据持久性是它们自己 fsync 的职责。

| 场景（同 QEMU、同磁盘） | 修复前 | 修复后 |
|---|---|---|
| 10 次重复 fsync（4 KiB 文件） | 0.145 s | **0.034 s** |
| `fs.sync_all()` 阶段（TSC） | 12–17 M | 5.8–7.4 M |
| `dpkg --configure -a`（45 包，曾挂 16+ 分钟） | 未跑完 | **22.4 s** |
| `apt-get install sl`（含网络下载） | 挂死 | **14.9 s** |

崩溃一致性：先 fsync 文件 A，再用 mmap 弄脏文件 M 且不 fsync，直接 `kill -9`
QEMU。`e2fsck -fy` 只回放日志、零错误；重启后 A 的 SHA-256 不变，M 仍是上次
fsync 的内容（未 fsync 的 mmap 数据丢失合法）。`e2fsck -fn` 退出码 0。

## 2. pty/TTY：打开 tty 时自动取得控制终端（已提交 `058ae1515`）

之前"xterm 从不调用 TIOCSPTLCK"的结论是错的：LD_PRELOAD 只拦到了 open，glibc
内部的 `ioctl` 走隐藏符号，拦不到；内核日志里 `TIOCSPTLCK val=false` 是发生的。
真正的断点在后面：xterm 的子进程 `setsid` 后打开 pty slave，然后检查 `/dev/tty`
可用性。Linux 在打开 tty 时会把该 tty 设为"没有控制终端的会话首进程"的控制
终端（`tty_open_proc_set_tty`），本内核只在 TIOCSCTTY 里设置。于是 `/dev/tty`
返回 ENXIO，xterm 判 PTY_BAD，向父进程要新 pty，而新 pty 没有人解锁，之后每次
打开都是 EIO（日志里 create/EIO/master_drop 的循环）。

修复：把 `O_NOCTTY` 带入 `Inode::open` 的 flags，并在 pty slave 打开成功后按
Linux 条件自动 `set_control`（不抢已有会话的 tty；`set_control` 继续检查会话
首进程与已有控制终端）。验证：`xterm` 窗口 `IsViewable`，`-e` 的命令真的在
pty 里执行（写 `/tmp/pty-ok`），`xterm.log` 为空。

## 3. /dev/input 与真实键盘（门禁新增 KEYBOARD 域，`DAILY_KEYBOARD=ok`）

机制本身在内核里是通的，缺口在两处环境：

1. 门禁的 QEMU（`boot_debian.sh`）从未挂 virtio-keyboard，所以 guest 里
   根本没有 `/dev/input/event0`。门禁 wrapper 现在追加
   `-device virtio-keyboard-pci,disable-legacy=on,disable-modern=off`。
2. Xorg 没有输入驱动，且镜像里有一个 `xorg.conf.d/20-asterinas.conf`
   fragment，其 ServerLayout 只引用不存在的 `/run/asterinas-input/*`，会压过
   我们未被引用的 InputDevice 段。门禁现在写一份自包含的
   `xorg-kbd.conf`（modesetting + screen + evdev `/dev/input/event0` +
   layout），checklist 在选择该配置时删掉那个 fragment；APT 域顺带安装
   `xserver-xorg-input-evdev libevdev2`。

验收用例是"真键盘"而不是 XTEST：checklist 启动
`xterm -title kbdtest -e sh -c 'read x; printf %s "$x" > /tmp/kbd-out'`，
`xdotool windowfocus` 并确认 `getwindowfocus` 等于该窗口后打印
`DAILY_KEY_WAIT`；宿主在 QEMU monitor 上发 `sendkey a/b/c/ret`（重试 3 轮）。
本轮门禁：`DAILY_XFOCUS win=2097164 focus=2097164 tries=0`、
**`DAILY_KEYBOARD=ok virtio-evdev-xterm-abc`**。

## 4. x86_64 rseq(334)（已提交 `5fb41a488`）

generic 表有 `SYS_RSEQ`，x86_64 表没有 334，Debian 每个进程启动都走未知
syscall 路径：一轮日常门禁里 `Unimplemented syscall number: 334` 出现 **870**
次。注册后仍返回 ENOSYS（glibc 视为"无 rseq"），行为不变、日志清零。

## 5. wait4 `__WALL`（已提交 `328dcc496`）

按 Linux `eligible_child` 实现 clone 子进程区分：普通 wait4 只看 SIGCHLD 子
进程，`__WALL` 看全部，`__WCLONE` 只看 clone 子进程，被 trace 的子进程始终
可见。`WALL/WCLONE` 不再告警（`WNOTHREAD` 仍未实现）。静态探针结果：

```
WALL3 pid=76 nowall=-1 errno=10 wall=76 raw=0x700 exited=1 code=7
```

## 6. CLONE_SYSVSEM（已提交 `94ad82da6`）

pthread_create 都带这个标志，一轮门禁 94 条 `CLONE_SYSVSEM is not supported
now` 告警。内核还没有 SEM_UNDO 的 undo 列表（SEM_UNDO 本身返回 EINVAL），
Linux 也从不因该标志失败，因此静默接受即可；注释写明等 SEM_UNDO 落地后再
共享/复制。门禁日志：0 条。

## 7. RISC-V 性能（与 x86 同一份 ext2 代码，A/B 实测）

内核构建（含本轮全部修复）：
`--target-arch riscv64 --scheme riscv --features riscv_sv39_mode`。
为了能构建，先修了 `syscall/mod.rs` 里 x86 专用的 `regs.rsp/regs.rip`
（见 `b30c776b`，改用跨架构的 `UserContextApi`）。A/B 的"修复前"内核用
`git revert --no-commit` 回退 `2d6d80fa2`+`37c0fda9f` 后构建，其余不变。

测试负载：QEMU virt（TCG，无 KVM）、64 MiB ext2、先创建 400 个 4 KiB 文件并
一次 fsync 使其全部变干净（模拟 dpkg 装完后的 inode 缓存），再对一个小文件
重复 20 次 write+fsync。两次运行都用新格式化磁盘。

| 内核 | 20 次 fsync | 每次 | 说明 |
|---|---|---|---|
| 修复前 | 1597.2 ms | **79.9 ms** | 每次刷 400 个干净 inode |
| 修复后 | 639.9 ms | **32.0 ms** | 干净 inode 被跳过 |

即 RISC-V 上同样 **2.5×** 加速。剩余 ~32 ms 是 TCG 下 virtio-blk 的 I/O 与
日志提交成本（x86 KVM 上同负载约 1.5–2 ms），不是 inode 遍历。
`e2fsck -fn`/`-fy` 在 A/B 两个镜像上均干净（退出码 0）。

## 8. 未完成项（如实记录）

- **fork O(RSS) 页表复制**：设计文档
  `2026-10-27-fork-page-table-bulk-copy-design.md` 已存在，但"页表页整页复制
  会破坏 ostd 抽象"是 2026-10-26 明确划下的红线，该设计已被否决、只能保持
  逐页路径（512 MiB fork 204.7 ms，1.56 µs/页）。在红线解除前本项不实现。
- **XIM 客户端 XCreateIC**：门禁 `INPUT_METHOD` 仍 fail
  (`engine-wbpy-no-text`)，`XIM_SERVERS=@server=fcitx` 已注册、openbox 在跑，
  但 xterm 的 XIM 客户端路径没有提交出文字；详见
  `2026-10-27-input-method.md`。这是 fcitx5/Xlib 用户态问题，与内核无关，
  低优先级。

## 9. 门禁矩阵（`target/daily-kbd2`，内核 `94ad82da6`）

```
DESKTOP        ok    Xorg+DRI3
APP            ok    xterm
KEYBOARD       ok    virtio-evdev-xterm-abc
APT            ok    xclip+xterm+alsa-utils+xf86-input-evdev
CLIPBOARD      ok    xclip-round-trip
INPUT_METHOD   fail  engine-wbpy-no-text:
AUDIO          ok    aplay-hw:0,0
USB            ok    mounted-/dev/sda-read-ASTERINAS-USB-OK
SUSPEND        ok    woke-up-disk-and-io-ok
```

8/9 域 ok；唯一 fail 是既有的 INPUT_METHOD。`DAILY_DONE=ok`、稳定性 soak
6/6（load 0.000）、suspend 3/3 正常。

## 复现

```sh
# x86 日常门禁（含 KEYBOARD 域）
make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/ff-lag/daily.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm X86_DEBIAN_XORG_TIMEOUT=5400 \
  DAILY_USE_WORKSPACE=$PWD/target/daily-kbd2

# RISC-V A/B（内核 + 自带 initramfs 的 fsbench；见 /tmp/riscv_bench.c 思路）
```
