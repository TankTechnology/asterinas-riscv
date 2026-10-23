# Debian 源码包构建（L4：dpkg-buildpackage）

## 结论摘要

- 三个包中 **两个成功产出 `.deb`**：
  - `hello` 2.10-5 —— 1 次尝试，**2 个**二进制包（含 dbgsym）；
  - `python3-minimal`（源码包 `python3-defaults` 3.13.5-1）—— 1 次尝试，
    **19 个**二进制包；
- `coreutils` 9.7-3 **两次都在 `debian/rules clean` 阶段被 SIGSEGV 杀死**
  （1.02 s / 0.97 s，两次一致），没有进入编译，未产出 `.deb`；
- 基线（不加任何 guest 适配）下 `hello` 也失败。本轮定位并规避了 **3 个内核
  缺口**，全部是 guest 侧适配，没有改内核：
  1. `splice(2)` 返回 ENOSYS，GNU grep 3.11 的管道排空路径只接受 EINVAL；
  2. SysV IPC 缺失，fakeroot 默认传输无法建立消息通道；
  3. ext4 日志单事务最多 253 个元数据块，大构建树的 `fsync()` 触发 E2BIG；
- `coreutils` 的崩溃已收敛到一条具体构造：它在 `debian/rules` 里设了
  `DPKG_EXPORT_BUILDFLAGS = 1`，dpkg 的 `buildflags.mk` 因此导出 22 个递归
  `$(shell dpkg-buildflags …)` 变量，make 在构造子进程环境时失控
  （SIGSEGV 或挂起）。`python3-defaults` 不设该开关，因此不受影响。
- 整轮墙钟约 **16 分钟**（含 `apt-get update` 与工具链安装）；单包最长
  382.59 s，均未触及 900 s 的单包上限。

## 方法

- guest 探针：`tools/x86/debian/dpkg_build.sh`（注入到
  `/usr/local/bin/dpkg-build`），由 `daily_use_gate.sh --source-build`
  在串口登录后以 root 执行；
- 每个包依次执行：

  ```sh
  apt-get build-dep -y <pkg>     # 装 Build-Depends
  apt-get source <pkg>            # 取源码并解包
  dpkg-buildpackage -b -us -uc    # 只构建二进制包，不签名
  ```

  构建参数为 `DEB_BUILD_OPTIONS="nocheck parallel=4"`（目标是产物，不是包
  自带测试；4 个 vCPU）；
- 串口协议（每行前缀 `DPKGB `）：

  | 行 | 含义 |
  |---|---|
  | `DPKGB pkg=<n> start name=<pkg>` | 开始一个包 |
  | `DPKGB pkg=<n> stage=<builddeps\|source\|build> rc=<n> dur=<s> attempt=<k>` | 每个阶段的 rc 与耗时 |
  | `DPKGB pkg=<n> artifact=<deb>` | 产物文件名 |
  | `DPKGB pkg=<n> err=<摘要>` | 失败摘要（单 token） |
  | `DPKGB pkg=<n> evidence=<日志行>` | 去掉 make/dpkg 包装行后的日志尾部 |
  | `DPKGB pkg=<n> done name=<pkg> result=<ok\|fail\|timeout\|disk> attempts=<k> dur=<s>` | 包级结果 |
  | `DPKGB env <key>=<value>` / `DPKGB setup step=<n> rc=<n>` | 环境与准备步骤 |
  | `DPKGB summary total=<n> ok=<n> fail=<n>` / `DPKGB_DONE` | 收尾 |

- 重试与超时：每包最多 **2 次**尝试，**整包 15 分钟**预算（两次共享）。
  超时立即结束该包、不再重试；其他失败在预算内重试；
- 源码索引：基础镜像只有 `deb` 行，探针自动追加匹配的 `deb-src` 并
  `apt-get update`（rc=0）；`dpkg-buildpackage`/`dpkg-source`/`fakeroot`
  缺失时先 `apt-get install -y dpkg-dev build-essential fakeroot`（rc=0）；
- 构建树放在 `/tmp`（2 GiB tmpfs），日志与产物放 `/var/tmp`（根 ext2）。
  原因见下文“环境缺口 3”；
- 产物复制到 `/var/tmp/dpkg-artifacts/`，文件名写入 `packages.tsv`。

### guest 侧适配（shim）

均以 `/var/tmp/dpkg-shims/` 注入 PATH，不修改内核、不修改 dpkg 包文件：

| shim | 作用 |
|---|---|
| `grep` | stdout 是 `/dev/null` 时改用普通文件承接，避免 GNU grep 走 `splice()` 排空路径 |
| `fakeroot` | `exec "$@"`：gate 以真实 uid 0 运行，fakeroot 的“伪造 root”语义已由真实 root 满足 |

## 环境

| 项 | 值 |
|---|---|
| 日期 | 2026-10-24 |
| 架构 / 加速 | x86_64 / KVM（4 vCPU、4 GiB） |
| 宿主 HEAD | `3432ed5ba`（L3 提交；L4 改动见本文件所在提交） |
| 内核 | `target/prime/kernels/x86.elf`，sha256 前缀 `9e8ee5dd544fa03a` |
| Stage1 initramfs | `target/debian-x86/stage1/initramfs.cpio`，sha256 前缀 `fb9c5b1765911cf0` |
| 根镜像 | `target/final-matrix/xwrap/image18.ext2`，sha256 前缀 `6941801d07c8c893`；4 GiB ext4（`has_journal`、`extent`），启动时根文件系统可用约 2.24 GiB |
| 发行版 | Debian trixie（13.7） |
| `gcc` | `gcc (Debian 14.2.0-19) 14.2.0` |
| `dpkg` | `1.22.22` |
| `dpkg-buildpackage --version` | `Debian dpkg-buildpackage version 1.22.22.` |
| `fakeroot` | `1.37.1.1` |
| GNU make | `4.4.1` |
| 构建目录 | `/tmp`（tmpfs，2,085,216 KB） |
| 日志 / 产物 | `/var/tmp`（根 ext2） |

## 结果

`build-deps`、`source`、`build` 三列取成功的那次尝试（失败包取最后一次）。

| 包 | 源码包 / 版本 | build-deps | source | build | 产物 | 尝试 | 总耗时 | 结果 |
|---|---|---|---|---|---|---|---|---|
| `hello` | `hello` 2.10-5 | 0 / 229.51 s | 0 / 6.63 s | 0 / 122.04 s | 2 个 `.deb` | 1 | 359.94 s | **ok** |
| `coreutils` | `coreutils` 9.7-3 | 0 / 152.17 s | 0 / 11.60 s | **255 / 1.02 s** | 0 | 2 | 190.87 s | **fail** |
| `python3-minimal` | `python3-defaults` 3.13.5-1 | 0 / 343.08 s | 0 / 6.57 s | 0 / 29.41 s | 19 个 `.deb` | 1 | 382.59 s | **ok** |

`coreutils` 的重试：build-deps 1.62 s（已满足）、source 11.83 s、build
0.97 s 后再次 SIGSEGV。

产物清单：

- `hello`：`hello_2.10-5_amd64.deb`、`hello-dbgsym_2.10-5_amd64.deb`；
- `python3-defaults`（19 个）：

  ```
  idle_3.13.5-1_all.deb
  libpython3-all-dbg_3.13.5-1_amd64.deb
  libpython3-all-dev_3.13.5-1_amd64.deb
  libpython3-dbg_3.13.5-1_amd64.deb
  libpython3-dev_3.13.5-1_amd64.deb
  libpython3-stdlib_3.13.5-1_amd64.deb
  python3_3.13.5-1_amd64.deb
  python3-all_3.13.5-1_amd64.deb
  python3-all-dbg_3.13.5-1_amd64.deb
  python3-all-dev_3.13.5-1_amd64.deb
  python3-all-venv_3.13.5-1_amd64.deb
  python3-dbg_3.13.5-1_amd64.deb
  python3-dev_3.13.5-1_amd64.deb
  python3-doc_3.13.5-1_all.deb
  python3-examples_3.13.5-1_all.deb
  python3-full_3.13.5-1_amd64.deb
  python3-minimal_3.13.5-1_amd64.deb
  python3-nopie_3.13.5-1_amd64.deb
  python3-venv_3.13.5-1_amd64.deb
  ```

> 注意：`python3-minimal` 在 trixie 的源码包是 `python3-defaults`，构建链路
> 很短（29.41 s），并不是“长 configure/make”；真正长的 `python3.13`
> 源码包不在本轮三包范围内。

## 失败归因

### 最终三包

| 包 | 阶段 | rc | 分类 | 证据 |
|---|---|---|---|---|
| `coreutils` | build（`debian/rules clean`） | 255（子进程 SIGSEGV=11） | **其他**（工具链崩溃） | `dpkg-buildpackage: error: debian/rules clean subprocess was killed by signal 11`；日志在 `debian/rules clean` 之后没有任何 `dh`/`make` 输出；两次尝试一致 |

### 构建基线暴露、已被 shim 规避的环境缺口

这些是**未加 shim 时**观察到的失败，最终轮已不再出现：

| 包 / 场景 | 阶段 | rc | 分类 | 一行证据 | 处置 |
|---|---|---|---|---|---|
| `hello`（基线） | build（`dh_autoreconf`） | 2 | **syscall 缺失** | `autom4te: error: need GNU m4 1.4 or later`，同轮内核日志 `Unimplemented syscall number: 275`（`splice`） | grep shim |
| `hello`（基线，构建树在 ext2） | build（`dh_builddeb`） | 2 | **磁盘**（日志事务） | `dpkg-deb: error: unable to sync file`；内核 `ext2: metadata transaction failed: E2BIG … ext4 journal transaction is too large` | 构建树移到 tmpfs |
| `fakeroot` 自检 | setup | 1 | **syscall 缺失** | `fakeroot, while creating message channels: Function not implemented … lack of SYSV IPC support` | root-exec shim |

三个缺口的机制：

1. **`splice(2)` 未实现（ENOSYS）**
   - GNU grep 3.11 在“stdin 是管道且 stdout 是 `/dev/null`”时用
     `splice()` 排空输入（`grep-3.11/src/grep.c:1772-1790`），且只把
     `EINVAL` 当作“回退到 `read()`”的信号；
   - `autom4te` 的 m4 版本检查正是
     `m4 --help </dev/null 2>&1 | grep reload-state >/dev/null`
     （`/usr/bin/autom4te:96-97`），ENOSYS 被 grep 报成读错误，autom4te
     误判 m4 过旧；
   - 自检对照：`m4 --version | grep …`（stdout 不是 `/dev/null`）rc=0；
     加 `>/dev/null` 复现；去掉 `export`/换 bash 均无关。

2. **SysV IPC 缺失**
   - fakeroot 默认 transport 创建消息通道时得到 ENOSYS；`fakeroot-tcp`
     的 `listen()` 在该内核上返回 EINVAL（`fakeroot daemon: listen:
     Invalid argument`）；
   - guest 是真实 uid 0，shim 直接 `exec "$@"`，语义等价于 fakeroot
     在这里唯一要做的事（让 root 拥有的文件显示为 root 所有）。

3. **ext4 日志单事务上限**
   - ext4 驱动每个事务只编码一个 JBD2 descriptor，容量
     `(4096-12-16-8)/16 = 253` 个元数据块
     （`kernel/core/src/fs/fs_impls/ext2/journal.rs:233-238`）；
   - 完整构建树留下超过 253 个脏元数据块，`dpkg-deb` 对成品 `.deb` 的
     `fsync()`（`dpkg-1.22.22/src/deb/build.c:746`）触发 E2BIG；
   - tmpfs 的 `sync` 是 no-op，把源码树/构建树放 `/tmp` 后同一个 `fsync`
     成功；产物再 `cp` 回 `/var/tmp`（普通写，不带 fsync）。

### `coreutils` SIGSEGV 的根因证据

`debian/rules` 只有 `dpkg-buildpackage` 的 info 行、`debian/rules clean`
和 “killed by signal 11”，没有 `make`/`dh` 的任何输出，说明崩溃发生在
`debian/rules` 启动后的 makefile 解析/环境构造阶段。用 make 片段逐个
复现（每次一个独立 makefile，`timeout 60`）：

| makefile | rc | 结论 |
|---|---|---|
| `include /usr/share/dpkg/architecture.mk` | **139**（SIGSEGV） | 单独 include 就会崩 |
| 同上，加 `SHELL = /bin/bash` | 139 | 与 shell 无关 |
| 同上，但先 `eval $(dpkg-architecture -s)` 导出 `DEB_*` | **0** | `?=` 变量已被环境定义时不再生成递归变量，**不崩** |
| `architecture.mk` 去掉每一行的 `export` | **0** | `export` 是关键 |
| `include /usr/share/dpkg/buildflags.mk` | 0 | 不导出时正常 |
| `buildflags.mk` + `DPKG_EXPORT_BUILDFLAGS = 1` | **139** | **coreutils 的精确构造** |
| `include /usr/share/dpkg/default.mk` | 139 | 它包含 buildflags/architecture |
| 36 个 `export A?=$(shell echo …)` 的合成 makefile | **124**（30 s 超时） | 导出递归变量会让 make 持续派生进程 |
| `dpkg-architecture -qDEB_HOST_ARCH` 等直接调用 | 0 | Perl 辅助程序本身正常 |

结论：

- `dpkg-buildpackage` 会把 `DEB_*` 放进环境，所以 `architecture.mk` 的
  `?=` 不生成递归变量；`python3-defaults` 只 include 了它，因此不受影响；
- `coreutils` 额外设置了 `DPKG_EXPORT_BUILDFLAGS = 1`。`buildflags.mk`
  没有 `?=` 保护，它导出的 22 个变量在展开时调用
  `dpkg-buildflags --get …`；GNU make 在本内核上为子进程构造环境时对这类
  “导出 + 递归 + `$(shell)`”变量失控，表现为 SIGSEGV（真实构建）或持续
  派生进程直到超时（合成用例）；
- 直接调用 `dpkg-architecture`/`dpkg-buildflags` 全部 rc=0，且没有定位到
  单一未实现的 syscall，因此归入 **其他（工具链崩溃）**，而不是
  `syscall 缺失`。

## 复现方式

```sh
# 三包完整测量（约 16 分钟）
make test_x86_debian_source_build \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/final-matrix/xwrap/image18.ext2 \
  SOURCE_BUILD_WORKSPACE=$PWD/target/debian-source-build

# 只跑一个包
make test_x86_debian_source_build ... SOURCE_BUILD_PACKAGES=hello

# 工具链自检（m4/grep/autom4te/fakeroot/make 片段），不构建
make test_x86_debian_source_build ... SOURCE_BUILD_SELFTEST=1
```

产物：`target/debian-source-build/console.txt`（串口全量）、
`source-build/packages.tsv`、`stages.tsv`、`env.tsv`、
`source-build.json`。guest 内的日志在 `/var/tmp/dpkg-logs/`，产物在
`/var/tmp/dpkg-artifacts/`（随 root 镜像保留）。

## 基线声明

- **结果**：3 包 **2 ok / 1 fail**；
- **成功**：`hello` 2.10-5（2 个 `.deb`）、`python3-defaults` 3.13.5-1
  （19 个 `.deb`）；
- **已知失败**：`coreutils` 9.7-3，`debian/rules clean`，signal 11；
- **判定规则**：后续运行若 `hello` 或 `python3-minimal` 不再产出 `.deb`，
  或 `coreutils` 的失败阶段/信号发生变化，即视为退化；若内核修好了
  `splice`/SysV IPC/ext4 事务上限或 make 的导出展开，对应 shim/处置
  可以移除，并更新本基线（记日期、内核 sha、diff 原因）。
