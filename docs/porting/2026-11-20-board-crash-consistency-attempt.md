# 板上崩溃一致性测试：三轮尝试、两个无效轮与一条结论（2026-11-20）

## 1. 目标

ext2 写序修复（`ext2: order allocation metadata writes and persist it on fsync`）
在 QEMU 门禁上已有 A/B：有写序 3/3 clean，无写序对照 3/3 `dangling=[12]`。
本轮想在**真机 eMMC** 上补一条同类的观察：在"位图已落盘、inode table 未落盘"
的窗口内断电，重启后 `e2fsck` 应保持干净。

被测对象**不是** p3 本身，而是 p3 上的一个 64 MB 文件 `cc.img`
（主机 `mke2fs -t ext2` 生成，`mount -o loop` 进去跑 churn）。
这样崩溃只可能损坏镜像文件的内容，而不是内核镜像所在的文件系统。

## 2. 三轮的做法

| 轮 | 窗口 | 当轮落盘手段 | 杀点 | 结果 |
|---|---|---|---|---|
| v1 | `ext2_sync_hold_ms=1500` | 无 | 首轮 `CHURN 1` 后 ~0.45 s | 无效：镜像里只有 `probe.txt` |
| v2 | 5000 ms | 无 | `R4` 后 3.0 s | 无效：`r0004.dat` 不在 |
| v3 | 2000 ms | 辅助线程每 100 ms `syncfs(/mnt3)` | `R3` 后 1.6 s | 无效：`r0003.dat` 不在 |

v2/v3 都命中了窗口（`R1 S1 R2 S2 R(n)`，无 `S(n)`，随后 `RESET_TRACE path=emergency_restart`），
但崩溃后从 p3 取回的镜像里，**当轮写入的文件一个都不存在**。

## 3. 根因

内层 loop 设备的写只落到**外层 ext2（p3）的 page cache**，而 Asterinas 的 ext2
**没有周期性回写**——脏页只在 `sync`/`fsync` 或内存压力时下去。窗口内没有任何人替外层刷盘，
所以内层刚写下的位图跟着一起丢。

v3 试图让辅助线程用 `syncfs(2)` 只刷外层，但 `sys_syncfs` 走的是同一个
`Ext2::sync_all`（`kernel/core/src/syscall/sync.rs:19` → `fs().sync()`），
**它自己也带 `ext2_sync_hold_ms` 的 hold**，刷新粒度等于窗口长度，等于没有抽样机会。

## 4. 结论（原样）

> **板上 loop 文件方案无法观测写序窗口。** 外层 ext2 无周期性回写；`syncfs(2)` 虽可用
> （`syscall/sync.rs:19` → `fs().sync()`），但它自身也走带 `ext2_sync_hold_ms` 的
> `sync_all`，在窗口内无法保证对"内层刚写下的位图"完成一次刷写。实测两轮
> （hold=5000 无 flusher、hold=2000 带 flusher）第 N 轮文件均未出现在镜像里。
>
> 写序修复的**有效性**只由 QEMU A/B 3/3 承担（有写序 clean；无写序对照 `dangling=[12]`）。
> 板上保留的**间接证据**是：A2 连续 5 轮 READY、`PANIC message=` 0、`IRQ_STORM` 0、
> 正常 churn（`/mnt3/cct` 50 轮 / 300 K）无 `refusing to free` 报错。
> **不得**用"板上 e2fsck 为 0"表述写序已被验证。

两条附带记录：

* `tools/riscv/fs/crash_writer.c` 在 QEMU **块设备**上调用 `syncfs`，没有"外层 ext2"
  这一层，因此它证明的是 **syscall 通路可用**，**不能**说明它能恢复 loop 方案的观测力；
* 本轮 p3 上**没有**出现 `refusing to free already-free`（`e2fsck` 与串口都干净），
  所以 p2 那串启动期报错确实是独立旧伤（`inode 131089` / `group 16 894 vs 895`）。

## 5. 判据（比结论更重要）

崩溃后必须能在镜像里看到**当轮**写的文件，分两条、分开写：

* **b1**：历史轮（`r0001..r(n-1)`）在镜像里 → 外层没有掩盖历史；
* **b2**：**当轮**（`r000n.dat`）在镜像里 → 窗口被观测到。**b2 不成立即判无效**，
  不是"通过"。

三个曾经骗过我们的点：

1. 只看 `e2fsck` 退出码 0 —— 它描述的是**上一个完整同步**的快照，与窗口无关；
2. 只看 b1 —— "前几轮在"会被误当成"窗口被观测到"；
3. 跑前不清理镜像里的旧标记 —— 同名文件被覆盖后 b1 失去判别力（v3 前已用
   `rm -f /mnt/cc/r000*.dat /mnt/cc/c{0..7}; sync` 清场，前置状态记为"镜像内只有 `probe.txt`"）。

另外，杀点判据仍沿用门禁那套：**只有一条 `R(n)`、没有 `S(n)`** 才算杀在窗口内；
若 `S(n)` 抢先出现，说明 hold 没生效，该轮放弃而不是硬杀。

## 6. 带上板的下一次（如果还要做）

换掉"文件当块设备"这条路：**腾出一个裸分区**（或裸 LV）作被测对象，
让断裂直接发生在被测文件系统的设备上，外层只有块层、没有第二层 ext2 缓存。
在拿到裸分区之前，不再在板上重复 loop 方案。

## 7. 本轮顺带确认的传输事实

* `fatwrite`（U-Boot 写 FAT）在写 64 MB 时**板子复位两次**、无 `bytes written`——
  U 盘不作为关键路径；U-Boot 的 USB 枚举本身也不稳（多次 `0 storage`，需热插拔）；
* 可靠的小文件通道：initramfs 里 `chroot /p2` 的 `gzip -9 | base64`，
  按 8 KB 分块、每块 md5，实测 65 KB 镜像约 10 秒、字节级一致。
  64 MB 镜像压缩后（ext2 镜像几乎全零）约 65 KB，足以取回；
* 厂商 RockOS 的启动项把根指向 p3，而 p3 长期是数据分区（只有 `kernel-*.img`、
  `node26`、`img` 等），因此 RockOS 起不来；救援环境改为 initramfs + `chroot /p2`。
