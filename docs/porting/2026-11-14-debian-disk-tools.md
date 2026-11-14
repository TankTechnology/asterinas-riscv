# 让标准 Debian 磁盘工具能用（2026-11-14）

## 1. 问题

板上 `lsblk` 直接退出：

```
lsblk: failed to access sysfs directory: /sys/dev/block: No such file or directory
```

`blkid` 返回空，`iostat` 没有数据。包都是好的，缺的是内核对外暴露的目录。

## 2. 缺的是什么

| 缺失项 | 谁在用 | 之前的表现 |
| --- | --- | --- |
| `/sys/dev/block/<major>:<minor>` | `lsblk` 的入口 | 直接退出 |
| `/sys/class/block/<name>` | `blkid`、mount 助手、`fdisk` 一类 | 找不到设备 |
| `/proc/diskstats` | `iostat`、`vmstat -d`、`sar`、监控脚本 | 文件不存在 |
| loop 设备节点的 `dev` 属性 | 一切按号找设备的工具 | 条目被列出后丢弃 |

`/sys/block` 本身是完整的（`dev`/`size`/`ro`/`stat` 都有），缺的是**两层索引**和**统计表**。

## 3. 改动

- `kernel/src/device/block_sysfs.rs`：每发布一个 `/sys/block/<name>`，同时发布
  `/sys/class/block/<name>` 与 `/sys/dev/block/<major>:<minor>` 两个符号链接，
  指向 `../../block/<name>`；分区多一层（`../../block/<disk>/<part>`）。
- `kernel/src/device/sysfs.rs`：`/sys/class` 与 `/sys/dev` 改为**单入口取或建**。
  之前 `/sys/dev` 只在有 DRM 节点时创建，且两者都是"先建后挂"，块设备一旦早于
  device init 发布链接就会重复建节点（实测直接 panic）。现在任何顺序都安全。
- `kernel/src/device/loop.rs`：loop 节点原先只有 `ro`/`size`，补上
  `dev`/`removable`/`range`/`stat`/`inflight`。**`dev` 是关键**：`/sys/dev/block/…`
  解析到的就是它，没有它 loop 设备会被"列出又丢掉"。
- `kernel/comps/block/src/iostats.rs`（新）：按设备 id 的 I/O 计数器。
  一次 bio 提交记 in-flight，完成时按方向累加请求数与扇区数；**失败的请求不计入**
  读写总数（`iostat` 期望如此）。
- `kernel/src/fs/fs_impls/procfs/diskstats.rs`（新）：输出 iostat 解析的 14 列
  （合并数与三个时间列为 0，内核没有请求级时间戳）。
- `/sys/block/*/stat` 改用同一份计数，不再是常量 0。

## 4. 验证

**QEMU 先行**（`tools/riscv/perf/probes/devprobe.c`，静态二进制当 init，
带一块 virtio 盘启动）：

```
ok   /sys/class/block lists devices
ok   /sys/dev/block lists devices
ok   class and dev listings agree
ok   /sys/dev/block entries resolve through the symlink
ok   every dev/block entry has a readable dev file
ok   every class/block entry exposes attributes
ok   /proc/diskstats has lines
ok   every diskstats line has fourteen fields
ok   diskstats covers the devices sysfs lists
DEVPROBE OK
```

探针在 QEMU 里抓到一个真实缺陷：loop 节点缺 `dev`，说明"列出但解析不了"。

**真机**（同一探针 9/9 通过）上跑真实工具：

```
$ lsblk
mmcblk0     252:0    0 119.1G  1 disk
├─mmcblk0p1 252:1    0   500M  1 part
├─mmcblk0p2 252:2    0     4G  1 part /
└─mmcblk0p3 252:3    0 114.6G  1 part /data

$ blkid
/dev/mmcblk0p1: UUID="17e0bc64-…" BLOCK_SIZE="1024" TYPE="ext4"
/dev/mmcblk0p2: LABEL="ASTER_BROWSERWEB" … TYPE="ext2"
/dev/mmcblk0p3: UUID="4ddde1a8-…" TYPE="ext2"

$ iostat
Device      tps    kB_read/s   kB_wrtn/s    kB_read   kB_wrtn
mmcblk0p2  143.04   2578.78      387.16      324952     48786
```

**计数精度**：`dd if=/dev/mmcblk0 of=/dev/null bs=1M count=16 iflag=direct` 后，
`mmcblk0` 的读扇区数正好 +32768（16 MiB ÷ 512 B），与 sysfs 的 `stat` 同步变化。

顺带确认：`blkid` 把 p1 识别为 **ext4**（`BLOCK_SIZE=1024`）——我们内核一直挂不上的
那个分区，至少它的元数据是可读的。

## 5. 仍然缺的

`vmstat` 依旧报 `Unable to create vmstat structure`：它要 `/proc/vmstat`，属于下一项
（连同 `/proc/zoneinfo`、`/proc/buddyinfo`）。`/proc/diskstats` 就位之后，
`vmstat -d` 的磁盘部分已经能用了。
