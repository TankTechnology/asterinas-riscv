# Debian 阶梯最终验收报告（L1–L6）

日期：2026-10-25　仓库 HEAD：`2b7dfe6f0`　终验内核：`e23120606e4d942f`

## 结论摘要

- **L1–L6 全部通过**（L4 按基线为 2/3，见“已知遗留”）。
- **L6 终验端到端零分配失败**：`allocation failed` **0** 条（含
  `size=65536`）、`segment allocation failed` **0**、`segmented fallback`
  **0**；`full-upgrade` **rc=0**，**4825.30 s**，341/341 解包与配置；
  二次启动 `forky/sid`、systemd running。
- 决定性修复有两处（外加一处收尾）：
  1. **内核栈 16 页 → 4 页分段**（虚拟连续、物理分段）；
  2. **内核堆 16 MiB 启动预留连续池**（heap 大槽全部走池）；
  3. 收尾：**ring-buffer 增长走同一预留池**（保留 4 MiB 给 heap），
     把最后 64 KiB 连续请求清零。
- 已排除：**slot 阶梯改造（方案 A）**——代码中不存在 64 KiB 档可降，
  大槽尺寸由调用方 `Layout` 决定；上一轮 +126 行其实已经是池。

## 逐级验收表

| 级别 | 判据 | 结果（关键数字） | 内核 | 证据 |
|---|---|---|---|---|
| **L1** 包生态 | popcon top-50 可安装且可冒烟 | 安装 **50/50**；smoke **31/50**（`smoke_ok=28` + `smoke_cli=3`）；探针 339.29 s，门禁墙钟 353 s | `9e8ee5dd544fa03a` | `target/debian-popcon/console.txt`；[`debian-package-compat.md`](debian-package-compat.md) |
| **L1b** 真实新装 | 基础镜像里没有的 40 包 | 安装 **40/40**（`pre=absent 40/40`）；smoke **38/40** + 1 CLI；`iptables` 因缺 `NETLINK_NETFILTER` 失败；364.03 s | `9e8ee5dd544fa03a` | `target/debian-popcon-new40/console.txt`；[`debian-package-compat.md`](debian-package-compat.md) |
| **L2** systemd 健康 | `is-system-running`、失败 unit 数、启动时间 | systemd **257.13**，**running**，失败 unit **0/156**，运行中 service **9**；启动 **3.416 s**（kernel 0.303 + userspace 3.112）；3 个关键 unit rc=0；boot error 3 条 | `9e8ee5dd544fa03a` | `target/systemd-health/console.txt`（`SYSD_DONE systemd=yes failed=0 running=9 units=156 jerr=3`）；[`debian-systemd-health.md`](debian-systemd-health.md) |
| **L3** syscall 一致性 | LTP 子集 40 case | **34 pass / 6 fail / 0 skip**（85%）；整轮 **179 s**；6 个失败均有源码级归因 | `9e8ee5dd544fa03a` | `target/ltp-final/console.txt`（`LTP summary total=40 pass=34 fail=6 skip=0`）；[`debian-ltp-baseline.md`](debian-ltp-baseline.md) |
| **L4** 源码包构建 | guest 内 `dpkg-buildpackage` 产出 `.deb` | 3 包 **2 ok / 1 fail**：`hello` 2 个 `.deb`（281.31 s）、`python3-minimal` 19 个 `.deb`（343.26 s）；`coreutils` 两次在 `debian/rules clean` 被 **signal 11**（156.52 s） | `e2312060`（2026-10-25 回归） | `target/debian-source-build/console.txt`；[`debian-source-build.md`](debian-source-build.md) |
| **L5** 从零 rootfs | debootstrap minbase 三阶段 + chroot | **stages=3 ok=3**（install 19.99 s、foreign 44.66 s、second 440.18 s）；chroot/mount/umount/unshare **全 rc=0**；`debian_version=13.7`；`/target` 133,124 KB | `e2312060`（2026-10-25 回归） | `target/debian-debootstrap/console.txt`；[`debian-debootstrap.md`](debian-debootstrap.md) |
| **L6** 跨版本升级 | trixie→forky `full-upgrade` 跑完并二次启动 | **rc=0**，**4825.30 s**；**296 upgraded / 45 newly installed / 0 not upgraded**；**341/341** 解包与配置；`allocation failed` **0**；二次启动 `forky/sid`、systemd **running**；根文件系统剩余 **1,064,400 KB** | `e2312060` | `target/debian-upgrade-final/console.txt`；[`evidence/2026-10-25-l6-final-zero-failures/`](evidence/2026-10-25-l6-final-zero-failures/)；[`debian-full-upgrade.md`](debian-full-upgrade.md) |

说明：L1–L3 的数字来自各自文档记录的基线轮（内核 `9e8ee5dd`）；
L4/L5 在最终内核 `e2312060` 上于 2026-10-25 重新回归，结果与基线一致；
L6 为最终内核上的终验。

## 归因一：内核栈分段（16 页 → 4 页）

**为什么需要**：内核栈原本一次申请 **128 个连续页（order 7，512 KiB）**。
L6 在 guest **3340.2 s**、仍有 **1,671,266,304 B** 空闲时
`spawn task failed: NoMemory`——不是总量不足，而是 buddy 里没有 order-7
连续块。把栈段降到 16 页（order 4）后，同样的空闲水平把 panic 推到
3459 s/3578 s，仍是 order-4 灭绝；再降到 **4 页（order 2）** 才越过。

**改了什么**：

- `ostd/src/task/kernel_stack.rs`：128 页拆成 **32 段 × 4 页**；
  虚拟地址连续、物理分段，guard page 语义与栈布局不变；
- 分配失败由 RAII 回滚（段数组提前返回即释放）；
- 提交：`1406bd76d`（8×16 页）→ `f67945641`（32×4 页）。

**证据**：`spawn task failed` 在 4 页段后消失；帧余量序列与各阶统计见
[`2026-10-24-frame-allocator-starvation.md`](2026-10-24-frame-allocator-starvation.md)。

## 归因二：内核堆 16 MiB 池 + ring-buffer 池

**为什么需要**：内核堆没有 64 KiB 档。slab 只到 2048 B；更大的布局是
按页取整的 `LargeSlot(size)`，尺寸由调用方 `Layout` 决定（实测
9–15 页 = 36–60 KiB）。碎片化后 order-4 灭绝，`alloc_large` 的连续申请
失败；分段回退能接住请求，但 heap 侧频繁的 KVirtArea 映射/释放会触发
无 panic 的挂起（3643/3885/4083 s 三次）。

**改了什么**：

- `ostd/src/mm/heap/slot.rs` + `ostd/src/lib.rs`：启动早期（`sync::init`
  后、`boot::init_after_heap` 前）预留 **16 MiB 连续物理内存**；
  heap 大槽全部 first-fit 位图分配，池尽才回退分段；
- ring-buffer 的 >4 页请求（64 KiB）也走该池，但**保留 1024 页（4 MiB）
  给 heap 大槽**（`ReservedPoolBuffer::alloc(pages, 1024)`），避免互相
  饿死；池不足时回退分段；
- 提交：`a9c0bd5fd`（8 MiB 池原型）→ `6c3dfe0d0`（16 MiB 启动池，L6
  首次跑完）→ `99d4b116e`（ring-buffer 走池，终验 0 失败）。

**证据**（终验）：heap 池峰值 **2685/4096 页**、**0** 次耗尽；
ring-buffer 池 **33 命中 / 0 未命中**；`allocation failed` **0**。

## 已排除的方案

| 方案 | 为什么排除 | 证据 |
|---|---|---|
| **slot 阶梯改造（方案 A）** | 代码中**没有 64 KiB 档**可降；大槽尺寸是调用方布局，缩小会破坏 `GlobalAlloc` 必须返回一个连续指针的语义。上一轮 +126 行其实是“预留池”而非阶梯改造；失败点从 `slot.rs:87` 移到 `:206`（分段回退调用点），次数不降，且 8 MiB 池耗尽后仍在 3642.9 s 挂起 | `2026-10-24-frame-allocator-starvation.md` 第六轮 |
| page cache shrinker 作为首选 | panic 时仍有 1.59 GiB 空闲，不是总量耗尽；shrink 救不了“有内存但拿不到连续块” | `2026-10-24-l6-h1-verification.md` |
| 跨 CPU 本地池归并作为首选 | 失败瞬间全局池也没有 order-4；归并不能凭空拼出连续块 | 同上 |

## 环境与方法

| 项 | 值 |
|---|---|
| 架构 / 加速 | x86_64 / KVM，4 vCPU、4 GiB |
| 启动方式 | `--scheme microvm`（vmm-direct，QEMU `-kernel` 直接加载 multiboot2 ELF） |
| 内核 | L1–L3 基线：`9e8ee5dd544fa03a`；L4/L5/L6 终验：`e23120606e4d942f`（HEAD `99d4b116e`） |
| Stage1 initramfs | `target/debian-x86/stage1/initramfs.cpio`（sha256 `fb9c5b17…`） |
| 根镜像 | L1–L5 原始基线：`image18.ext2`（`6941801d…`）；L4/L5/L6 终验：`target/systemd-health/daily.ext2`（`450ebe3b…a083e0`） |
| L6 副本 | `target/debian-upgrade-final/daily.ext2`（sha 与基础相同） |
| 耗时 | L1 探针 339.29 s；L1b 364.03 s；L2 启动 3.416 s；L3 179 s；L4 包时间合计约 781 s；L5 三阶段合计约 505 s；L6 **4825.30 s** |

## 已知遗留（如实记录）

1. **L4 `coreutils` signal 11**：`debian/rules clean` 阶段子进程崩溃，
   两次一致；已收敛到 `DPKG_EXPORT_BUILDFLAGS=1` 导出的递归
   `$(shell dpkg-buildflags)` 变量问题（见 `debian-source-build.md`）。
   这是**基线问题，与本轮内存修复无关**；L4 因此为 **2 ok / 1 fail**。
2. **L3 的 6 个 LTP 失败**：均为已归因的内核语义差异（fd 上限未强制 ×3、
   O_NOATIME 权限 ×1、`/proc/maps` ×1、测试 fd 复用 ×1），属基线。
3. **negative dentry 无淘汰**：L6 终验 46,643 条警告，串口 18 MB 级。
4. **page cache 无压力回收**：升级完成时约 1.4 GiB 空闲，余量不宽裕；
   在更小内存的 guest 上仍可能失败。
5. **池大小是常量**：heap 16 MiB、ring-buffer 预留 4 MiB；更小内存的
   guest 可能需要重新标定。

## 可复现命令

```sh
export K=$PWD/target/osdk/asterinas-osdk-bin.elf
export I=$PWD/target/debian-x86/stage1/initramfs.cpio
export R=$PWD/target/systemd-health/daily.ext2

make test_x86_debian_popcon         X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I X86_DEBIAN_XORG_BASE_ROOT=$R X86_DEBIAN_XORG_ACCEL=kvm
make test_x86_debian_popcon_new40   X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I X86_DEBIAN_XORG_BASE_ROOT=$R X86_DEBIAN_XORG_ACCEL=kvm
make test_x86_debian_systemd_health X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I X86_DEBIAN_XORG_BASE_ROOT=$R X86_DEBIAN_XORG_ACCEL=kvm
make test_x86_debian_ltp LTP_PHASE=run X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I X86_DEBIAN_XORG_BASE_ROOT=$R X86_DEBIAN_XORG_ACCEL=kvm
make test_x86_debian_source_build   X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I X86_DEBIAN_XORG_BASE_ROOT=$R X86_DEBIAN_XORG_ACCEL=kvm
make test_x86_debian_debootstrap    X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I X86_DEBIAN_XORG_BASE_ROOT=$R X86_DEBIAN_XORG_ACCEL=kvm
make test_x86_debian_full_upgrade   X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I X86_DEBIAN_XORG_BASE_ROOT=$R X86_DEBIAN_XORG_ACCEL=kvm
```

## 证据索引

- L1：`target/debian-popcon/console.txt`
- L1b：`target/debian-popcon-new40/console.txt`
- L2：`target/systemd-health/console.txt`
- L3：`target/ltp-final/console.txt`、`target/ltp-final-gate.log`
- L4：`target/debian-source-build/console.txt`
- L5：`target/debian-debootstrap/console.txt`
- L6：`target/debian-upgrade-final/console.txt`、
  [`evidence/2026-10-25-l6-final-zero-failures/`](evidence/2026-10-25-l6-final-zero-failures/)
- 归因长文：[`2026-10-24-frame-allocator-starvation.md`](2026-10-24-frame-allocator-starvation.md)、
  [`2026-10-24-l6-h1-verification.md`](2026-10-24-l6-h1-verification.md)、
  [`2026-10-24-kernel-stack-fragmentation-plan.md`](2026-10-24-kernel-stack-fragmentation-plan.md)
