# 存储专项：清理、p3 使用与扩根（2026-11-14）

板子在 06:44（内核时间 4856 s）之后彻底失联：ping 100% 丢包、SSH 超时、串口无输出。
本文件是**离线准备**：板子回来前只写方案和脚本，不执行任何破坏性操作。

## 0. 已知事实（失联前实测）

```
/dev/mmcblk0p2   3.0G  2.6G  308M  90%  /         根分区（ext2/4 兼容 profile）
mmcblk0p1       500 MiB              启动分区（U-Boot 脚本、内核、initrd）
mmcblk0p3       114.6 GiB            数据分区，**放着 repair.cpio.gz**（修复用 initramfs）
```

| 目录 | 大小 | 可回收性 |
|---|---|---|
| `/var` | 209 MB | `/var/lib/apt/lists` 89 MB 可重下 |
| `/home` | 139 MB | **用户数据，不动** |
| `/lost+found` | 61 MB | 2026-11 两次 e2fsck 修复的孤儿，看完再删 |
| `/root` | 42 MB | 含内核镜像备份（每个约 6.8 MB） |
| `/usr/share/doc` | 66 MB | 文档，可删但会影响 `dpkg -L` 输出 |

Asterinas 当前**不暴露** p1/p3 的设备节点，所以 p3 的内容只能用 U-Boot 只读查看
（`ls mmc 1:3 /`），不能用 `blkid`/`file`。

## 1. 可安全回收项与预计释放量

| 项 | 命令 | 预计释放 | 风险 |
|---|---|---|---|
| apt 包列表 | `rm -rf /var/lib/apt/lists/*` | 89 MB | 无（`apt-get update` 重建） |
| apt 包缓存 | `rm -rf /var/cache/apt/archives/*.deb` | 0–90 MB | 无（重下即可） |
| 旧内核镜像 | 删除 `/root/kernel-rel.img.bak` 等旧镜像 | 约 14–21 MB | 保留当前 + 上一版即可 |
| 孤儿文件 | `rm -rf /lost+found/*`（看完内容后） | 61 MB | 确认无有用文件；`/lost+found` 目录本身保留 |
| 文档 | `rm -rf /usr/share/doc/*` | 66 MB | 可选，影响 `dpkg -L` |

合计约 **230 MB → 使用率 90% → 约 82%**。目标是 <70%（≈ 剩余 900 MB），
因此还需要结构性手段（见第 2 节），只靠清理不够。

## 2. p3（114.6 GiB）的三种用法

**先确认现状**：p3 上至少有 `repair.cpio.gz`（boot.scr 的修复回退路径会从
`mmc 1:3` 加载它）。所以 **p3 不能格式化**，只能在它现有的文件系统里使用。

### 方案 A：挂到 `/data`（推荐）
- 前提：Asterinas 暴露 `mmcblk0p3` 设备节点并可挂载（当前没有，需要内核/驱动工作）。
- 做法：`mount -t ext4 /dev/mmcblk0p3 /data`，`/etc/fstab` 里不去动（Asterinas 用 stage1）。
- 收益：一次性解决空间问题；`/home`、构建目录都可以迁过去。
- 风险：低（只读挂载验证后再写）；回滚：`umount /data`。

### 方案 B：把 `/var/cache/apt`、`/tmp` 迁到 p3
- 做法：`/data/apt-cache` + bind mount 或符号链接；`/tmp` 本来就是 tmpfs，**不需要迁**。
- 收益：apt 的 88 MB 缓存不再占根分区。
- 风险：低；回滚：删链接/改回 bind。

### 方案 C：扩根分区（离线，破坏性）
- 做法：在 repair initramfs 里 `e2fsck -f /dev/mmcblk0p2` → 缩小 p3 →
  `resize2fs /dev/mmcblk0p2`（需要先改分区表，`fdisk`/`sgdisk`）。
- 风险：**高**。改分区表时断电会丢全盘；p2 是 4 GiB 起点的 ext 文件系统，
  扩容需要连续的未分配空间，p3 的起点紧邻 p2。
- 回滚：分区表备份（`sgdisk --backup`）+ 操作前整盘镜像到主机（约 4 GB，走网络）。
- **结论：只出方案，等用户明确同意再做，且必须先有整盘备份。**

## 3. ESTALE 修复（D3）

现象：`/var/lib/apt/extended_states`、`/var/lib/dpkg/updates/*` 返回 `ESTALE`，
重启不清除 —— 目录项指向已释放的 inode，来自一次被 eMMC 写错误中断的 apt 安装。

步骤：
1. U-Boot 加载 repair initramfs（boot.scr 的 fallback 路径会自动选它：
   `ext4load mmc 1:2 /root/kernel-rel.img` 失败 → 用 `.bak` + `repair.cpio.gz`）。
2. 在修复 shell 里：`e2fsck -f -y /dev/mmcblk0p2`（只读检查用 `e2fsck -fn` 先看一眼）。
3. 验收：
   - `e2fsck -fn` 退出码 0 或 1，无 "unattached inode"/"deleted inode referenced"；
   - 板上 `stat /var/lib/apt/extended_states` 不再 ESTALE；
   - `dpkg --audit` 为空，`apt-get update` 成功，`apt-get install --reinstall -y less` 成功。

## 4. eMMC CMD25 写失败（0x1008001）排查清单

现象（2026-11-14 02:0x，`apt install fio` 期间）：

```
block: failed to do Write ... IoError (sectors 5217312..5225504)
mmc: [mmc] CMD25 failed: status=0x1008001 error=AutoCommand
mmc: [mmc] CMD12 failed: status=0x18000 error=Timeout
```

`0x1008001` 拆位：bit24 = `AUTO_CMD_ERR`，bit0 = `CMD_CRC` 之外的低位错误；
随后 CMD12（停止传输）超时，说明控制器在自动 CMD12 之后没有回到传输态。
这正是 MEMORY 里记录的「自动 CMD12 卡死」：整机 I/O 全 EIO，连 reboot 都读不进来，
只能复位。

排查顺序：
1. 复现只在大块写（≥ 512 KiB 合并写）时出现？小写（4 KiB）是否稳定？
2. 看 `mmc_write_coalescing` 的合并上限与 SDMA 边界（`boundary=524288`）——
   跨越 SDMA 边界的合并写是首要嫌疑。
3. 出错后控制器状态：`CTRL`/`STATUS` 是否停在 `Transfer Complete` 未清；
   复位 HS 时钟与 1-bit 模式是否恢复。
4. 若确认跨边界合并是触发点，短期把合并窗口限制在 SDMA 边界内（牺牲吞吐换稳定），
   中期改成非合并的多块写 + 显式 CMD12。

## 5. 执行顺序（板子回来后）

1. **D1**：`storage_inventory.sh`（只读）→ 记录 → `storage_cleanup.sh`（先 dry-run，
   再 `--apply`）→ 前后 `df` 对比。
2. **D2**：U-Boot `ls mmc 1:3 /` 只读确认 p3 内容 → 若可挂载则挂 `/data`；
   否则只留方案。
3. **D3**：repair initramfs + e2fsck 修 ESTALE → 验收 apt/dpkg。
4. **D4**：盘健康后恢复页缓存整块读快速路径（`docs/porting/2026-11-14-page-cache-read-path.md`）。

## 6. 护栏

- 不 push；不重刷镜像；**不改分区表、不 mkfs**（除非用户明确同意且已整盘备份）。
- 不动 `/home` 与 git 工作区；不确定的目录只列出、不删。
- 每步先 dry-run，命令与输出写进 `/tmp/tp/storage-log.md`。
- 每次写操作前 `sync`；eMMC 一旦报 `CMD25` 立即停止写操作并按第 4 节排查。
