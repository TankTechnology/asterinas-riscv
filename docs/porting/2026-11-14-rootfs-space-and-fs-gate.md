# 根分区空间与崩溃一致性门禁（2026-11-14，仅建议）

## 1. 空间现状（板上实测）

```
/dev/mmcblk0p2   3.0G  2.6G  308M  90%  /
mmcblk0p1   500 MiB   （启动分区，ext4，Asterinas 不挂载）
mmcblk0p3   114.6 GiB （数据分区，ext4，当前未使用）
```

占用明细（`du -sm`）：

| 目录 | 大小 | 说明 |
|---|---|---|
| `/var` | 209 MB | 其中 `/var/lib/apt/lists` 89 MB（可重下） |
| `/home` | 139 MB | 用户数据 |
| `/lost+found` | 61 MB | 两次 e2fsck 修复后的孤儿文件 |
| `/root` | 42 MB | 内核镜像备份与测试二进制 |
| `/usr/share/doc` | 66 MB | 文档 |

## 2. 可清理项（按性价比排序，均不涉及分区操作）

1. **`rm -rf /var/lib/apt/lists/*`** → 释放 89 MB。列表可随时 `apt-get update` 重建；
   注意 `apt-get update` 会重新写入同样大小，适合在「不装包」的时段清理。
2. **`/lost+found`（61 MB）**：先 `ls` 看内容再删——里面是 2026-11 两次修复
   留下的孤儿 inode，确认没有需要的东西后清空。
3. **`/usr/share/doc`（66 MB）**：可删，代价是 `dpkg -L` 的文档路径失效。
4. **`/root` 里的旧内核镜像**（`kernel-rel.img.bak`、`kernel-rel-20261110.img` 等）：
   每个约 6.8 MB，保留当前 + 上一版即可。

合计可回收约 200 MB，使用率 90% → 约 83%。

## 3. 结构性建议（需要设计，不在本次范围）

- **把 p3（114 GB）用起来**：当前 Asterinas 不暴露 p3 的设备节点，无法挂载。
  最小改动是让 stage1 或 mmc 驱动暴露 `mmcblk0p3` 并允许按分区挂载，
  然后把 `/home` 或 `/var/lib/apt` 迁过去。这是根治，且与「写保护门只放行 p2/p3」
  的现有安全设计一致。
- **根分区扩容**：p2 只有 4 GiB。e2fsck + resize2fs 可以在不重刷的情况下扩大
  （需离线操作，风险高于上一条）。
- 无论哪种，**先跑一次 e2fsck**：当前 `/var/lib/apt/extended_states` 与
  `/var/lib/dpkg/updates/*` 指向已释放 inode（ESTALE，重启不清除），
  dpkg 因此不可用。

## 4. 崩溃一致性门禁常态化

新增 `tools/riscv/fs/nightly_gate.sh`：跑两组 writer，各自 2 次随机杀 QEMU：

```bash
tools/riscv/fs/nightly_gate.sh /tmp/fs-gate-$(date +%H%M%S)
```

- `records` × ext2：**通过**（2026-11-14 实测，valid=3096/3512，同步标记全覆盖，尾部干净）
- `churn` × ext4：**未通过，但原因是测试脚手架**——guest 的 init 以状态 1 退出
  （`The init process terminates with code 256`），churn writer 根本没启动，
  不是内核崩溃。下一步看 `crash_consistency_gate.py` 生成的 ext4 镜像与 guest init
  的匹配（该用例要求 4 KiB 块）。

门禁本身跑在 QEMU 里，不碰开发板，可以随时回归。
