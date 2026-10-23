# E2BIG 修复后的 L4/L5/L6 闭环验证

## 结论摘要

- 使用修复 ext4 日志事务上限后的内核（HEAD `14b2d352a`，ELF
  `target/osdk/asterinas-osdk-bin.elf`，sha256 前缀 `800cc5be82f91a38`）
  重跑三级；
- **L5 转绿**：`install/foreign/second = 0/0/0`，second-stage **418.89 s**
  （修复前 rc=1，`/sbin/ldconfig` 写缓存 E2BIG）；rootfs 完整，且
  `/target` 在关机后**仍然存在**（修复前整棵树未落盘）；
- **L6 的 E2BIG 消失**：内核 `ext4 journal transaction is too large` 从
  修复前的多条降为 **0 条**；升级推进到 `/etc/debian_version=forky/sid`，
  副本仍可启动（systemd `running`、串口可登录）。但升级在 **40 分钟上限**
  超时（rc=124，解包 **227/341**、配置 **78**）——新的失败是**超时**，
  不是 E2BIG；
- **L4 仍是 2/3**：`hello` 与 `python3-minimal` 成功，`coreutils` 仍以
  **signal 11** 失败（make 处理 `DPKG_EXPORT_BUILDFLAGS` 导出递归变量的
  崩溃）。这个失败**修复前也不是 E2BIG**，日志修复不涉及它；
- 结论：**E2BIG 已确认消除，没有引入内核退化**。剩余问题变成 L6 的时间
  预算与 L4 的 make 崩溃，两者都不是文件系统事务问题。

## 环境

| 项 | 值 |
|---|---|
| 日期 | 2026-10-24 |
| 内核 HEAD | `14b2d352a`（`fix(ext4): split oversized journal transactions across descriptors`） |
| 内核 ELF | `target/osdk/asterinas-osdk-bin.elf`，sha256 前缀 `800cc5be82f91a38`；23 MB stripped，`Machine: Intel 80386` |
| 根镜像 | `target/systemd-health/daily.ext2`，sha256 `450ebe3bf2506c290902d55ae651ba796e67c0b9a846c528de34528f82a083e0`（trixie 13.7；原 `image18.ext2` 已不在 `target/`） |
| L4 workspace | `target/debian-source-build-verify/` |
| L5 workspace | `target/debian-debootstrap-verify/` |
| L6 workspace | `target/debian-upgrade-verify/`（升级副本 sha 与基础镜像相同） |

## 修复前 vs 修复后

### L4：源码包构建

| 项 | 修复前 | 修复后 |
|---|---|---|
| `hello` | ok，2 个 `.deb` | **ok**，2 个 `.deb`（276.94 s） |
| `python3-minimal` | ok，19 个 `.deb` | **ok**，19 个 `.deb`（338.99 s） |
| `coreutils` | fail，rc=255，`signal 11` | **fail，rc=255，`signal 11`**（156.81 s，未变） |
| 汇总 | 2/3 | **2/3** |
| E2BIG | 基线 `hello` 在 ext2 上曾于 `dpkg-deb fsync` 处 E2BIG（后被 tmpfs 构建树规避） | 本轮未再出现 E2BIG |

`coreutils` 的错误串（修复前后相同）：

```
dpkg-buildpackage: error: debian/rules clean subprocess was killed by signal 11
```

归因：**其他（make 崩溃）**，不是 E2BIG，也不是 dpkg 冲突/依赖问题。

### L5：从零 rootfs

| 阶段 | 修复前（ext2 `/target`） | 修复后（ext2 `/target`） |
|---|---|---|
| install | 0 / 19.09 s | **0 / 18.73 s** |
| foreign | 0 / 46.46 s | **0 / 43.84 s** |
| second | **1 / 2.68 s**（ldconfig E2BIG） | **0 / 418.89 s** |
| verify | 未执行 | **`debian_version=13.7`、`/bin/sh→dash`、`ok=1`** |
| `/target` 关机后 | **不存在**（整棵树未落盘） | **存在**（`/target/etc/debian_version` 可读） |

修复前错误串：

```
/sbin/ldconfig: Writing of cache data failed: Argument list too long
```

修复后：同一 `/target`（ext2）上三阶段全 0，chroot/mount/umount/unshare
探针仍全部 rc=0。

### L6：trixie → forky

| 项 | 修复前 | 修复后 |
|---|---|---|
| 镜像复制 | base/copy sha 相同 | base/copy sha 相同（`450ebe3b…`，0.57 s） |
| source | 0 | **0**（0.05 s） |
| update | 0（297 可升级） | **0**（12.77 s，297 可升级） |
| upgrade | **100**（`linux-libc-dev` 处 E2BIG） | **124（超时，2400.06 s）** |
| 内核 E2BIG 条数 | 多条 | **0** |
| 实际进度 | 解包 8 / 配置 4 | **解包 227 / 配置 78**（被 40 分钟上限终止于 `git-man`） |
| `debian_version` | 13.7 | **`forky/sid`** |
| reboot | 0（13.7，systemd running） | **0（`forky/sid`，systemd running，登录成功，10.00 s）** |

修复前错误串：

```
Unable to synchronize mmap - msync (7: Argument list too long)
unable to fsync updated status of linux-libc-dev: Argument list too long
```

修复后错误串：

```
UPG stage=upgrade rc=124 dur=2400.06
（日志末尾）Unpacking git-man (1:2.53.0-1) over (1:2.47.3-0+deb13u1) ...
```

## 新失败归因（不再归因到 E2BIG）

| 项 | 现象 | 归因 |
|---|---|---|
| L6 upgrade | rc=124，2400 s 上限；227/341 解包、78 配置；0 条 E2BIG | **超时**：按本内核约 9.5 s/包的节奏，341+45 个包及其配置在 40 分钟内做不完；这是执行速度预算，不是文件系统事务 |
| L4 coreutils | rc=255，`signal 11`，0.84 s | **其他（make 崩溃）**：`DPKG_EXPORT_BUILDFLAGS=1` 导出递归 `$(shell dpkg-buildflags)` 变量导致 make 失控；与 ext4 日志无关 |
| 残留 `grep` 错误 | L6 维护脚本中出现 `grep: (standard input): Function not implemented` | **syscall 缺失（splice）**：L4 已用 grep shim 规避，本轮 L6 没有启用该 shim；没有中止升级 |

## 结论

1. **E2BIG 确实消除**：L5 从失败转为三阶段全 0，且 `/target` 可持久化；
   L6 不再出现任何 ext4 事务失败，并推进到 `debian_version=forky/sid`；
2. **没有引入内核退化**：LTP 34/6/0 已在本轮之前用同一内核验证；L4 的
   `coreutils` 失败与修复前完全一致（signal 11）；
3. **L6 的失败模式已从“文件系统事务”转为“时间预算”**：在 40 分钟内完成了
   227 个包的解包和 78 个配置，但没走完；副本仍能启动到 forky/sid；
4. 下一步：把 L6 的 `UPG_UPGRADE_TIMEOUT`（探针）与 gate 超时提高
   （例如 4800 s / 5400 s）重跑，确认能否完整配置全部包；若需要，
   同时给 L6 注入 grep shim 消除残留 splice 错误。

## 复现方式

```sh
# 统一使用新内核与 trixie 基础镜像
K=$PWD/target/osdk/asterinas-osdk-bin.elf
I=$PWD/target/debian-x86/stage1/initramfs.cpio
R=$PWD/target/systemd-health/daily.ext2

make test_x86_debian_source_build   X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I X86_DEBIAN_XORG_BASE_ROOT=$R SOURCE_BUILD_WORKSPACE=$PWD/target/debian-source-build-verify SOURCE_BUILD_TIMEOUT=2400
make test_x86_debian_debootstrap    X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I X86_DEBIAN_XORG_BASE_ROOT=$R DEBOOTSTRAP_WORKSPACE=$PWD/target/debian-debootstrap-verify DEBOOTSTRAP_TIMEOUT=1500
make test_x86_debian_full_upgrade   X86_DEBIAN_KERNEL=$K X86_DEBIAN_STAGE1_INITRAMFS=$I X86_DEBIAN_XORG_BASE_ROOT=$R FULL_UPGRADE_WORKSPACE=$PWD/target/debian-upgrade-verify FULL_UPGRADE_TIMEOUT=3000
```

产物：三个 workspace 下的 `console.txt`、`*/stages.tsv`、`*/upgrade.json`
或 `*/source-build/`；L6 的 `upgrade/copy.txt` 记录 base/copy 双 sha。

## 基线更新

- **L4**：2/3；已知失败 `coreutils`（signal 11，make）。
- **L5**：ext2 `/target` 也是 3/3；second-stage 约 419 s；`/target` 可持久化。
- **L6**：E2BIG 已消除；当前失败是 40 分钟超时，`debian_version` 已达
  `forky/sid`，副本可启动。
