# p2 备份与修复 runbook（2026-11-20 只读清点之后）

清点结论见 `2026-11-20-board-crash-consistency-attempt.md` 之外的单次记录：p2（Asterinas 的 Debian 根）
`e2fsck -fn` 报出 **1732 行**错误，集中在

- 目录区 51200–51267：大量 zero-length directory、`Deleted inode has zero dtime`；
- inode 51228：块指针里是文本片段（`Illegal block #12 (1194156649) …`）；
- inode 131089–131093：xattr 块损坏（`bad extended attribute block 547526` / `h_blocks > 1` / `i_blocks` 不匹配）。

**在动手之前记住三条**：任何 `-fy` 都是写盘；没有备份就没有回滚；救援 initramfs 的 shell 就是 PID 1，
**永远不要对它发 Ctrl-D**，长命令一律用 PACE ≥3 敲。

## 0. 前置

- 板子到 U-Boot（`Hit any key` 时按空格停下）。
- `e2fsck` 工具包已在 p3：`/e2.b64`（547,042 B，`md5sum` = `eab808b8cf8d27a89e8cc8019083141f`），
  解出的 tgz md5 = `0cfa82f59b3a9ffb5633059b251ad5a8`，内含 `e2fsck` + `libext2fs/libe2p/libss/libcom_err`。
- **不要跑 `bootcmd`**：它会把 Asterinas 拉起来，p2 就会以读写方式挂成根。

## 1. 备份 p2 到 p3（唯一回滚路径）

```
run bootcmd_rockos      → 菜单 1 → 掉进 (initramfs)
cat /proc/partitions                                   # 先确认 p2 的扇区数
mkdir -p /p2 ; mount -t ext4 -o ro /dev/mmcblk1p2 /p2     # 只读！
dd if=/dev/mmcblk1p2 of=/root/p2-backup.img bs=4M      # /root 就是 p3
ls -l /root/p2-backup.img                              # 必须是 4294967296
sha256sum /root/p2-backup.img /dev/mmcblk1p2           # 两行必须相同
umount /p2
```
p3 约 114 GiB，放得下 4 GiB。**没看到 `4294967296` 就不要往下走。**

备份的判据是**两个**，缺一不可：

1. 长度 `4294967296`（防截断）；
2. `sha256sum` 备份文件与**设备本身**相同（防读错误/空洞）——第二次读设备约多花 2 分钟，
   但这是唯一回滚路径，值得。

先在 `/proc/partitions` 里确认 p2 确实是 4 GiB（8,388,608 个 512 B 扇区）；
如果数字对不上，先停下核对，不要拿一个记错的大小去判备份成功。

## 2. 把 e2fsck 放到"不依赖 p2"的地方

`-fy` 要求 p2 **未挂载**，所以工具不能留在 p2 上。做法：p2 只读挂着的时候把运行时复制到 p3。

```
mount -t ext4 -o ro /dev/mmcblk1p2 /p2
mkdir -p /root/e2run/lib
cp /p2/lib/ld-linux-riscv64-lp64d.so.1 /root/e2run/lib/
cp /p2/lib/riscv64-linux-gnu/libc.so.6 /root/e2run/lib/   # 路径按实际 lib 布局调整
chroot /p2 /bin/sh -c "base64 -d /mnt3/e2.b64 > /tmp/e2.tgz ; mkdir -p /tmp/e2 ; tar xzf /tmp/e2.tgz -C /tmp/e2"
cp /p2/tmp/e2/e2fsck /root/e2run/
cp /p2/tmp/e2/lib/* /root/e2run/lib/
cd /root/e2run/lib && ln -sf libext2fs.so.2.4 libext2fs.so.2 && ln -sf libe2p.so.2.3 libe2p.so.2 \
  && ln -sf libss.so.2.0 libss.so.2 && ln -sf libcom_err.so.2.1 libcom_err.so.2
umount /p2
```
自检（此时 p2 **必须是未挂载**状态）：

```
/root/e2run/lib/ld-linux-riscv64-lp64d.so.1 --library-path /root/e2run/lib /root/e2run/e2fsck -V
```
打印 `e2fsck 1.47.2` 才继续。

## 3. 修复

```
/root/e2run/lib/ld-linux-riscv64-lp64d.so.1 --library-path /root/e2run/lib \
    /root/e2run/e2fsck -fy /dev/mmcblk1p2 2>&1 | tee /root/p2-fsck-fix.log
echo "RC=$?"
```
- 把 `tee` 的输出留在 p3 上——这是"改了哪些 inode"的唯一记录。
- 结束之后**再跑一次 `-fn` 确认干净**（第二次应当 0 错）。

## 4. 验证

```
mount -t ext4 /dev/mmcblk1p2 /p2 && ls /p2 && ls /p2/usr/lib/systemd/system | head && umount /p2
```
然后正常引导 Asterinas，判据：
- 桌面 `ASTERINAS_DESKTOP_BOOT_READY` 出现；
- Firefox 能建新 profile（`MarionetteActivePort` 内容为 `2828`）；
- 启动日志里不再出现 `refusing to free already-free block`。

## 5. 回滚

任何一步失败：把备份写回去，再回到修复前。

```
cat /root/p2-backup.img > /dev/mmcblk1p2
```

## 未决

- xattr 修不回来的 inode 会导致个别文件丢失；fsck 日志里的 inode 清单要单独留档。
- p2 是 4 GiB、无 journal（挂载时显示 `without journal`），修复后是否需要重建取决于损伤范围，不在本 runbook 内。
