# Debian 从零 rootfs（L5：debootstrap + chroot）

## 结论摘要

- **debootstrap 在 guest 内可用**：`debootstrap 1.0.141` 的三个阶段
  （install / `--foreign` / second-stage）都能在本内核上执行完成；
- **chroot 可用**：对刚解出的 rootfs 直接执行
  `chroot /tmp/target /bin/sh -c 'echo DBST_CHROOT_OK'`，rc=0 且输出
  `DBST_CHROOT_OK`；`mount -t proc`、`umount`、`unshare -m` 探针也全部
  rc=0；
- **目标放在 tmpfs 时三阶段全过**：`/tmp/target`（2 GiB tmpfs）上
  install 18.29 s、foreign 38.05 s、second-stage 24.28 s，得到完整的
  trixie rootfs（`/etc/debian_version` = 13.7，`/bin/sh -> dash`，
  清理前 218,820 KB）；
- **按任务书把 `/target` 放在根 ext4 上时，second-stage 失败**：
  `/sbin/ldconfig: Writing of cache data failed: Argument list too long`
  （E2BIG）。内核日志是
  `ext2: metadata transaction failed … "ext4 journal transaction is too large"`，
  与 L4 的 `dpkg-deb` fsync 是同一个缺口；关机后 `/target` 整棵树没有落盘；
- 结论：**能否 debootstrap：能；chroot 是否可用：可用**。唯一阻塞是把
  rootfs 持久化到根 ext4——这受 ext4 日志单事务 253 块上限限制，与
  chroot/debootstrap 本身无关。

## 方法

- guest 探针：`tools/x86/debian/debootstrap_rootfs.sh`（注入到
  `/usr/local/bin/debootstrap-rootfs`），由
  `daily_use_gate.sh --debootstrap` 在串口登录后以 root 执行；
- 三个阶段：

  ```sh
  apt-get install -y debootstrap wget     # install
  debootstrap --foreign --variant=minbase trixie /target MIRROR
  chroot /target /debootstrap/debootstrap --second-stage
  ```

  基础镜像只有 `curl`，而 debootstrap 的 http(s) 下载器用 **wget**，因此
  install 阶段同时装 `wget`；
- 每个阶段前后各打一条 `DBST disk_free_kb=<n> stage=<name>
  phase=<before|after>`，取的是 **target 所在文件系统** 的空闲空间；
  target 还不存在时取根文件系统。空闲低于 300 MB（307,200 KB）立即记
  `disk` 止损；
- second-stage 之前先做三个能力探针：

  | 探针 | 命令 |
  |---|---|
  | chroot | `chroot /target /bin/sh -c 'echo DBST_CHROOT_OK'` |
  | mount/umount | `mount -t proc proc /target/proc`、`umount /target/proc` |
  | unshare | `unshare -m /bin/true` |

- 串口协议：

  | 行 | 含义 |
  |---|---|
  | `DBST stage=<install\|foreign\|second> rc=<n> dur=<s>` | 阶段结果 |
  | `DBST disk_free_kb=<n> stage=<n> phase=<before\|after\|after_cleanup>` | 磁盘采样 |
  | `DBST chroot_probe rc=<n> dur=<s> out=<…>` | chroot 能力 |
  | `DBST mount_probe` / `umount_probe` / `unshare_probe rc=<n> out=<…>` | 其他 syscall |
  | `DBST err=<一行摘要>` / `DBST log second=…` / `DBST log target=…` | 失败摘要与日志尾巴 |
  | `DBST verify debian_version=<v> sh=<path> ok=<0\|1>` | rootfs 完整性 |
  | `DBST summary install_rc=… foreign_rc=… second_rc=…` / `DBST_DONE` | 收尾 |

- 目标目录可由 `DEBOOTSTRAP_TARGET` 指定；默认 `/target`（根 ext2），
  `/tmp/target` 则落在 tmpfs。探针会先 `rm -rf` 目标目录，因此只接受
  绝对路径且拒绝 `/`；
- 结束后删除目标里的 apt 下载缓存与包列表，再记录最终空闲与 rootfs 大小。

## 环境

| 项 | 值 |
|---|---|
| 日期 | 2026-10-24 |
| 架构 / 加速 | x86_64 / KVM（4 vCPU、4 GiB） |
| 宿主 HEAD | `c07489c88`（L4 提交；L5 改动见本文件所在提交） |
| 内核 | `target/prime/kernels/x86.elf`，sha256 前缀 `9e8ee5dd544fa03a` |
| Stage1 initramfs | `target/debian-x86/stage1/initramfs.cpio`，sha256 前缀 `fb9c5b1765911cf0` |
| 根镜像 | `target/final-matrix/xwrap/image18.ext2`，sha256 前缀 `6941801d07c8c893`；4 GiB ext4，启动时空闲 **2,308,968 KB**（约 2.2 GiB） |
| 发行版 | Debian trixie（13.7） |
| debootstrap | `1.0.141`（install 阶段从镜像安装） |
| wget | `1.25.0-2`（debootstrap 的下载器；镜像原本只有 curl） |
| chroot | `/usr/sbin/chroot`（coreutils） |
| 目标配置 A | `/target`（根 ext2，任务书默认） |
| 目标配置 B | `/tmp/target`（tmpfs，2,085,216 KB） |

## 结果

### 配置 A：`/target`（根 ext2，任务书默认）

| 阶段 | rc | 耗时 | 目标文件系统空闲（前 → 后） | 说明 |
|---|---|---|---|---|
| install | **0** | 19.09 s | 2,308,968 → 2,303,444 KB（−5,524） | 装 `debootstrap` + `wget` |
| foreign | **0** | 46.46 s | 2,303,440 → 2,088,024 KB（**−215,416**，约 210 MiB） | 下载并解包 minbase |
| second | **1** | 2.68 s | 2,088,020 → 2,088,004 KB | `/sbin/ldconfig` 失败，见下 |
| 清理后 | — | — | 2,176,032 KB | 删除 apt 缓存后 |

能力探针（在 second-stage 之前）：

| 探针 | rc | 输出 |
|---|---|---|
| `chroot /target /bin/sh -c 'echo DBST_CHROOT_OK'` | **0** | `DBST_CHROOT_OK`（0.05 s） |
| `mount -t proc proc /target/proc` | **0** | — |
| `umount /target/proc` | **0** | — |
| `unshare -m /bin/true` | **0** | — |

second-stage 失败证据：

```
W: Failure trying to run:  /sbin/ldconfig
W: See //debootstrap/debootstrap.log for details
/sbin/ldconfig: Writing of cache data failed: Argument list too long
```

内核侧同一时刻：

```
WARN: ext2: metadata transaction failed: Error { errno: E2BIG,
      msg: Some("ext4 journal transaction is too large") }
```

**持久化**：关机后从镜像里只能读到 `/var/tmp/dbst/install.log`；
`foreign.log`、`second.log` 和整个 `/target` 都不在磁盘上。`/target` 的
目录项与远超 253 块的元数据集合从未提交——与上面的 E2BIG 是同一原因。

### 配置 B：`/tmp/target`（tmpfs，对照）

| 阶段 | rc | 耗时 | 目标文件系统空闲（前 → 后） |
|---|---|---|---|
| install | **0** | 18.29 s | 2,308,968 → 2,303,444 KB（根 ext2） |
| foreign | **0** | 38.05 s | 2,085,216 → 2,085,216 KB（tmpfs） |
| second | **0** | 24.28 s | 2,085,216 → 2,085,216 KB（tmpfs） |

说明：本内核的 tmpfs `statfs` 不随使用量变化，`df` 始终报 2,085,216 KB；
实际占用看 `du`：

| 指标 | 值 |
|---|---|
| rootfs 大小（清理前） | **218,820 KB** |
| rootfs 大小（清理后） | **131,108 KB** |
| chroot 探针 | rc=0，`DBST_CHROOT_OK` |
| mount / umount / unshare | 全部 rc=0 |
| `/etc/debian_version` | `13.7` |
| `/bin/sh` | `lrwxrwxrwx 1 root root 4 Feb 4 2025 /tmp/target/bin/sh -> dash` |
| 完整性判定 | `ok=1` |

## 失败归因

| 配置 / 阶段 | 阶段 | rc | 分类 | 一行证据 |
|---|---|---|---|---|
| A（ext2） | second | 1 | **磁盘**（ext4 日志事务上限） | `/sbin/ldconfig: Writing of cache data failed: Argument list too long`；同轮内核 `ext4 journal transaction is too large`（`kernel/core/src/fs/fs_impls/ext2/journal.rs:233-238`，单 descriptor 最多 253 个元数据块） |

没有出现 `chroot 未实现`、`syscall 缺失`、`网络/源` 或 `超时`：

- `chroot`、`mount`、`umount`、`unshare` 的探针全部 rc=0；
- install/foreign 的下载与解包 rc=0（wget 从清华镜像取完整个 minbase）；
- 三阶段均在 900 s 预算内（最长 46.46 s）。

### syscall 状态对照

| syscall | 内核实现 | 本轮探针 |
|---|---|---|
| `chroot`（x86_64 161） | 有（`kernel/core/src/syscall/chroot.rs`） | **rc=0**，rootfs 内 `/bin/sh` 正常执行 |
| `mount`（165）/ `umount2` | 有（`kernel/core/src/syscall/mount.rs`） | procfs 挂载 **rc=0**，卸载 **rc=0** |
| `unshare`（272，`CLONE_NEWNS`） | 有（`syscall/arch/x86.rs:390`） | **rc=0** |
| `mknodat`（259） | 有（`kernel/core/src/syscall/mknod.rs`） | debootstrap `setup_devices` 建 `/dev` 节点无报错 |
| `fsync`（74） | 有，但 ext4 元数据事务编码失败 | `/sbin/ldconfig` 写缓存时报 E2BIG |

## 结论

1. **debootstrap 能在 guest 内从零生成 Debian rootfs**：三阶段都有 rc，
   minbase 的下载、解包、dpkg 配置链路在本内核上可执行；
2. **chroot 可用**，而且不是“只返回 0”：新 rootfs 里的 `/bin/sh`（dash）
   真实执行并输出了标记；procfs 挂载、卸载、mount namespace 的
   `unshare` 也都可用；
3. **唯一阻塞是根 ext4 的日志事务上限**：`/target` 放在根 ext4 时，解包
   产生的元数据超过单个 JBD2 descriptor 的 253 块容量，`ldconfig` 写
   `/etc/ld.so.cache` 时得到 E2BIG，second-stage 中止；关机后整棵树也没有
   落盘。把目标放到 tmpfs（`/tmp/target`）后同一流程三阶段全过；
4. 因此“从零 rootfs”在本快照上的可用形态是：**tmpfs 内构建 + chroot
   可用；要持久化到磁盘，需要修 ext4 事务编码（内核改动，不在本轮范围）**。

## 复现方式

```sh
# 配置 A：任务书默认，/target 在根 ext2
make test_x86_debian_debootstrap \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/final-matrix/xwrap/image18.ext2

# 配置 B：目标放 tmpfs，三阶段全过
make test_x86_debian_debootstrap ... DEBOOTSTRAP_TARGET=/tmp/target
```

产物：`target/debian-debootstrap/console.txt`（最近一轮串口全量）、
`debootstrap/stages.tsv`、`disk.tsv`、`env.tsv`、`debootstrap.json`。
两轮的完整证据分别保留在
`target/debian-debootstrap/ext2-run2/` 与 `tmpfs-run2/`。

> 注意：`/target` 在 ext2 轮的日志与根树没有落盘，因此该轮的
> `debootstrap.log` 只能从串口的 `DBST log target=` 行读取；文档中的
> 失败证据即来自这些串口行与内核日志。

## 基线声明

- **debootstrap 版本**：1.0.141；**suite/variant**：trixie / minbase；
- **结果**：配置 A（ext2）install/foreign/second = **0/0/1**；
  配置 B（tmpfs）= **0/0/0**，rootfs 218,820 KB（清理前）；
- **chroot**：可用（两轮探针均 rc=0）；mount/umount/unshare：可用；
- **已知失败**：只在“目标位于根 ext4”时出现，`second-stage`，
  `/sbin/ldconfig` 写缓存 E2BIG；
- **判定规则**：若后续内核修复了 ext4 事务上限，配置 A 的 second-stage
  应转为 rc=0 且 `/target` 在关机后仍存在；若 chroot/mount/unshare 探针
  不再 rc=0，视为退化。
