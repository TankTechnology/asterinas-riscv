# M1 报告：静态 BusyBox 在 Asterinas RISC-V 上跑通核心命令

> 2026-08-13。对应计划 `docs/superpowers/plans/2026-08-13-nixos-riscv-track.md` 的 M1。
> 结论先行：**静态 riscv64 BusyBox 已在 Asterinas 内核上跑通 sh/ls/cat/mount/ps，
> 无崩溃**；仅 2 个 glibc 启动探针型 syscall 返回 ENOSYS（无害），交互 shell 与
> `/proc/self` 存在缺口。

## 交付物（tools/riscv/nixos/）

| 文件 | 作用 |
|---|---|
| `build_busybox.sh` | 交叉编译静态 riscv64 BusyBox（allnoconfig + 指定 applet） |
| `init.c` | `/init`：挂载 /proc /sys /tmp，再用 `sh -c` 跑冒烟脚本 |
| `build_busybox_initramfs.sh` | 组装 initramfs（init + busybox + applet 软链 + 目录） |
| `boot_busybox_smoke.py` | QEMU 启动 → U-Boot booti → 采集串口 → 逐条判定命令 |
| `M1-report.md` | 本报告 |

产物（`target/nixos/`）：`busybox`（893 KiB，static ELF）、
`busybox-initramfs.cpio.gz`。

## 复现步骤

```bash
# 1. 交叉编译静态 busybox（riscv64-linux-gnu-gcc 15.1）
tools/riscv/nixos/build_busybox.sh

# 2. 组装 initramfs
tools/riscv/nixos/build_busybox_initramfs.sh

# 3. 用现有 booti 管线重打 boot 盘（复用同 commit 的 kernel Image 与 U-Boot）
export ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel-osdk-bin.Image"
export ASTERINAS_INITRAMFS="$PWD/target/nixos/busybox-initramfs.cpio.gz"
export QEMU_UBOOT_CACHE_DIR=/home/arch-anjie/Program/asterinas-riscv/target/qemu-uboot/cache
export QEMU_UBOOT_PROFILE=generic-sv39
tools/riscv/prepare_qemu_uboot_booti.sh prepare

# 4. QEMU 冒烟
python3 tools/riscv/nixos/boot_busybox_smoke.py
```

内核 Image 复用姊妹仓库 `asterinas-riscv`（同 HEAD `0b87071e5`）的构建产物；
本仓库仅做用户态引导，未改内核源码。

## 能跑的命令（12/12 通过，无崩溃）

核心五项（M1 目标）全部跑通，输出逐字如下：

| 命令 | 输出（节选） | 状态 |
|---|---|---|
| `sh` | 脚本解释执行，`echo __M1_SH_OK__` 输出 `__M1_SH_OK__` | ✅ |
| `ls /` | `proc  init  bin   dev   tmp   sys` | ✅ |
| `cat` | `hello-m1`（先 `echo hello-m1 > /tmp/m1.txt` 再读回） | ✅ |
| `mount` | `proc on /proc type proc (rw,relatime)` 等 7 行挂载表 | ✅ |
| `ps` | `1 0 9248 S /bin/sh -c ...` + `{ps} ...` | ✅ |

额外探针（用于补全 syscall 缺口清单）：

| 命令 | 输出 | 状态 |
|---|---|---|
| `uname -a` | `Linux (none) 5.13.0 #1 SMP ... riscv64` | ✅ |
| `stat /bin/busybox` | 完整 stat 输出（uid/gid 显示 `UNKNOWN`，因无 /etc/passwd） | ✅ |
| `df` | `tmpfs ... /dev/shm`、`tmpfs ... /tmp` | ✅ |
| `free` | `Mem: 2084524 used 105780 ...`（读 /proc/meminfo） | ✅ |
| `cat /proc/cpuinfo` | `processor : 0` | ✅ |
| `dd if=/dev/zero of=/tmp/z bs=4096 count=2` | 正常结束（stderr 摘要被重定向） | ✅ |

## syscall 缺口清单

冒烟过程中内核只打印了 2 条 `WARN: Unimplemented syscall number`，均为 glibc
在进程启动期的探针型调用，**返回 ENOSYS 后 glibc 自行降级，不影响任何命令**：

| syscall 号 | 名称 | 来源 | 影响 |
|---|---|---|---|
| 258 | `riscv_hwprobe`（arch_specific 244 + 14） | glibc 启动探 CPU 特性 | 无害 |
| 293 | `rseq`（restartable sequences） | glibc 启动注册每线程数据 | 无害 |

此外有两个**功能性缺口**（不是 ENOSYS，而是行为缺失）：

1. **termios / 交互 shell**：若让 `/init` 直接 `exec /bin/sh` 进入交互模式，
   ash 会把键入的字符回显（tty ECHO），但**不执行命令**——`exec /bin/sh` 之后
   串口再无 `read`/`fork`/`execve` 系统调用。这是计划里已知的「termios 边界
   （交互 shell 需要）」。因此 M1 用 `sh -c <脚本>` 的非交互路径验证命令，
   交互 shell 留待后续。
2. **`/proc/self` 缺失**：`readlink /proc/self` 报 `readlink: not found`
   （ENOENT）。`/proc/<pid>`（ps 读）、`/proc/meminfo`（free 读）、
   `/proc/cpuinfo`、`/proc/mounts`（mount 读）均正常，唯独 `self` 魔法软链未实现。

### busybox 实际用到的 syscall（已验证可用的面）

execve、clone、wait4、read/write、openat、newfstatat/newfstat、getdents64、
ioctl、fcntl、dup3、close、brk、mprotect、prctl、getpid/getppid/geteuid/getcwd、
rt_sigaction/rt_sigprocmask/rt_sigreturn、set_tid_address、set_robust_list、
prlimit64、readlinkat、getrandom、exit_group、mount。

## 崩溃清单

**无**。12 条命令全部正常退出，未触发内核 panic/Oops。

## 与计划「内核侧已知缺口」对拍

| 计划项 | M1 结论 |
|---|---|
| statx / openat2 / renameat2 | busybox 1.36.1 用旧式 `fstatat/fstat`，不触发；**对 M2/M3 的现代 coreutils 仍是缺口** |
| mlock/munlock | 未触发（busybox 不需要） |
| System V shm | 未触发 |
| /proc 节点完整性 | `meminfo/cpuinfo/mounts/<pid>` 有，**`self` 缺** |
| 用户命名空间 | 未评估（M3/M4） |
| termios 边界 | **确认缺口**（交互 shell 不工作） |

## 结论与下一步

M1 目标达成：静态 busybox 的核心命令在 Asterinas RISC-V 上跑通，缺口清单已产出。
下一步（M2）进入动态 glibc 用户态前，建议先补两项内核硬化：

1. `rseq` / `riscv_hwprobe`：实现或显式 stub（避免启动期 WARN 刷屏）。
2. termios 最小集（`TCGETS`/`TCSETS`）＋ `/proc/self` 魔法软链，解锁交互 shell。
