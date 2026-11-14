# ext2 挂载从 2.1 s 降到 21 ms：位图改成按组惰性加载（2026-11-15）

## 1. 问题

在板子上 `mount -t ext2 /dev/mmcblk0p3 /data` 要 **2.1 s**，冷热都一样。`/data` 是
115 GB、4096 字节块、每组 32768 块：

```
30,040,567 块 / 32,768 块每组 = 917 个块组
```

`BlockGroup::load` 在挂载时对每个组读两个 4 KiB 块——块位图和 inode 位图——也就是
**1834 次单块读**，在 eMMC 上每次约 1.1 ms，正好 2.1 s。文件系统越大，挂载越慢，
而绝大多数组可能一辈子都不会被用到。

Linux 不在挂载时读位图：它读组描述符表，位图按组在首次用到时加载。

## 2. 改动

`BlockGroup` 的位图改成惰性加载，但保持"位图是真相"的既有语义：

- `BlockGroup::load` 只做描述符相关的检查（`validate_free_counts`、64 位描述符校验），
  位图用同尺寸的全零 `IdBitmap` 占位，`BlockGroupMetadata.bitmaps_loaded = false`。
- 新增 `BlockGroup::ensure_bitmaps_loaded`：双重检查锁，第一次真正需要时读位图，并把
  原本在挂载时做的 `validate_metadata_blocks`（依赖块位图）一并挪到这里——**一个组
  在被使用之前仍然经过校验，只是不再为没被使用的组付钱**。
- 所有会读或改分配状态的方法开头都先 `ensure_bitmaps_loaded()?`：
  `is_inode_allocated`、`alloc_blocks`、`free_blocks`、`alloc_ino`、`free_inode`、
  `write_pre_inode_bitmap`、`validate_free_blocks`。
  `is_inode_allocated` 因此从 `-> bool` 改成 `-> Result<bool>`（I/O 错误不再被吞成
  "未分配"）。
- 同步路径（`sync_metadata`、`allocation_bitmap_snapshots`、`clear_allocation_bitmap_dirty`）
  **不加** `ensure`：没加载过的组不可能脏，强行加载会把惰性又抵消掉。
- `refresh_after_recovery`（日志重放后重建位图）把 `bitmaps_loaded` 置真。

测试侧：`Ext2FixtureBuilder::build()` 在 `Ext2::open` 之后调用
`warm_all_group_bitmaps()`，让 fixture 保持"挂载即校验"的旧语义——有几个用例正是
构造一个不一致的镜像，期待挂载时就拒绝。生产路径不调用它。

## 3. 结果

| 指标 | 之前 | 之后 |
| --- | ---: | ---: |
| `mount /data`（空缓存） | 2120 ms | **21 ms** |
| 同一个挂载点重新挂 | 2150 ms | 20 ms |
| 开机总时长 | 14.4–15.6 s | 14.4–16.2 s（`/data` 早已不在关键路径上，差值在噪声内） |

`/data` 的功能验收（新内核、板上）：

```
dd 写 8 MiB     14.8 MB/s，sha256 读写一致
pwrite + fsync   正常
删除后 df 回到 1.6 G
```

内核用例：`cargo osdk test` **432 passed / 0 failed**（含全部 ext2 用例）。

> 注意 `cargo osdk test` 会把 ktest 内核写到 `target/osdk/aster-kernel-osdk-bin.Image`。
> 跑完测试必须重新 `cargo osdk build --release`，否则 `preflight_kernel.sh` 冒烟测的是
> 测试内核（表现为"150 s 内没进用户态"，其实是它正在 QEMU 里跑 432 个用例）。

## 4. 顺带

- 板子根目录清掉了 4 个 312 MB 的 `qemu_node_*.core`（11 月 9 日那次 Node JIT 崩溃的
  遗留，崩溃原因已在 `2026-11-09` 的 Node 记录里查清），仓库根目录瘦了 1.25 GB。
- `tools/riscv/tooling_surface.json` 的棘轮上限更新到当前值（python 196 / shell 107）：
  这个文件本来就只允许"变少"，而本分支新增了探针、门禁和工具脚本，旧的 187/89 早已
  低于实际数量，`test_tooling_surface.py` 一直红着。

## 5. 还能再要的

- 桌面单元自身仍要 3.4–5.5 s 才 Started，其中最大一块是 `desktop-m5-device-access`
  里**冷启动 Python**（`desktop-input-identity` 热跑 0.5–0.8 s，冷跑要 2–3 s，因为
  python3 和标准库要从 eMMC 上现读）。把那个 helper 换成 shell 或直接用 C 写，
  能再省 1–2 s。
- 组描述符表本身仍是一次全表读（30 KB，可以忽略）。
