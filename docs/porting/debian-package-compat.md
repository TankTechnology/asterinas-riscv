# Debian 包生态兼容性（L1：popcon top-50）

## 方法

- 清单：`tools/x86/debian/popcon_top50.txt`（popcon 前列，排除
  `libreoffice*`/`firefox*`/`thunderbird*`/`texlive*`/`chromium*`）；
- 探测脚本：`tools/x86/debian/popcon_packages.sh`（guest 内逐包
  `apt-get install --no-install-recommends`，记录 rc/耗时/尝试次数/smoke/
  安装前状态，串口打
  `POPCON pkg=… rc=… smoke=… dur=… tries=… pre=… err=…`，JSON 落到
  `/tmp/popcon-result.json`）；`--list PATH` 可参数化清单（L1b 用）；
- 注入：`daily_use_gate.sh --popcon` 在复制出的镜像里写入
  `/usr/local/bin/popcon-packages` 与 `/usr/local/share/popcon_top50.txt`，
  然后用与日常门禁相同的 QEMU 启动链、串口登录和 host 侧解析；
- 每包最多 2 次尝试（`POPCON_ATTEMPTS`）；`DEBIAN_FRONTEND=noninteractive`，
  apt 带 `DPkg::Lock::Timeout=120` 与 `Acquire::Retries=2`；任何子进程
  `stdin` 都接 `/dev/null`，包清单走独立 fd 3；
- smoke 口径：
  - `ok`：同名可执行程序能跑 `--version`/`--help`/`-V` 且退出 0；
  - `cli`：程序成功执行并拒绝了参数（无 `--version` 接口），例如 `cron`；
  - `lib`：包本身不提供同名可执行程序（库/数据/工具集，如 `coreutils`）；
  - `no`：程序存在但既没跑通、也没有用法/参数错误输出（真正的可执行失败）；
  - `na`：安装失败，无从 smoke。

## 运行方式（一条命令）

```sh
make test_x86_debian_popcon \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/final-matrix/xwrap/image18.ext2
```

产物：`target/debian-popcon/popcon.tsv`（逐包结果）、
`popcon-result.json`、`console.txt`（串口全量）。可用
`POPCON_WORKSPACE`/`POPCON_TIMEOUT`（默认 3600 s）覆盖输出目录与截止时间。

## 环境

| 项 | 值 |
|---|---|
| 日期 | 2026-10-23 |
| 架构 / 加速 | x86_64 / KVM（4 vCPU、4 GiB） |
| 宿主 HEAD | `97925edc8` |
| 内核 | `target/prime/kernels/x86.elf`（2026-10-23 构建，sha256 前缀 `9e8ee5dd544fa03a`） |
| 根镜像 | `target/final-matrix/xwrap/image18.ext2` |
| 发行版 / 源 | Debian trixie，`https://mirrors.tuna.tsinghua.edu.cn/debian` |

## 结果

| 指标 | 数值 |
|---|---|
| 安装成功 | **50 / 50**（100%） |
| 直接可执行 smoke 可用 | **31 / 50**（`ok` 28 + `cli` 3） |
| 库/数据包（无同名可执行） | 19 / 50 |
| smoke 真失败（`no`） | **0 / 50** |
| 安装失败（rc≠0） | **0 / 50** |
| 需要重试的包 | 0（50 个包全部 `tries=1`） |
| guest 内包循环总耗时 | **339.29 s**（逐包累计 321.51 s） |
| 整轮门禁墙钟 | **353 s**（含复制镜像、注入、启动、登录、关机） |
| 平均每包 | ≈ 6.8 s |

最慢的 8 个包（其余均 < 7 s）：

| 包 | 秒 | 说明 |
|---|---|---|
| vim | 157.00 | 首次下载 + 安装；镜像原先未装 vim |
| man-db | 49.92 | 包含 man 索引重建 |
| rsync | 17.62 | |
| e2fsprogs | 16.96 | |
| apt-utils | 13.92 | |
| logrotate | 13.57 | |
| wget | 13.56 | |
| gettext-base | 6.93 | |

## 失败归因

本轮 **没有 rc≠0 的包**，因此没有需要归因的安装失败：

| 包 | rc | 归因 |
|---|---|---|
| — | — | 无安装失败（50/50 rc=0） |

归因口径（供后续轮次使用）：`syscall 缺失`（EINVAL/ENOSYS/功能未实现）、
`返回码错`（工具返回非预期错误码）、`依赖链`（无候选包/依赖不可满足）、
`磁盘/时间`（ENOSPC 或超过每包 300 s 上限）、`其他`。

### smoke 非 `ok` 项的逐条归因

3 个 `cli`——程序可执行，只是不提供 `--version` 接口，**不是兼容性缺陷**：

| 包 | rc | smoke | 归因 |
|---|---|---|---|
| cron | 0 | cli | 探测口径：`cron: invalid option -- '-'`，二进制正常执行 |
| debconf | 0 | cli | 探测口径：`Unknown option: version`，二进制正常执行 |
| debianutils | 0 | cli | 探测口径：`Illegal option -- '-'`，二进制正常执行 |

19 个 `lib`——包内没有与包同名的可执行文件（数据/库/工具集），安装即可用：

`apt-utils`、`base-files`、`base-passwd`、`bsdutils`、`ca-certificates`、
`coreutils`、`dbus`、`diffutils`、`e2fsprogs`、`findutils`、
`init-system-helpers`、`iproute2`、`man-db`、`ncurses-base`、`procps`、
`sysvinit-utils`、`tzdata`、`util-linux`、`vim-common`。

> 注意：`coreutils`/`iproute2`/`findutils` 等实际提供的命令（`ls`、`ip`、
> `find`）在其它包里另有覆盖；本探测只验证“包按名安装成功”，命令级验证由
> 日常门禁的 apt/应用域承担。

## 过程中发现的测试夹具问题

第一次重跑只记录到 48/50，`vim-common` 与 `wget` 凭空消失。根因是 `vim`
的安装后脚本继承了 `while read` 的 stdin，把包清单剩余两行读走了。修复：
包清单改用 fd 3，所有 apt/smoke 子进程 `stdin` 接 `/dev/null`。这不是内核
缺陷，但会让结果静默少包，已在脚本里固化。

## L1b：新装包（40）

### 为什么需要 L1b

L1 的 top-50 里 **40 个在基础镜像中已经安装**，只有 10 个需要真正下载安装，
所以 50/50 主要证明的是“apt 重装/已装路径”。L1b 专门测“基础镜像里没有、
必须下载并首次配置”的包。

### 清单怎么来的

- 数据源：Debian popcon `by_inst`；基础镜像的已装清单取自
  `/var/lib/dpkg/status`（即 `dpkg -l` 的数据源）；
- 在 popcon **top-600** 中，基础镜像已装 **257 个 → 排除**；
- 剩余 343 个未装包中，挑出 **40 个带真实命令**的包（跳过纯库/数据包），
  排名范围 42–1946；
- 清单：`tools/x86/debian/popcon_new40.txt`；guest 内安装前用
  `dpkg-query -W -f='${Status}'` 逐包预检，串口行带 `pre=absent|installed`。

### 运行方式（一条命令）

```sh
make test_x86_debian_popcon_new40 \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/final-matrix/xwrap/image18.ext2
```

产物：`target/debian-popcon-new40/popcon.tsv`、`popcon-result.json`、
`console.txt`。

### 结果

| 指标 | 数值 |
|---|---|
| 包总数 | 40 |
| 安装前 **pre=absent** | **40 / 40**（0 个已装） |
| 安装成功 | **40 / 40**（100%，全部 `tries=1`） |
| 命令级 smoke `ok` | **38 / 40**（95%） |
| 命令已执行但无该参数（`cli`） | 1 / 40 |
| 命令执行失败（`no`） | **1 / 40**（iptables） |
| 命令可用率（`ok` + `cli`） | **39 / 40**（97.5%） |
| guest 包循环总耗时 | **364.03 s** |
| 整轮门禁墙钟 | **376 s** |

命令级 smoke 不是 `--version` 摆设：每个包跑自己的真实命令（`jq -e`、
`printf '1+1' | bc | grep -qx 2`、`zip -v`、`tree --version`、`dig -v`、
`lvm version`、`mkfs.fat --version` 等），见脚本里的 `smoke_probe_cmd()`。

最慢的 5 个包：`lvm2` 44.34 s、`gnupg` 22.47 s、`aspell` 20.08 s、
`bind9-dnsutils` 17.07 s、`iptables` 17.01 s。

### 失败归因表

| 包 | 现象 | 归因分类 | 说明 |
|---|---|---|---|
| iptables | 安装 rc=0；`iptables --version` 失败：`Failed to initialize nft: Address family not supported by protocol` | **syscall / 内核能力缺失** | 内核只实现 `NETLINK_ROUTE`、`NETLINK_SOCK_DIAG`、`NETLINK_KOBJECT_UEVENT` 三个标准 netlink 族；`NETLINK_NETFILTER` 落在 `EAFNOSUPPORT` 分支（`kernel/core/src/syscall/socket.rs:82-86`）。iptables 1.8 默认 nft 后端，启动即开 netfilter netlink socket，因此命令不可用。 |
| dosfstools | 安装 rc=0；`mkfs.fat --version` 退出非 0：`unrecognized option '--version'` | 其他（CLI 接口差异，非失败） | 二进制正常执行并解析 argv，只是没有 `--version`；与 L1 的 cron/debconf/debianutils 同类。 |

其余 38 个包的命令级 smoke 全部 `ok`。

### 与 L1 的对比

| 维度 | L1（top-50） | L1b（新装 40） |
|---|---|---|
| 基础镜像已装 | **40 / 50** | **0 / 40** |
| 真实新装 | 10 | **40** |
| 安装成功 | 50 / 50 | 40 / 40 |
| 命令级失败 | 0（只做 `--version` 探测） | **1**（iptables） |
| guest 包循环 | 339.29 s | 364.03 s |
| 整轮墙钟 | 353 s | 376 s |

**结论**：L1 的 50/50 确实主要覆盖“重装路径”；L1b 把真实新装提高到
40 个包，并首次用命令级 smoke 暴露了 **netfilter netlink 缺失**这个
L1 测不到的内核 gap。两轮共同说明 Debian 包管理/下载/解包/配置链路完整，
短板在具体内核子系统（netfilter），不在 apt/dpkg。

## 结论

- **安装链路**：L1 50/50、L1b 40/40，100% 成功，无 syscall/返回码/
  依赖链/磁盘类阻塞；
- **命令可用性**：L1 31/50 有同名可执行程序且全部可运行；L1b 39/40
  可执行（38 严格 ok + 1 CLI 参数差异），唯一真实失败是 `iptables`
  依赖的 `NETLINK_NETFILTER` 未实现；
- **下一步**：若要让 iptables/nftables 可用，需要在内核补 netfilter
  netlink 族与 nf_tables 子系统（不在本轮“只加测试与文档”的范围内）。
