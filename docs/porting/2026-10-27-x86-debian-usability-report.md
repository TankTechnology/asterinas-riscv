# x86 Debian 可用性报告（2026-10-27 收口）

日期：2026-10-27
内核：`4a5f18528`（本报告写作时的 HEAD；各节数字标注其自身的测量内核/提交）
镜像：`target/systemd-health/daily.ext2`（Debian 13 trixie，L4/L5/L6 与 7 域矩阵同一基座）
范围：只收口今夜（2026-10-26 晚—10-27 凌晨）的六次落地与既有验收，不含 RISC-V 线。
本文所有数字都可追到 commit 或 `文件:行号`；没有实测支撑的条目写「未验证」。

## 1. 可用性现状

### 1.1 L1–L6 阶梯（验收基线）

| 级别 | 关键数字 | 内核 | 出处 |
|---|---|---|---|
| L1 包生态 | 安装 **50/50**；smoke **31/50**（`smoke_ok=28` + `smoke_cli=3`）；339.29 s | `9e8ee5dd` | `docs/porting/2026-10-25-ladder-final.md:24` |
| L1b 新装 40 包 | 安装 **40/40**；smoke **38/40**；364.03 s | `9e8ee5dd` | 同文件 `:25` |
| L2 systemd | 257.13 **running**；失败 unit **0/156**；启动 **3.416 s** | `9e8ee5dd` | `:26` |
| L3 LTP 子集 | **34 pass / 6 fail / 0 skip**（85%）；179 s | `9e8ee5dd` | `:27` |
| L4 源码包构建 | **3 ok / 0 fail**，23 个 `.deb`（详见 §4） | `17f5f904c` 的复核内核 `86cf3f5d0` | `:28`；`docs/porting/2026-10-27-l4-coreutils-closure.md:103-117` |
| L5 从零 rootfs | **stages=3 ok=3**；chroot/mount/umount/unshare 全 rc=0 | `e2312060` | `:29` |
| L6 跨版本升级 | **rc=0**，4825.30 s；341/341 解包配置；`allocation failed=0` | `e2312060` | `:30` |

### 1.2 七域日常矩阵

最近一次（与本报告同一内核 `4a5f18528`）：

```
DESKTOP ok  APP ok  APT ok  CLIPBOARD ok  AUDIO ok  USB ok  SUSPEND ok
DAILY_DONE=ok   panic=0   allocation failed=0
```

- 原始表：`docs/porting/evidence/2026-10-27-ptrace-attach/matrix.txt:1-9`
- 检查项定义：`tools/x86/debian/daily_use_checklist_regress.sh:43`（APT）、`:60`（DESKTOP）、
  `:67`（APP）、`:82`（CLIPBOARD），AUDIO/USB/SUSPEND 同文件后段。
- `INPUT_METHOD=fail`（`engine--no-text`）**不在**这 7 域要求内，是既有缺口。

## 2. 应用兼容（30 个 CLI）

测量内核 `86cf3f5d0`，文档提交 `e74d914e8`。

| 指标 | 数值 | 出处 |
|---|---|---|
| 安装成功 | **30/30** | `docs/porting/2026-10-27-app-compat-matrix.md:15` |
| 版本命令成功 | **30/30** | `:16` |
| 功能命令成功 | **29/30** | `:17` |
| 工具输出中出现 ENOSYS/EPERM/EINVAL | **0 次** | `:19` |

两项异常：

- **`htop`：脚手架问题**。矩阵里 `script` 分配的 pty 是 0×0，htop 无法渲染；补
  `stty rows 40 cols 120` 后输出 1447 字节并画出 `CPU` 行（`2026-10-27-app-compat-matrix.md` §2.1）。
- **`dig`：真实缺陷**。`dig` 显式 `bind(INADDR_ANY)` 后超时，而 `getent`/`curl`/`nc` 正常；
  根因是通配 bind 被钉在 loopback（`kernel/core/src/net/socket/ip/common.rs:24-33,50-56`），
  详见 §6.1 与 `e74d914e8`。

## 3. 今夜落地的修复

| 修复 | commit | 关键数字 | 代码位置 | 证据 |
|---|---|---|---|---|
| 页缓存顺序预读 | `f8091faef` | 冷读 **132.53 → 29.74 µs/页（−77.6%）**；整文件 **5.311 → 1.192 s**；吞吐 **31 → 138 MB/s**；Firefox 冷启动 **4979 → 4535 ms（−8.9%）** | `kernel/core/src/vm/page_cache/vmo/mod.rs:223`（`READAHEAD_PAGES=32`）、`:1100`（`read_page_with_prefetch`） | `docs/porting/2026-10-27-page-cache-readahead.md:34-37,70` |
| `/proc` 时间单位 | `0afe3b2ee` | utime/stime 曾 **10×**（997/0 vs Linux 99/0）；修后 **99/0**，getpid 用例 9/90 vs Linux 52/47 | `kernel/core/src/fs/fs_impls/procfs/pid/task/stat.rs:353`（`USER_HZ=100`）、`:188-189` | `docs/porting/2026-10-27-calibrate.md:19-21`；`2026-10-27-land-fix.md:6-15` |
| VmSize 单位 | `ac4e536d3` | **466,423,808 kB → 455,492 kB（445 MiB）**，与 Linux 447 MB 同量级 | `kernel/core/src/fs/fs_impls/procfs/pid/task/status.rs:158-159` | `docs/porting/2026-10-27-wait-account.md:6-14` |
| `/proc/net/*6` 表头 | `6d06f9ab2` | `lsof` 的 `unsupported format` 告警 **2 → 0**；`lsof -i` 能解析真实 UDP 行 | `kernel/core/src/fs/fs_impls/procfs/pid/task/net.rs:320-327` | `docs/porting/2026-10-27-procnet-header.md` §4 |
| `PTRACE_ATTACH/DETACH` | `4a5f18528` | `strace -p` 从 **0 行 → 39 行 syscall**；attach 后目标 `state=t`、detach 后 `state=S` 并 exit 0 | `kernel/core/src/syscall/ptrace.rs:48`（ATTACH）、`:65`（DETACH） | `docs/porting/2026-10-27-ptrace-attach.md` §3 |

五个提交都已包含在矩阵/验证里：预读的 Firefox 探针轮、时间口径的 CAL1/CAL2、VmSize 的
7 域矩阵、表头与 ptrace 各自的 7 域矩阵（最近一次见 §1.2）。

## 4. 结论修正：L4 的「2/3」是假阴性

- 所谓失败是 gnulib `m4/mknod.m4` 的检测程序在 `geteuid()==0` 时**先返回 99**，
  从不调用 `mknod()`（`evidence/2026-10-27-l4-coreutils/gnulib-conftest-linux.txt`，m4 第 39-44 行）；Linux 以 root 构建结果相同
  （宿主机实测 uid 1000 → rc=0，`unshare -r` → rc=99）。
- 我们的 FIFO 语义**不要求 root**：`kernel/core/src/syscall/mknod.rs:17-66`；
  `fs/vfs/path/dentry.rs:647-662` 只对字符/块设备要求 `CAP_MKNOD`，`NamedPipe` 在
  `:649` 直接放行。guest 内 uid 1000 建 FIFO 实测 rc=0。
- **当前内核重跑（`86cf3f5d0`）**：三包全量 **3 ok / 0 fail，23 个 `.deb`，panic=0**
  （hello 274.31 s、coreutils 572.36 s、python3-minimal 370.86 s）。
  证据：`docs/porting/2026-10-27-l4-coreutils-closure.md:103-117`、提交 `17f5f904c`。

即：L4 自 2026-10-25 起就是 3/3，早期的 2/3 是**环境性假阴性**（root 构建守卫），
不是内核缺陷。

## 5. 已排除的方向（负结果）

| 方向 | 结论与数字 | 出处 |
|---|---|---|
| 范围 TLB flush 合并 | madvise 90.7 → **82.8 µs（−8.6%）**，远未达 <40 µs，已 revert；瓶颈是范围走页表而非 flush 条数 | `2026-10-27-tlb-range.md:9,38` |
| 跳过空区间 | **证伪**：8 MiB/0 页的 madvise 只走 **0.26 个 fragment/调用**，不是逐页遍历 | `2026-10-27-skip-empty.md:1,9` |
| 叶子层跳过 | PTE 表**存在且被逐项走**：0 页仍约 **936 次 PTE 步进/调用** | `2026-10-27-leaf-skip.md:1,15` |
| per-mapping 驻留计数 | 实现正确（随机 1000 次 `mismatches=0`），但快路径**无效**：0 页 madvise **78.96 µs**（基线 78.60） | `2026-10-27-mm-wrapup.md:10,43` |
| clone COW | 真值 **2.36 ms/次**（26 次 61.3 ms）；旧的 56.5 ms/次是探针污染 | `2026-10-27-clone-cow.md:9-14` |
| 缺页 | 8 s 窗口 **61.3 ms**，约为主进程内核 CPU 的 **5%** | `2026-10-27-pagefault.md:1,9` |
| 调度/时钟 tick | 五段合计 **135.9 ms**，tick **125.4 ms**；自证不通过 | `2026-10-27-sched-account.md:9-10` |
| poll / futex | 干净账里 **掉出 top36**（poll <1.8 ms、futex 未进榜）；clone 从 850 ms 修正为 **24.6 ms** | `2026-10-27-clean-syscall.md:11-13` |

**统一结论**：Firefox 8 s 墙钟里主进程 CPU 只有 **150 ms（1.9%）**；
等待集中在 `wait4`（8.045 s，探针自身的会话等待）与 **`epoll_wait`（2.721 s，事件循环）**，
`futex` 仅 **0.021 s**。所以内核 CPU 侧已不是主瓶颈
（`2026-10-27-wait-account.md:22-24,35`）。

## 6. 两个待批修复与下一步

### 6.1 通配 bind 被钉在 loopback（dig 失败根因）

- 现状：`bind(0.0.0.0)` 走 `default_bind_iface()`，固定选 **loopback**；
  发送按路由走 eth0，回来的应答投递不到挂在 lo 的 socket。
  `kernel/core/src/net/socket/ip/common.rs:24-33`、`:50-56`，FIXME 在 `:47-49`。
- 判别证据：`/proc/net/udp` 显示 dig 的 socket `0.0.0.0:8005 → 10.0.2.3:53 st=01`、
  rx_queue 为空；Python 显式 bind 的 4 个变体全部超时，隐式 bind 立即有应答
  （`docs/porting/2026-10-27-wildcard-bind-fix.md` §1、§2）。
- 方案：**A 多 iface 绑定 + 每包路由选 egress**（推荐，需新抽象，已写设计）；
  **B 全局 UDP socket 表**（更大重构）。
- 复现脚本已就绪：`tools/x86/net/wildcard_bind_check.sh`（改前 bind=FAIL、nobind=PASS，
  `check_rc=1`）。

### 6.2 冷邻居首包丢失

- 现状：邻居未解析时 `EtherIface::dispatch` **丢弃数据包**、改用同一缓冲发 ARP：
  `kernel/libs/aster-bigtcp/src/iface/phy/ether.rs:137-147`、`:231-247`（注释 `:234-236`）；
  ARP 表 `BTreeMap`，无待发队列、无过期（`:32`、`:172`）。
- 实测：冷态单发丢 1 个；连发 5 个**丢 4 个**；ARP 在 **3.2 ms** 内完成，而被丢的首包
  4.9 ms 时仍无应答；TCP 由 RTO 掩盖（冷态 `connect` **0.744 s**）。Linux 同场景 5/5 不丢
  （`docs/porting/2026-10-27-cold-neighbor-loss.md` §1）。
- 方案：**A 有界待发队列 + ARP 完成后再发**（推荐，对齐 Linux）；**B 发送路径同步解析 +
  短超时**（最小改动，但引入阻塞）。

### 6.3 下一步

1. 等批准后按各自文档的 C1–C3 实施并跑 7 域矩阵；
2. 两个方案都涉及网络核心路径，必须单变量、失败 revert；
3. 可继续扩应用矩阵（当前 30 项样本），优先覆盖"会显式 bind 通配地址"的守护进程类工具。

**未验证项**：通配 bind 的多 iface 实现、冷邻居队列实现、以及两者对 Firefox 冷启动的
实际收益，均**未实现、未验证**。

## 7. 可直接引用的一段话

> x86_64 上已通过 Debian L1–L6：常用包 50/50 可装、LTP 34/40、源码包 3/3 出 23 个 .deb；
> 30 个日常 CLI 中 29 个可用。今夜修复 5 个真实缺陷（页缓存预读、/proc 时间单位、VmSize、
> /proc/net 表头、strace -p）。剩余 2 个已知缺陷（通配 bind、冷邻居首包）已定位并附方案，
> 待批准。

（正文 186 字符，≤200。）
