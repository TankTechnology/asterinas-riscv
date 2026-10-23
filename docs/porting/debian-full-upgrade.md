# Debian 跨版本升级（L6：trixie → forky）

## 结论摘要

- **升级路径在本快照上不可行**，但不是 apt/dpkg 的依赖或冲突问题：`apt-get
  update`（切到 forky）正常，`full-upgrade` 下载了 **341 个包**后开始解包，
  在第 **9 个**包 `linux-libc-dev` 处被 **ext4 日志事务上限（E2BIG）** 中止；
- 中止时实际**解包 8 个、配置 4 个**；其中 glibc 已从 trixie 的
  `2.41-12+deb13u4` 换成 forky 的 **`2.43-7`** 并配置成功；
- 中止后 `debian_version` 仍是 **13.7**（`base-files` 尚未升级），所以
  “变成 forky”这一步没有发生；
- **升级后的副本仍可启动**：二次启动后串口登录成功，
  `sudo systemctl is-system-running` = **`running`**；
- 归因：**磁盘**（ext4 日志单事务 253 块上限），与 L4 的 `dpkg-deb fsync`
  和 L5 的 `ldconfig` 是同一个缺口；最小复现见
  [`debian-debootstrap.md` 的「最小复现」](debian-debootstrap.md#最小复现)；
- 可用性评级：**不可用（kernel-blocked）**。源切换、下载、依赖解析全部
  正常；阻塞点在内核的文件系统事务编码，不在 Debian 的升级逻辑。

## 安全前置

| 项 | 值 |
|---|---|
| 基础镜像 | `target/systemd-health/daily.ext2` |
| 基础镜像 sha256 | `450ebe3bf2506c290902d55ae651ba796e67c0b9a846c528de34528f82a083e0` |
| 升级副本 | `target/debian-upgrade/daily.ext2` |
| 副本 sha256 | `450ebe3bf2506c290902d55ae651ba796e67c0b9a846c528de34528f82a083e0` |
| 复制耗时 | 0.45 s（`cp --sparse=always`） |
| 基础镜像是否被修改 | **否**（升级前后 sha 相同，所有写入只落在副本上） |

> 说明：L1–L5 文档引用的 `target/final-matrix/xwrap/image18.ext2` 已不在
> `target/` 中（本轮开始前被清理）。本轮改用同源的 L2 工作副本
> `target/systemd-health/daily.ext2`：trixie 13.7、`sources.list` 相同
> （仅 `trixie main`）、`e2fsck -fn` 干净、空闲 2.4 GiB。基础与副本的
> sha 都在 `target/debian-upgrade/upgrade/copy.txt` 里。

## 方法

- guest 探针：`tools/x86/debian/full_upgrade.sh`（注入到
  `/usr/local/bin/full-upgrade`），由 `daily_use_gate.sh --full-upgrade`
  在串口登录后以 root 执行；
- 五个阶段：

  | 阶段 | 在哪 | 动作 |
  |---|---|---|
  | copy | host | `cp --sparse=always` 基础镜像 → `target/debian-upgrade/daily.ext2`，记录双 sha |
  | source | guest | `sed -i s/trixie/forky/g`（`.list` 与 `.sources`） |
  | update | guest | `apt-get update -o Acquire::AllowReleaseInfoChange=true` |
  | upgrade | guest | `DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Lock::Timeout=300 full-upgrade`，上限 40 分钟 |
  | reboot | host | 对**同一个副本**再次启动，串口登录后执行 `cat /etc/debian_version` 与 `sudo systemctl is-system-running` |

- 串口协议：`UPG stage=<...> rc=<n> dur=<s>`、`UPG disk_free_kb=<n>`、
  `UPG err=<摘要>`、`UPG_DONE`；host 侧另加 `stage=copy` 与 `stage=reboot`；
- 磁盘护栏：每个 guest 阶段前后采样根文件系统；低于 **800 MiB**
  （819,200 KB）立即记 `disk` 并停止；
- 升级参数：`-o Dpkg::Options::=--force-confold`（无人值守，保留旧配置）；
- 失败后**不修复**副本，只做二次启动判定并如实记录。

## 环境

| 项 | 值 |
|---|---|
| 日期 | 2026-10-24 |
| 架构 / 加速 | x86_64 / KVM（4 vCPU、4 GiB） |
| 宿主 HEAD | `0d5b7260e`（L5 最小复现提交；L6 改动见本文件所在提交） |
| 内核 | `target/prime/kernels/x86.elf`，sha256 前缀 `9e8ee5dd544fa03a` |
| Stage1 initramfs | `target/debian-x86/stage1/initramfs.cpio`，sha256 前缀 `fb9c5b1765911cf0` |
| 基础镜像 | `target/systemd-health/daily.ext2`（见“安全前置”） |
| 起始发行版 | Debian trixie（13.7） |
| 目标发行版 | Debian forky（testing） |
| 镜像源 | `https://mirrors.tuna.tsinghua.edu.cn/debian` |
| 起始根文件系统空闲 | 2,308,936 KB（约 2.2 GiB） |
| 宿主磁盘空闲 | 约 21.1 GB |

## 结果（五阶段）

| 阶段 | rc | 分类 | 耗时 | 关键数据 |
|---|---|---|---|---|
| copy | **0** | ok | 0.45 s | base/copy sha 相同：`450ebe3b…a083e0` |
| source | **0** | ok | 0.08 s | `deb https://mirrors.tuna.tsinghua.edu.cn/debian forky main` |
| update | **0** | ok | 12.29 s | **297** 个包可升级 |
| upgrade | **100** | **磁盘** | 143.09 s | 计划 296 upgraded + 45 newly installed；下载 **341** 个包；实际解包 **8**、配置 **4**；在 `linux-libc-dev` 处中止 |
| reboot | **0** | ok | 8.85 s | 串口登录成功；`debian_version=13.7`；`systemctl is-system-running=running` |

升级中止时已完成的包（从 `/var/tmp/upgrade/upgrade.log`）：

```
Unpacking (8):
  rpcsvc-proto, libc-dev-bin, libcrypt1, libcrypt-dev, libc6-dev,
  libc6 (2.43-7, from forky), libc-gconv-modules-extra, libc-bin
Setting up (4):
  libcrypt1 (1:4.5.2+20251210-1), libc-gconv-modules-extra (2.43-7),
  libc6 (2.43-7), libc-bin (2.43-7)
```

关键错误原文（串口 `UPG log upgrade=` 与 `UPG err=`）：

```
dpkg: error processing archive /var/cache/apt/archives/linux-libc-dev_7.2.11-1_all.deb (--unpack):
unable to sync file /usr/include/asm-generic/auxvec.h.dpkg-new : Argument list too long
dpkg: unrecoverable fatal error, aborting:
unable to fsync updated status of linux-libc-dev: Argument list too long
E: Sub-process /usr/bin/dpkg returned an error code (2)
E: Unable to synchronize mmap - msync (7: Argument list too long)
```

升级后的副本（部分升级）：

- 串口登录：**成功**（client `user=True pass=True command=True done=True`）；
- systemd：**`running`**（`sudo systemctl is-system-running`）；
- `/etc/debian_version`：**13.7**（仍是 trixie；`base-files` 未升级）；
- 值得注意的是：forky 的 **glibc 2.43** 已经替换并配置成功，副本仍能启动
  到 systemd——说明 libc 本身在这版内核上可用，阻塞点不是 libc。

## 失败归因

| 阶段 | rc | 分类 | 证据 |
|---|---|---|---|
| upgrade | 100 | **磁盘**（ext4 日志事务上限） | `Unable to synchronize mmap - msync (7: Argument list too long)`；`unable to sync file …auxvec.h.dpkg-new : Argument list too long`；`unable to fsync updated status of linux-libc-dev: Argument list too long` |

不是以下类别：

- **dpkg 冲突**：没有 `trying to overwrite`、conffile 冲突或
  `dpkg: error: ... conflict`；中止前 4 个包的配置均成功；
- **依赖不满足**：`apt-get update` 后依赖解析完成，计划（296+45）已生成，
  341 个包全部下载成功；
- **syscall 缺失**：错误是 `E2BIG`（`Argument list too long`），来自文件
  系统写回，不是 `ENOSYS`；同轮没有 `Function not implemented`；
- **超时**：143.09 s，远低于 40 分钟上限；
- **启动失败**：副本二次启动成功（systemd running、串口可登录）。

根因与 L4/L5 相同：ext2/ext4 驱动的 JBD2 事务编码器每个 descriptor 最多
**253 个元数据块**（`kernel/core/src/fs/fs_impls/ext2/journal.rs:233-238`）。
`linux-libc-dev` 解包时 `dpkg` 的 `msync`/`fsync` 强制提交一个超过容量的
元数据集合，于是返回 E2BIG。`linux-libc-dev` 本身有大量头文件，正是
“一个包就制造出大量脏元数据”的典型。L5 已给出不跑 debootstrap 的最小
复现（12000 个空文件 + `ldconfig`）。

## 评级与结论

- **升级路径可用性：不可用（kernel-blocked）**；
- 分数意义上的评级：**源/网络/依赖 5/5，执行 1/5**——切换源、`apt-get
  update`、下载、依赖计算全部通过；一旦 dpkg 开始解包就触发文件系统
  事务上限；
- 由于升级在 `base-files` 之前中止，系统仍是 13.7，不能称为
  “升级到 forky”；但**部分升级的副本仍可启动**，说明没有把镜像弄成
  不可引导；
- 要让 full-upgrade 走完，必须解决 ext4 事务容量（内核改动，不在本轮
  纪律内），或者把 rootfs 放到 tmpfs 上执行升级——但 tmpfs 掉电即失，
  仍无法得到持久化的 forky 根文件系统。所以在本快照上，**跨版本升级
  不可行**。

## 复现方式

```sh
make test_x86_debian_full_upgrade \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/systemd-health/daily.ext2 \
  FULL_UPGRADE_WORKSPACE=$PWD/target/debian-upgrade
```

整轮墙钟约 3 分钟（含复制、两次启动）；升级阶段自身 143 s。

产物：`target/debian-upgrade/console.txt`（第一次启动全量串口）、
`reboot-console.txt`（二次启动）、`upgrade/stages.tsv`、`upgrade.json`、
`upgrade/copy.txt`（双 sha）。前两轮（含一次标记回显导致的假 reboot）
的原始记录保留在 `target/debian-upgrade/run1/`、`run2/`。

## 基线声明

- **基础镜像 sha256**：`450ebe3bf2506c290902d55ae651ba796e67c0b9a846c528de34528f82a083e0`
  （L2 工作副本；原 `image18.ext2` 已被清理）；
- **副本 sha256**：与基础相同（升级在副本上进行，基础未改）；
- **五阶段**：copy **0** / source **0** / update **0** / upgrade **100** /
  reboot **0**；
- **已知失败**：`upgrade`，`linux-libc-dev` 解包，`E2BIG`（ext4 日志）；
- **判定规则**：若内核修复事务容量后重跑，upgrade 应转为 rc=0，且
  reboot 的 `debian_version` 应变成 forky（14.x）；若 copy 的
  base/copy sha 不再相同，说明基础镜像被改，属于门禁故障。
