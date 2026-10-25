# Asterinas 上的 Debian 可用性总报告

日期：2026-10-25　仓库 HEAD：`ad9e5f829`
目标：x86_64 Debian（trixie → forky/sid 升级路径），QEMU/KVM
内核：最新验证内核 `e23120606e4d942f`（L6）、`20582f4b207b6cb1`
（ring-buffer 修复后）与 `548e5f0debee52c0`（tmpfs 根 1777 修复后）

本报告只汇总**已经跑过并有日志/文档证据**的结果；未验证的项不写入能力表。
所有证据路径相对于 `docs/porting/`。

## 1. 能力清单

| 类别 | 已验证项 | 关键数字 | 证据 |
|---|---|---|---|
| **L1** 包生态 | popcon top-50 安装 + 冒烟 | 安装 **50/50**；冒烟 **31/50**（含 3 个 CLI） | `debian-package-compat.md`；`target/debian-popcon/` |
| **L1b** 真实新装 | 镜像里没有的 40 包 | 安装 **40/40**；冒烟 38/40 + 1 CLI | `debian-package-compat.md` |
| **L2** systemd 健康 | `is-system-running`、失败 unit、启动时长 | systemd **257.13**，**running**，失败 unit **0/156**，启动 **3.416 s**（kernel 0.303 + userspace 3.112） | `debian-systemd-health.md` |
| **L3** syscall 一致性 | LTP 40 case 子集 | **34 pass / 6 fail / 0 skip**；6 个失败均逐条归因 | `debian-ltp-baseline.md` |
| **L4** 源码包构建 | guest 内 `dpkg-buildpackage` 产出 `.deb` | **3 包 3 ok / 0 fail**：`hello`、`coreutils` **首次通过**（575.99 s）、`python3-minimal`；内核 `66004cd6` | `debian-source-build.md`；`2026-10-25-l4-mknodat.md`；`2026-10-25-l4-coreutils-sig11.md` |
| **L5** 从零 rootfs | debootstrap minbase 三阶段 + chroot | **stages 3/3**；chroot/mount/umount/unshare 全 rc=0；`debian_version=13.7` | `debian-debootstrap.md` |
| **L6** 跨版本升级 | trixie→forky `full-upgrade` + 二次启动 | **rc=0**，**4825.30 s**；296 升级 / 45 新装；**341/341** 解包与配置；`allocation failed` **0**；二次启动 `forky/sid`、systemd running | `debian-full-upgrade.md`；`2026-10-25-ladder-final.md`；`evidence/2026-10-25-l6-final-zero-failures/` |
| **日常矩阵** | 桌面、应用、x11-apps、GTK、APT/剪贴板/音频、U 盘、休眠、输入法、稳定性、性能 | **10 域 ok**；GTK：pcmanfm `0x200002`、eog `0x400002` | `daily-use-summary.md`；`daily-use-matrix.md`；`2026-10-25-gtk-desktop-apps.md` |
| **压力** | 30 轮内存、30 轮编译、1 小时复合 | 内存 Δ **-0.983%**；编译 **30/30**（Δ -1.474%）；1 小时 Δ **-1.57%**、耗时比 **0.925** | `2026-10-25-stress-memory.md`；`2026-10-25-toolchain-stress.md`；`2026-10-25-hour-soak.md` |
| **GUI 重负载** | 10 并发 + 1.5 GiB I/O + 3 次休眠 | 无 panic；10 应用全部起窗；`dd/sha` rc=0；suspend **3/3**；Δ **-3.24%** | `2026-10-25-gui-stress.md`；`2026-10-25-ringbuf-nomemory-fix.md` |
| **真实项目** | zlib 1.3.1 编译 + 自带测试 | `configure/make/make test` rc=0；**3 项 `zlib test OK`** | `2026-10-25-real-workflow-build.md` |
| **开发者工作流** | git / C++ / pip / zlib | git 2.47.3，GitHub clone **2.1 s / 6.1 M**；g++ threads 输出 **1000**；pip six 1.17.0 / click（需 PEP 668 覆盖）；zlib 3 项 OK | `2026-10-25-dev-workflow.md` |

## 2. 七个关键修复的归因

1. **内核栈 128 页 → 32×4 页分段**（`1406bd76d` → `f67945641`）
   - 原因：L6 在 1.59 GiB 空闲时仍因 order-7 连续块缺失而 panic；
   - 做法：虚拟连续、物理分段（4 页段），guard page 语义不变；
   - 证据：`2026-10-24-frame-allocator-starvation.md`、`2026-10-25-ladder-final.md`。

2. **内核堆 16 MiB 启动预留连续池**（`6c3dfe0d0`）
   - 原因：heap 大槽是调用方 `Layout` 决定的一次连续分配，碎片化后 order-4
     灭绝；分段回退会触发 KVirtArea 映射路径的无 panic 挂起；
   - 做法：启动早期预留 16 MiB 连续物理内存，大槽 first-fit 位图分配；
   - 证据：同上；终验 heap 池峰值 2685/4096 页、0 次耗尽。

3. **ring-buffer 增长走同一预留池**（`99d4b116e`）
   - 原因：ring-buffer 增长请求 64 KiB（order-4）连续块，是最后一个高 order
     需求；
   - 做法：`ReservedPoolBuffer` 从池分配，保留 4 MiB（1024 页）给 heap；
   - 证据：L6 终验 `allocation failed` **0**；ring-buffer 池 33 命中/0 未命中。

4. **初始栈补 `AT_EXECFN`**（`f12f6388b`）
   - 原因：内核从未设置该 auxv 项；`envp[0]` 贴住 8 MiB 栈映射上界，
     GNU make 读 `environ[i][name_len]` 时越过 2 字节 → L4 coreutils
     `debian/rules clean` SIGSEGV；
   - 做法：把 exec 路径字符串写在 envp 之上并设置 `AT_EXECFN`；
   - 结果：signal 11 消失；随后 `coreutils` 因 root 构建的 configure 保护
     被 `FORCE_UNSAFE_CONFIGURE=1` 放行，L4 变为 **3/3**；
   - 证据：`2026-10-25-l4-coreutils-sig11.md`、`2026-10-25-l4-mknodat.md`。

5. **ring-buffer NoMemory 不再 panic**（`3b71c16c6`）
   - 原因：GUI 重负载（10 应用 + 2 GiB I/O）在旧 `prime` 内核触发
     `lib.rs:104` 的 unwrap panic；当前树虽已用“池 → 分段 → 减半”替换旧
     路径，但最后一页分配失败仍会 `panic!`；
   - 做法：S1 降级——`warn!` + `Storage::Empty`（capacity 0），push/pop 对
     0 容量短路；保留 `new() -> Self` 签名（S2 会波及 5 处构造调用）；
   - 结果：重建内核 `20582f4b` 复验，无 panic，10 应用、1.5 GiB I/O、
     suspend 3/3；
   - 证据：`2026-10-25-ringbuf-nomemory-fix.md`。

6. **tmpfs 根目录对齐 Linux 1777**（`3f934bf7a`）
   - 原因：`TmpFs` 复用 `RamFs` 的根 inode，模式硬编码 0755 且不解析
     `mode=`，`/dev/shm` 因此为 `drwxr-xr-x root root`；Claude Code 在
     guest 里跑 Python 多进程时 `multiprocessing.Lock()` 报
     `PermissionError: [Errno 13]`；
   - 做法：`RamFs::new_with_sb` 增加 `root_mode` 参数，tmpfs 传
     `mkmod!(a+rwx) | S_ISVTX`（1777），ramfs/rootfs 保持 0755，并新增
     `tmpfs_root_mode_is_1777` / `ramfs_root_mode_stays_0755` 两个 ktest；
   - 结果：修复内核 `548e5f0d`；`/dev/shm` = 1777，uid 1000 写入 rc=0，
     `Lock`/`Pool` 全通过；ktest 全量 `cargo osdk test` 退出码 0；
   - 证据：`2026-10-25-claude-obstacles.md`。

7. **GTK 桌面应用闭包与独立 toolchain 剖面**（`db20ab5ea`、`22e7aa049`）
   - 原因：`desktop-drm` 剖面已有 pcmanfm/GTK3/Xorg，但缺 `eog` 和日常门禁
     所需 `sudo`，且没有 amd64 串口登录面；
   - 做法：新增**独立** `desktop-drm-toolchain` 剖面（+eog/+sudo/build-essential，
     2 GiB 根），把 amd64 串口登录面复用到 desktop 分支，stage1 增加新卷标
     `ASTER_DRM_TOOLCH`；GUI 脚本补软件渲染环境、窗口 id 证据与 1.5 GiB I/O；
   - 结果：pcmanfm `0x200002`、eog `0x400002`；30/30 编译压力；
   - 证据：`2026-10-25-gtk-desktop-apps.md`、`2026-10-25-toolchain-stress.md`。

## 3. 已知限制（如实记录）

1. **L4 的当前状态是 3/3**：`coreutils` 已通过。对外文档里若看到“L4 2/3”，
   那是 `AT_EXECFN` 修复后、`FORCE_UNSAFE_CONFIGURE=1` 之前的中间状态。
   当前 L4 依赖该环境项（guest 以 root 构建；不是内核修复）。
2. **pip 需要 PEP 668 覆盖**：`python3 -m pip install six` 首次报
   `externally-managed-environment`；用 `--break-system-packages`（或 venv）
   后成功。属 Debian 镜像策略，不是 Asterinas 缺陷。
3. **磁盘/共享内存上限**：`/dev/shm` 上限 2 GiB（根模式已修为 1777，
   但 tmpfs 仍不解析 `mode=`/`size=` 等挂载选项）；根 ext2 空闲约
   300–400 MiB。
   单个 2 GiB 文件放不下，因此 GUI 压测用 **1.5 GiB**，1 小时复合负载用
   **1 GiB**；需要更大 I/O 的测试要给 guest 挂数据盘。
4. **guest DNS**：镜像内若为 `1.1.1.1`，经 slirp 不可解析；测试脚本在联网前
   改写为 **`10.0.2.3`**。builder 现在已对新镜像写 10.0.2.3。
5. **microvm 内核的 `dmesg`/syslog 未实现**（`read kernel buffer failed:
   Function not implemented`），因此脚本里的 `dmesg_count` 不能作为判据；
   panic 结论以串口日志为准。
6. **`xterm -e` 的 pty 问题**：GUI 压测中 xterm 窗口能起，但内部 `-e` 命令
   报 `open ttydev: Inappropriate ioctl for device`；属独立的 tty/pty 兼容性
   问题，未修复。
7. **日常矩阵的 10 域**是在历史镜像 `image18.ext2`（外部产物，不在仓库）上
   验证的；当前 toolchain 镜像只验证了桌面/应用/GTK 等子集。一键门禁在缺少
   矩阵镜像时把 daily 步标为 **SKIP**。
8. **旧 `prime` 内核**（`9e8ee5dd`）是修复前的构建；继续用它跑 GUI 压测会
   复现 ring-buffer panic。应从当前树重建日常内核。
9. `ring-buffer` 的最终兜底是**降级为容量 0**（`Storage::Empty`）并 `warn!`；
   调用方看到 `capacity() == 0` / `push() == None`，不再 panic，但该 buffer
   不可用。
10. **L3** 的 6 个 LTP 失败（fd 上限未强制 ×3、O_NOATIME ×1、`/proc/maps` ×1、
    测试 fd 复用 ×1）仍是已知基线。
11. **negative dentry 无淘汰**、**page cache 无压力回收**、**池大小是常量**
    （heap 16 MiB、ring-buffer 预留 4 MiB）；更小内存的 guest 需重新标定。
12. 1 小时复合负载实测 **3596.1 s**（59 分 56 秒；30 轮 ×120 s 的取整差 3.9 s）。

## 4. 复现命令

```sh
# L1-L6 的完整命令见 docs/porting/2026-10-25-ladder-final.md 的“可复现命令”。
K=$PWD/target/osdk/asterinas-osdk-bin.elf
I=$PWD/target/debian-x86/stage1/initramfs.cpio
R=$PWD/target/debian-x86-toolchain3/rootfs/debian-root.ext2

# 日常矩阵（需要矩阵镜像；外部产物）
make test_x86_daily_use X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/final-matrix/xwrap/image18.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm

# 开发者工作流（git/C++/pip/zlib）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/dev_workflow.sh \
make test_x86_daily_use X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I \
  X86_DEBIAN_XORG_BASE_ROOT=$R X86_DEBIAN_XORG_ACCEL=kvm \
  DAILY_USE_WORKSPACE=$PWD/target/dev-workflow2

# GUI 并发 + 1.5 GiB I/O + 3 次休眠
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/gui_stress.sh \
make test_x86_daily_use X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I \
  X86_DEBIAN_XORG_BASE_ROOT=$R X86_DEBIAN_XORG_ACCEL=kvm \
  DAILY_USE_WORKSPACE=$PWD/target/gui-fixed

# 内存/编译压测（10 轮，供一键门禁使用；默认 30 轮）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/stress_memory.sh STRESS_ROUNDS=10 \
make test_x86_daily_use X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I \
  X86_DEBIAN_XORG_BASE_ROOT=$R X86_DEBIAN_XORG_ACCEL=kvm \
  DAILY_USE_WORKSPACE=$PWD/target/stress-toolchain2

# 一键门禁（四步；缺矩阵镜像时 daily 为 SKIP）
tools/x86/debian/acceptance_gate.sh --kernel "$K" --initramfs "$I" \
  --work-root "$R"
```

## 5. 性能基线

| 指标 | 数值 | 来源 |
|---|---|---|
| 写入带宽 | **859 MB/s** | `2026-10-23-performance-baseline.md` |
| 读带宽（tmpfs，O_DIRECT） | **3.3 GB/s** | 同上 |
| GUI 启动（xclock） | **1.38 / 1.39 s** | 同上 |
| suspend → resume | **约 1.6–2.0 s** | 同上 |
| systemd 启动总时长 | **3.416 s**（kernel 0.303 + userspace 3.112） | `debian-systemd-health.md` |
| 启动到登录提示 | **≈3.3 s** | `2026-10-25-stress-memory.md` |

## 6. 一键门禁

`tools/x86/debian/acceptance_gate.sh` 依次执行：

1. `daily`：标准日常矩阵（需要矩阵镜像；缺失 → SKIP）；
2. `dev`：git/C++/pip/zlib（解析 `git_clone rc=0`、`run_out=1000`、
   `pip_six ... rc=0 import_rc=0`、`zlib ... result=yes`）；
3. `gui`：10 应用 + 1.5 GiB I/O + 3 次休眠（解析
   `DAILY_DESKTOP=ok`、`dd_rc=0 sha_rc=0`、`suspend_ok=3/3`、六个窗口类
   计数 ≥1，并要求串口无 panic 块）；
4. `stress`：10 轮内存 + 编译压力（解析
   `stress_failures python3=0/10 gcc=0/10 gcc_ok=10/10`、无 panic）。

退出码：任一**已执行**步骤 FAIL → 非 0；SKIP 不计失败但会在汇总表中显示。
每步的 `console.txt` / `host.log` 在 `target/acceptance/<step>/`。
