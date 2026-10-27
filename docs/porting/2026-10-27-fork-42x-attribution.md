# fork+exit 42× 归因：eager 的 O(RSS) COW 页表复制 + 固定的任务创建路径

日期：2026-10-27
内核：`64b028b0c`（比 A/B 初测内核多了两个 sockopt，fork 路径未变）
任务书：`/tmp/tp/FORK_42X.md`

## 0. 结论

- **fork 成本随 RSS 线性增长**：Asterinas 斜率 **390.6 µs/MB（1.53 µs/页）**，
  同一 QEMU、同一磁盘、同一静态二进制的 Linux 斜率 **15.9 µs/MB（0.062 µs/页）**，
  **每页成本相差 24.6×**。
  （拟合截距 A ≈ 2.81 ms / L ≈ 30 µs，但**不能**把它当固定开销：基础进程自身的
  ~1.9 MB RSS 也被算进截距；固定路径以 §3 的 `clone(CLONE_VM)`/`vfork` 为准。）
- 因此 42× 是**两项叠加**：
  1. **固定的进程创建路径**：`clone(CLONE_VM)`（不复制地址空间）在 Asterinas 上
     **949 µs**、vfork **663 µs**，而 Linux 只有 **21 µs / 18 µs**（32–44×）；
     小进程的 42× 主要由这一项贡献。
  2. **eager 的地址空间复制**：256 MB 时 `fork` 102.0 ms vs `clone(CLONE_VM)` 0.72 ms
     ⇒ **99.3% 花在页表/COW 复制**；且子进程**只 `_exit`、不碰任何页**也照付这个代价
     （任务 D 成立）。
- `/proc/syscall_time` nr=56 证实成本在 **clone 系统调用内部**：256 MB、25 次 fork，
  clone CPU 合计 2.699 s ⇒ **107.98 ms/次**，与 wall 106.0 ms 一致（waitpid/退出不是成本）。
- 倍数随 RSS **下降**（36.5× @1 MB → 24.9× @256 MB）：两侧都是 O(RSS)，Asterinas 的
  每页成本高 24.6×，而固定项高 32–44×（§3 实测），所以小进程倍数最大。

## 1. 方法（同盘同 QEMU，只换内核）

- 磁盘：`/var/tmp/ab2.ext2`（systemd 根盘，`lmbench-run.service` 在启动时运行 `/opt/sb-run.sh`）。
- 两侧跑**同一个静态二进制** `/opt/sbench2`（源码 `tools/x86/perf/sbench2.c`，
  `gcc -O2 -static`，1 020 424 字节），通过覆盖镜像里的 `/opt/sbench` 注入
  （原文件备份为 `/tmp/sbench.orig`，脚本备份 `/tmp/sb-run.orig`）。
- QEMU 参数除内核/控制台外完全相同（`-enable-kvm -cpu host -smp 4 -m 4G`,
  `virtio-blk` 根盘, `virtio-net`）：
  - A：`tools/x86/debian/boot_debian.sh --kernel target/osdk/asterinas-osdk-bin.elf
    --initramfs target/debian-x86/stage1/initramfs.cpio --root-image /var/tmp/ab2.ext2
    --accel kvm --loglevel 4`（`console=hvc0`）
  - B：`qemu-system-x86_64 … -kernel /usr/lib/modules/7.2.6-arch2-1/vmlinuz
    -initrd /tmp/initramfs-arch.img -append "console=ttyS0,115200 root=/dev/vda rw
    rootfstype=ext4 loglevel=4"`
- 注入方式（可复跑）：`debugfs -w -R "rm /opt/sbench"` + `write` + `sif … 0100755`，
  并用同样的方式把 `/opt/sb-run.sh` 换成「跑 `/opt/sbench`，若有 `/proc/syscall_time`
  再跑一次 `SB_ONLY=rss256 SB_N=20` 并 dump」的脚本。**测量结束后镜像已恢复原状**
  （`/opt/sbench`、`/opt/sb-run.sh` 均从备份写回，`e2fsck -fn` 干净）。
- 每个数据点 **N=200 次 fork+waitpid 的中位数**（前 5 次热身丢弃）；RSS 扫描前
  `mmap` 并**逐页写入**（`MADV_NOHUGEPAGE` 防止 Linux 侧塌成 THP），`VmRSS` 核对
  （1 MB→1.9 MB、256 MB→256.9 MB）。
- 测量问题（先说）：第一次 A 侧启动被一个**残留的旧 B 侧 QEMU**（`timeout 1200` 看守、
  早已停在 login）锁住镜像而失败；杀掉后 `e2fsck -fy` 修复并重跑。B 侧收集完也被
  SIGTERM 停止，`e2fsck -fy` 返回 1（有修复），A/B 两侧的磁盘状态因此**不完全对称**，
  但两者都在干净 fsck 之上启动。两侧各只跑了**一轮**，没有重复测量方差；同一侧的小进程 fork 在不同阶段测到 2.32 ms 与 3.48 ms，
  差约 50%——因此小于 ~1.5 ms 的差异不应解读为真实差别。

## 2. 任务 A：RSS 扫描（µs，N=200 中位数）

| fork 前 touch | Asterinas | Linux（同 QEMU/同盘） | 倍数 |
|---:|---:|---:|---:|
| 1 MB | 3 476.9 | 95.3 | **36.5×** |
| 32 MB | 15 549.7 | 532.5 | **29.2×** |
| 128 MB | 51 840.5 | 1 979.6 | **26.2×** |
| 256 MB | 103 246.1 | 4 144.5 | **24.9×** |
| 最小二乘斜率 | **390.6 µs/MB** | **15.9 µs/MB** | 24.6× |
| 最小二乘截距 | 2 813 µs | 30 µs | 不可比¹ |

¹ 截距含基础进程自身的 ~1.9 MB RSS（A ≈ 0.74 ms 正好占 A 截距的 26%，
Linux 的 ~30 µs 截距几乎全部来自这 1.9 MB）——固定创建路径请看 §3 的变体表。

形态判读（任务书的两选一）：**明确是「随 RSS 线性增长」**，且截距也高——
两项都要算，见 §4。原始行见 `evidence/2026-10-27-fork-42x/guest-A-rb.txt`。

本机 Linux 直跑同一二进制的对照（非 QEMU）：74.6 / 726.1 / 3 097.4 / 6 963.2 µs，
斜率 27 µs/MB —— 与 QEMU 内 Linux 同量级，说明对照没有被虚拟化扭曲。

## 3. 任务 B：三变体（µs，N=200 中位数）

| 变体 | RSS | Asterinas | Linux | 倍数 |
|---|---:|---:|---:|---:|
| `fork` | 0 | 2 317.8 | 73.0 | 31.8× |
| `clone(CLONE_VM\|SIGCHLD)` | 0 | **949.3** | 21.4 | **44.4×** |
| `vfork` | 0 | **662.7** | 18.2 | **36.4×** |
| `fork` | 256 MB | 101 977.8 | 3 932.3 | 25.9× |
| `clone(CLONE_VM\|SIGCHLD)` | 256 MB | **719.0** | 22.9 | 31.4× |
| `vfork` | 256 MB | **704.5** | 18.4 | 38.3× |

判读：

- `CLONE_VM` **不复制地址空间**：Asterinas 256 MB 时从 102.0 ms 掉到 **0.72 ms**
  （141×），且与 0 MB 的 0.95 ms 基本持平 ⇒ 地址空间那一侧的代价被完全消除。
- 但 `CLONE_VM`/`vfork` 仍有 **0.66–0.95 ms** 的固定成本（Linux 18–23 µs），
  **这一项单独就是 32–44×**。所以「42× 全是页表复制」不成立；小进程（无大映射）
  的 42× 主要是固定路径。

## 4. 阶段占比（任务 C）

测到的（本轮）：

| 阶段 | 依据 | 256 MB | 小进程（≈1.9 MB RSS） |
|---|---|---:|---:|
| `clone` 系统调用内部合计 | `/proc/syscall_time` nr=56：25 次 clone，cpu 2.699 s ⇒ 107.98 ms/次；wall 106.0 ms | **≈100%**（计数略高于 wall 属计时口径） | — |
| 固定创建路径（无地址空间复制） | `clone(CLONE_VM)` 实测 | **0.72 ms（0.7%）** | **0.95 ms（41%）** |
| 地址空间/COW 页表复制 | `fork − clone(CLONE_VM)` | **101.26 ms（99.3%）** | **1.37 ms（59%）** |

代码位置（未改任何 fork 语义）：

- `kernel/core/src/process/clone.rs`：`clone` 进程/线程分支；
- `kernel/core/src/vm/vmar/vmar_impls/fork.rs:18` `Vmar::fork_from`：逐 VMA 调
  `cow_copy_pt`；
- `kernel/core/src/vm/vmar/vmar_impls/fork.rs:80` `cow_copy_pt`：**在 fork 时逐页**
  做「源侧清 W 位（`:126` `protect_cur`）+ 目的页表 `map`（`:132`）」，最后一次性
  TLB flush（`:63-67`）。这就是 O(RSS) 的来源，**不是缺页时才复制**。

**未定位（不编）**：`cow_copy_pt` 内部「源侧 protect+TLB」与「目的 map」两段的
**本轮占比没有重测**。历史测量
（`2026-10-27-fork-clone-cost.md`，512 MB：固定 1.84 ms + 源侧 90.7 ms + 目的 180.3 ms，
2.08 µs/页）给出约 1:2；本轮斜率已降到 **1.53 µs/页**，与该文档记录的游标批量化优化
（`fork.rs:98-137`）一致，但两段的**新比例**未测。

## 5. 任务 D：复制确实发生在 fork 时（实锤）

- RSS 扫描里**子进程只有 `_exit(0)`，一页都不访问**；256 MB 仍付 **102 ms**
  （Linux 4.1 ms）。⇒ 代价在 fork 时付出，不是缺页时。
- 更直接：`/proc/syscall_time` 的 **clone(56) 自身 CPU = 107.98 ms/次**，
  与 wall 的 106 ms 一致 —— 即使把 waitpid/退出全扣掉，成本仍在 `clone` 里。

## 6. 归因结论与后续优先级

1. **大 RSS**：`cow_copy_pt` 的逐页复制（1.53 µs/页 vs Linux 0.062 µs/页）。
   路径已批量化；进一步要动的是「惰性页表复制 / 只读子树共享」，属设计层，需批准。
2. **小进程（本轮的 42×）**：固定创建路径 0.66–0.95 ms（Linux 18–23 µs）。
   下一步应在这个固定项里做**阶段计时**（clone 入口 → 任务/PCB 注册 → fd 表复制
   → 调度入队 → 返回用户态），本轮没有做（时间盒），因此**未定位到具体函数**。

## 7. 证据

- `evidence/2026-10-27-fork-42x/guest-A-rb.txt`：Asterinas 侧全部 `RB` 行 +
  `syscall_time` dump（含 `56 25 2699477227 0 2699477227 107979089`）
- `evidence/2026-10-27-fork-42x/guest-B-rb.txt`：Linux 侧全部 `RB` 行
- `tools/x86/perf/sbench2.c`：可复跑（`SB_N` 改样本数，`SB_ONLY=rss256` 单尺寸）
- 原始 console：`/tmp/ab2-A2.txt`、`/tmp/ab2-B2.txt`（主机临时文件，未入库）
