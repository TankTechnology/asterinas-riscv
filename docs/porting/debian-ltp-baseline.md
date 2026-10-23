# Debian syscall 一致性基线（L3：LTP 子集）

## 结论摘要

- 路线：**B —— guest 内下载 LTP 源码，只构建 8 个 syscall 目录，运行 40 个 case**；
- 结果：**34 pass / 6 fail / 0 skip**（通过率 85.0%），整轮 179 s，未触发 25 分钟上限；
- 6 个失败全部有源码级归因：**5 个为内核语义差异**（fd 上限未强制执行 ×3、O_NOATIME
  权限检查缺失 ×1、`/proc/maps` 权限展示 ×1），**1 个为 LTP 测试自身限制**
  （close 后 fd 号被复用）；
- 本轮**完整完成**，没有"部分完成"。

## 环境

| 项 | 值 |
|---|---|
| 日期 | 2026-10-23 |
| 架构 / 加速 | x86_64 / KVM（4 vCPU、4 GiB） |
| 宿主 HEAD | `0d5f0798f`（L2 提交；L3 改动提交见本文件所在提交） |
| 内核 | `target/prime/kernels/x86.elf`（sha256 前缀 `9e8ee5dd544fa03a`） |
| 根镜像 | `target/final-matrix/xwrap/image18.ext2` |
| 发行版 | Debian trixie（gcc 14.2.0-19、GNU Make 4.4.1） |
| LTP | release **20260930**（`ltp-full-20260930.tar.bz2`，4,058,442 B） |

## 第一阶段：可行性判定（约 22 s，gate 结果为 `LTPCHK` 行）

| 检查项 | 结果 |
|---|---|
| `apt-cache policy ltp` | **absent** |
| `apt-cache policy ltp-full` | **absent** |
| apt 是否有可安装候选 | **no** |
| GitHub release 可达性 | **HTTP 200**，4,058,442 B |
| `gcc` | gcc (Debian 14.2.0-19) 14.2.0 |
| `make` | GNU Make 4.4.1 |
| `autoconf` | 未安装 |
| `curl` / `tar` | curl 8.14.1 / GNU tar 1.35 |
| `bzip2` | 未安装 |
| `pkg-config` | 未安装 |
| `/tmp` 可用空间 | 2,085,216 KB（≈2.0 GiB） |
| 是否已有 `ltp` 命令 | no |

### 路线选择与理由

| 路线 | 判定 | 理由 |
|---|---|---|
| **A：apt 装 `ltp`** | **不可行** | trixie main 中没有 `ltp`/`ltp-full` 包（guest 与宿主索引一致）。 |
| **B：guest 内下载源码，只编子集** | **采用** | GitHub 200 且体积仅 3.9 MiB；gcc/make/libc6-dev 已装；release tarball 自带 `configure`，不需要 autoconf。唯一缺少的 `bzip2`、`pkg-config` 各用一次 apt 补装（均 rc=0）。 |
| C：host 编译后注入 | 不需要 | B 的工具链与网络前提全部满足，无需承担跨 glibc 版本风险。 |

B 的第一次尝试在 `configure` 阶段失败：`configure: error: pkg-config not
found`。补装 `pkg-config` 后同一配方成功（`configure rc=0`，全部构建 rc=0）；
这是依赖补齐，不是路线失败。

## 子集与构建

只构建并运行以下 8 个目录中的 40 个 case（没有 `make` 全量）：

| suite | case 数 | case |
|---|---|---|
| getpid | 1 | getpid01 |
| close | 2 | close01–close02 |
| fstat | 2 | fstat02、fstat03 |
| read | 4 | read01–read04 |
| write | 6 | write01–write06 |
| pipe | 9 | pipe01–pipe09 |
| open | 8 | open01–open04、open06–open09 |
| mmap | 8 | mmap01–mmap06、mmap08、mmap09 |
| **合计** | **40** | |

构建步骤与结果：

| 步骤 | rc |
|---|---|
| download（4,058,442 B） | 0 |
| extract（`.tar.bz2`，运行时装 `bzip2`） | 0 |
| `./configure --without-numa --without-tirpc --without-modules` | 0 |
| `make -C include` | 0 |
| `make -C lib` | 0 |
| `make -C testcases/lib` | 0 |
| `make -C testcases/kernel/syscalls/{getpid,close,fstat,read,write,pipe,open,mmap}` | 全部 0 |

构建耗时包含在整轮 179 s 内。

## 结果

| 指标 | 数值 |
|---|---|
| 执行 case | **40 / 40** |
| 通过（rc=0） | **34** |
| 失败（rc≠0 且非 TCONF） | **6** |
| 跳过（TCONF / 二进制缺失） | **0** |
| 通过率 | **85.0%** |
| 整轮墙钟 | **179 s**（上限 25 min，未触发） |
| case 自身累计耗时 | 3.20 s |

两轮完整运行（第一轮与带诊断的最终轮）结果一致：34/6/0，说明该快照稳定。

### 逐 case 结果

`ok` = pass；失败项的 reason 原样保留。

| case | suite | rc | status | dur(s) |
|---|---|---|---|---|
| getpid01 | getpid | 0 | pass | 0.44 |
| close01 | close | 0 | pass | 0.04 |
| close02 | close | 0 | pass | 0.05 |
| fstat02 | fstat | 0 | pass | 0.03 |
| fstat03 | fstat | 0 | pass | 0.04 |
| read01 | read | 0 | pass | 0.03 |
| read02 | read | 0 | pass | 0.03 |
| read03 | read | 0 | pass | 0.03 |
| read04 | read | 0 | pass | 0.03 |
| write01 | write | 0 | pass | 0.12 |
| write02 | write | 0 | pass | 0.03 |
| write03 | write | 0 | pass | 0.03 |
| write04 | write | 0 | pass | 0.03 |
| write05 | write | 0 | pass | 0.04 |
| write06 | write | 0 | pass | 0.03 |
| pipe01 | pipe | 0 | pass | 0.04 |
| pipe02 | pipe | 0 | pass | 0.04 |
| pipe03 | pipe | 0 | pass | 0.03 |
| pipe04 | pipe | 0 | pass | 0.03 |
| pipe05 | pipe | 0 | pass | 0.03 |
| **pipe06** | pipe | 1 | **fail** | 0.06 |
| **pipe07** | pipe | 2 | **fail** | 1.27 |
| pipe08 | pipe | 0 | pass | 0.04 |
| pipe09 | pipe | 0 | pass | 0.03 |
| open01 | open | 0 | pass | 0.04 |
| **open02** | open | 1 | **fail** | 0.03 |
| open03 | open | 0 | pass | 0.04 |
| **open04** | open | 1 | **fail** | 0.09 |
| open06 | open | 0 | pass | 0.05 |
| open07 | open | 0 | pass | 0.03 |
| open08 | open | 0 | pass | 0.04 |
| open09 | open | 0 | pass | 0.04 |
| mmap01 | mmap | 0 | pass | 0.03 |
| mmap02 | mmap | 0 | pass | 0.03 |
| mmap03 | mmap | 0 | pass | 0.03 |
| **mmap04** | mmap | 1 | **fail** | 0.06 |
| mmap05 | mmap | 0 | pass | 0.03 |
| mmap06 | mmap | 0 | pass | 0.03 |
| **mmap08** | mmap | 1 | **fail** | 0.03 |
| mmap09 | mmap | 0 | pass | 0.03 |

## 失败归因

| 包/case | rc | TFAIL/TBROK 证据 | 归因分类 | 根因 |
|---|---|---|---|---|
| pipe06 | 1 | `pipe06.c:39: TFAIL: pipe_fds succeeded`（期望 EMFILE） | **语义不符** | `RLIMIT_NOFILE` 未在 fd 分配路径强制执行：`FileTable::insert`（`kernel/core/src/fs/file/file_table.rs:190-194`）不做上限检查，`EMFILE` 在 `kernel/core/src` 里只有定义（`error.rs:32`），没有任何返回点；只有 `dup2` 检查了 RLIMIT。 |
| pipe07 | 2 | `tst_test.c:1973: TBROK: Test killed by SIGSEGV` | **语义不符**（同一根因） | 测试按 `getdtablesize()` 预分配 `max_fds` 个 int；内核不限制 fd 数，`pipe()` 持续成功直到写出数组边界 → SIGSEGV。**不是内核崩溃**。 |
| open04 | 1 | `open04.c:52: TFAIL: open(fname, O_RDWR\|O_CREAT, 0777) succeeded`（期望 EMFILE） | **语义不符**（同一根因） | setup 按 RLIMIT 打开满 fd 后，run 的下一次 `open()` 仍成功（fd 表可超过 1024）。 |
| open02 | 1 | `open02.c:47: TFAIL: open(unprivileged O_RDONLY \| O_NOATIME) succeeded`（期望 EPERM） | **语义不符**（权限检查缺失） | 内核只把 `O_NOATIME` 当作状态位（`kernel/core/src/fs/file/file_attr/status_flags.rs:22`），open 路径没有实现“文件属主或 CAP_FOWNER”检查；测试已用 `setresuid` 切到 `nobody`（setresuid 本身可用）。 |
| mmap04 | 1 | `mmap04.c:68: TFAIL: mapping permissions in /proc mismatched, expected: -w-p, found: rw-p` | **语义不符**（展示口径） | `mmap` 在 x86/riscv 上把 `PROT_WRITE` 补成 READ（`kernel/core/src/syscall/mmap.rs:62-69`，页面无法只写），VMA 保存有效权限，`/proc/maps` 直接渲染成 `rw-p`；Linux 展示的是请求权限。**功能可用，仅展示差异**。 |
| mmap08 | 1 | `mmap08.c:28: TFAIL: ... expected EBADF: EINVAL 22` | **测试自身限制** | LTP 在 setup 里 `close(fd)`，run 阶段才 `mmap(fd)`；期间 fd 号被框架复用，mmap 落到另一个不可映射对象上返回 EINVAL。最小复现（close 后立即 mmap）返回 **EBADF 9**，内核路径正确。 |

分类统计：

| 分类 | 数量 | case |
|---|---|---|
| syscall 缺失（ENOSYS） | 0 | — |
| 语义不符 | **5** | pipe06、pipe07、open04、open02、mmap04 |
| 权限/环境 | 0 | — |
| 测试自身限制 | **1** | mmap08 |
| 其他 | 0 | — |

> 注：`ltp.tsv` 的 `category` 列是串口文本的启发式分类（pipe07 显示
> `test-crash`、mmap08 显示 `semantics-mismatch`）；上表是结合源码与最小
> 诊断后的最终归因——pipe07 的崩溃源于 fd 上限缺失，mmap08 的最小内核
> 路径返回 EBADF，属测试自身 fd 复用。

### guest 内最小诊断（证据）

探针在跑完 40 个 case 后，用 gcc 编译一个独立诊断程序（不与任何 case
共享进程状态），串口输出：

```
LTP diag build rc=0
LTP diag noatime rc=0 errno=0
LTP diag mmap_closed result=fail errno=9
LTP diag maps_perm=rw-p
LTP diag fdlimit opened=20000 errno=0
```

逐条对应：

- `fdlimit opened=20000 errno=0`：在 `RLIMIT_NOFILE=1024` 的 guest 里连续
  `open("/dev/null")` 20000 次仍全部成功 → 证实 fd 上限未执行（pipe06/open04
  失败、pipe07 SIGSEGV 的共同根因）；
- `noatime rc=0 errno=0`：`seteuid(65534)` 后打开 root 拥有的 0644 文件并带
  `O_NOATIME`，仍成功 → 证实 O_NOATIME 权限检查缺失；
- `mmap_closed result=fail errno=9`：close 后立即 mmap 返回 EBADF → 内核在
  mmap08 的最小路径上是正确的，LTP 的 EINVAL 来自 fd 复用；
- `maps_perm=rw-p`：`PROT_WRITE`-only 匿名映射在 `/proc/self/maps` 显示
  `rw-p` → 证实 mmap04 的展示差异。

## 基线声明

这是后续“不退化”比较的基线：

- **LTP release**：20260930；
- **子集**：40 个 case（8 个 suite，见上表）；
- **内核**：`x86.elf` sha256 前缀 `9e8ee5dd544fa03a`；
- **基线结果**：pass **34** / fail **6** / skip **0**；
- **已知失败集合**：`pipe06`、`pipe07`、`open04`、`open02`、`mmap04`、`mmap08`；
- **判定规则**：后续运行若 pass < 34、出现新的失败 case，或耗时超过 25 分钟，
  即视为退化；若修复了某个内核 gap，对应 case 转入 pass，并更新本基线
  （记日期、内核 sha、diff 原因）。

## 复现方式

```sh
# 第一阶段：可行性判定（约 20 s）
make test_x86_debian_ltp LTP_PHASE=check \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/final-matrix/xwrap/image18.ext2

# 第二阶段：构建并运行 40 个 case（约 3 分钟）
make test_x86_debian_ltp LTP_PHASE=run \
  LTP_WORKSPACE=$PWD/target/ltp-final \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/final-matrix/xwrap/image18.ext2
```

产物：`target/ltp-final/console.txt`（串口全量）、
`target/ltp-final/ltp/ltp.tsv`、`ltp.json`；可行性证据保留在
`target/ltp/ltp/ltp-check.txt`。
