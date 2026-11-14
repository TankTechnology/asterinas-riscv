# 根文件系统重建方案（2026-11-14，仅主机侧，未执行写入）

## 1. 为什么值得重建

板上的 ext2/4 根分区已经出现三类故障，且都是同一块 eMMC 引起的：

1. **写入失败**：`writeback I/O failed ... page remains dirty`（单次 26 条）、
   `CMD12 ... Timeout`、`block: failed to do Write ... (sectors 3257224..3257232)`；
   失败 LBA 分散，不是固定区域。
2. **超级块读回失败**：两次离线 `e2fsck` 都在最后 "write file system info" 阶段
   报 `failed to do Read ... (sectors 2..3)` → `Input/output error`，rc=9。
3. **内容级损坏**（不只是元数据）：
   - `/etc/group` 整表 GID 字段被清空（`root:*::`），glibc 因此只认得 0 个组；
   - `/var/lib/dpkg/alternatives/pager` 里存的是一段与本文件毫无关系的 Python 脚本；
   - `/usr/bin/lesskey` 变成一个 mode 0000 的空目录；
   - 多处目录项指向已释放 inode（ESTALE，`/var/lib/apt`、`/var/lib/dpkg/updates`、
     Firefox 的 `profiles.ini`）。

已有修复能救回可用性（ESTALE 已清、dpkg/apt 已能装包、组查询已恢复），
**但内容级损坏无法靠 fsck 复原**——被写坏的文件只剩错误数据。所以重建是正解；
前提是换一块可靠的存储介质，否则新镜像会重复同样的命运。

## 2. 现有资产（重建的输入）

| 资产 | 位置 | 说明 |
|---|---|---|
| 可复现构建脚本 | `tools/riscv/debian/rootfs/build_rootfs.sh` | debootstrap + qemu-riscv64-static，清华镜像，profile 化 |
| 板子快照 | `~/megrez-backup-2026-11-14/snapshot/` | 656 个包、336 个 `/etc` 文件、45 个 unit、内核 cmdline、分区表 |
| 配置与用户数据 | `~/megrez-backup-2026-11-14/{system-config,home}.tar.gz` | `/etc`、`/root`、dpkg/apt 状态、`/home` |
| 文件清单 | `~/megrez-backup-2026-11-14/rootfs-inventory.txt` | 31,140 条（权限/属主/大小/路径） |
| 修复镜像 | 板上 `/root/repair.cpio.gz`、主机 `target/board/repair.cpio.gz` | e2fsck + busybox，可从 U-Boot 手工 `booti` |
| 对比工具 | `tools/riscv/debian/rootfs/{snapshot_board,compare_snapshot}.sh` | 快照与新镜像的差异清单 |

主机侧依赖已确认齐全：`debootstrap`、`qemu-riscv64-static`、`mke2fs`、`debugfs`、`dumpe2fs`、`curl`。

## 3. 目标介质（决定成败的一步）

| 选项 | 可行性 | 风险 |
|---|---|---|
| **A. SD 卡**（板上 `mmc 1`） | U-Boot 当前报 `Card did not respond to voltage select`，插入卡后再确认 | 最低：完全不动 eMMC，拔卡即回滚 |
| B. p3（同一片 eMMC，115 GiB） | 分区存在，但当前 EUCLEAN 且 fsck 写回失败 | 高：同一片已退化的闪存 |
| C. 重写 p2（同一片 eMMC） | 需要网络投递 3 GiB 镜像 | 高：写入正是当前失败的操作 |

**建议 A**。没有可用 SD 卡时，B/C 都只是把同样的问题挪个位置；那种情况下更稳的做法是
维持现状（只读使用 + 增量备份），等换介质再重建。

## 4. 步骤

### 阶段 A：主机侧构建与校验（不需要板子，随时可做）

```bash
# A1. 选与板上最接近的 profile（板上装了 firefox-esr/fcitx5/openbox/pcmanfm/lxpanel
#     但没有 netsurf-gtk 包——NetSurf 是源码编的），从 browser-web 或
#     desktop-m9-software 起，用 A2 的差异清单决定最终取舍。
tools/riscv/debian/rootfs/build_rootfs.sh browser-web --output-dir target/rebuild/rootfs

# A2. 与板子快照对比，得到「新镜像缺什么」
tools/riscv/debian/rootfs/compare_snapshot.sh \
    target/rebuild/rootfs ~/megrez-backup-2026-11-14/snapshot

# A3. 按清单补差（在 chroot + qemu-user 里 apt install 缺失包；/etc、unit、/root
#     脚本从 system-config.tar.gz 覆盖回板上版本；/home 从 home.tar.gz 恢复）

# A4. 离线校验（全部在主机，不接触板子）
e2fsck -fn target/rebuild/rootfs.ext4          # 期望 rc=0/1
dumpe2fs -h target/rebuild/rootfs.ext4         # 块大小/特性与板子一致（4 KiB、无 journal）
chroot+qemu：dpkg --audit 为空；getent group root 有输出；dpkg -l | wc -l ≈ 656
```

生产镜像时注意：板上根分区是 **4 KiB 块、无 journal** 的 ext2/4 兼容 profile，
重建必须沿用（`build_rootfs.sh` 已按此配置，脚本里有 `dumpe2fs` 校验）。

### 阶段 B：写入介质（**需明确同意后才做**）

```bash
# B1. 写 SD 卡（主机侧操作，板子不断电、eMMC 不动）
sudo dd if=target/rebuild/rootfs.ext4 of=/dev/sdX bs=4M conv=fsync status=progress && sync
```

B2. 从 SD 卡启动：**不修改 bootcmd**，在 U-Boot 提示符用一次性命令引导（`mmc 1:2`），
    这样 eMMC 上的原系统仍是默认启动项，随时可回滚。

### 阶段 C：验收门禁

| 检查 | 期望 |
|---|---|
| SSH / 串口 | 均可登录 |
| `dpkg --audit` | 空 |
| `apt-get update && apt-get install --reinstall -y less` | rc=0 |
| `getent group root` | `root:x:0:` |
| `/home` 内容 | 与备份一致（文件数、关键文件 sha256） |
| 桌面 | `asterinas-desktop-m5` 起来（`profiles.ini` 由 Firefox 重建） |
| 基准 | `tools/riscv/perf/al_gate.py` 三项比率不劣化 |
| 稳定性 | 连续 3 次重启，`dmesg` 无 `writeback I/O failed` |

## 5. 回滚

- 阶段 A 全部在主机，无回滚需求。
- 阶段 B 之后 eMMC 保持原样：**拔掉 SD 卡 + 重启即回到现有系统**；
  bootcmd 未修改，U-Boot 自动引导 eMMC 的 boot.scr。
- 若曾进入修复环境：`/root/repair.cpio.gz` 仍在，随时可手工 `booti` 再修一次。

## 6. 时间与风险

- 阶段 A：镜像构建 20–40 分钟（debootstrap + 包下载），补差 15–30 分钟，校验 5 分钟。
- 阶段 B：写 SD 卡 5–15 分钟（取决于卡速）。
- 主要风险：① profile 与板上包集有偏差（由 A2 的差异清单兜住）；② SD 卡槽在板上
  从未验证过（U-Boot 只报过"卡未插入"）；③ 板子上那些非包来源的东西
  （NetSurf 源码编译、xkb keymap stub、dsh、portal 脚本、内核镜像）必须从备份手工恢复，
  它们在 `/root` 与 `system-config.tar.gz` 里都有。

## 7. 明确不做的事

- 不修改板子 bootcmd / boot.scr；
- 不对 p2/p3 执行任何写入，直到用户明确同意并指定介质；
- 不删除 eMMC 上的任何数据。
