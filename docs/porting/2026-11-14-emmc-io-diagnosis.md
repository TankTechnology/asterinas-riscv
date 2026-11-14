# eMMC I/O 故障诊断与修复（2026-11-14）

## 1. 结论先行

板子的 eMMC **没有被判死刑**。11-14 全天只读验证显示介质可读、历史报错扇区全部正常；
真正把根文件系统写坏的是驱动的三处缺陷：

1. **所有数据等待共用 1 秒固定超时**（`IRQ_WAIT_TIMEOUT`），而这块卡在持续写时只有
   ~1 MB/s，一个 512 KiB 的 SDMA 传输本身就要 0.5–1 s → 健康传输被判超时；
2. **失败后的控制器恢复函数 `reset_data_line()` 定义了却从未被调用**（死代码），
   所以一次失败之后数据线一直忙、卡片等不到停止命令，后续传输继续失败——这正是
   "一次写错，之后整机 EIO" 的成因；
3. **块层没有重试**：一次瞬时超时直接变成 `BioStatus::IoError` → 页保持 dirty →
   ext2 元数据在后续写回中被写坏（`refusing to free already-free block`）。

修复后同一条命令：写 256 MiB 从「138 MiB 处失败、917 kB/s」变成「全部成功、6.2–6.8 MB/s」，
启动期 I/O 错误从 20 条降到 0–3 条，ext2 元数据损坏不再出现。

## 2. 介质是好的（只读证据）

| 检查 | 结果 |
| --- | --- |
| 整分区顺序读（4.0 GiB） | 207 s，20.8 MB/s，**0 错误** |
| 历史报错 LBA（10 个，含超级块 `2..3`）用 O_DIRECT 重读 | **全部成功** |
| 4 路并发 O_DIRECT 读 | **0 错误** |
| 失败区域的读回（写失败后的 138 MiB） | 20.6 MB/s 正常 |

历史错误里还有**垃圾 LBA**（`sectors 14935472224..14935472297`，远超设备容量），
这不可能来自真实介质错误，只能来自驱动传入的无效请求——与上面的判断一致。

## 3. 修复内容（`kernel/comps/mmc/src/arch/riscv.rs`）

| 缺陷 | 修改 |
| --- | --- |
| 1 秒固定超时 | `data_wait_timeout(bytes)`：按 64 KiB/s 的悲观速率预算时间，下限 1 s、上限 30 s |
| `reset_data_line()` 从未调用 | 数据等待失败、以及每次重试之间都调用，恢复数据线与卡片状态 |
| 无重试 | `submit_sdma` 内最多 4 次尝试，每次之间 2 ms 退避（等卡片把页编程完） |

## 4. 实测（同一条命令，修复前后）

```
dd if=/dev/zero of=/root/scratch.bin bs=1M count=256 oflag=direct
```

| | 修复前 | 修复后 |
| --- | --- | --- |
| 结果 | 138 MiB 处 `rc=1` | `rc=0`，256 MiB 全部写完 |
| 吞吐 | 917 kB/s | **6.2–6.8 MB/s** |
| I/O 错误 | 1 条（并伴随元数据损坏） | 0 |
| 并发读干扰下 | — | 6.2 MB/s，0 错误 |

启动期（桌面 + Firefox 同时起来的那 3 秒）：

| 启动 | I/O 错误 | 重试 | ext2 元数据损坏 |
| --- | --- | --- | --- |
| 修复前 | 20 | — | 有 |
| 仅加超时+恢复 | 3 | 10 | 无 |
| 加超时+恢复+重试 | **0** | 0 | 无 |

## 5. 仍然未知 / 未做

- **介质寿命仍未知**：`EXT_CSD` 的 `DEVICE_LIFE_TIME_EST` / `PRE_EOL_INFO` 没有读出来
  （需要从 U-Boot 侧读，我们的内核没有暴露）。读到之前，不能说"这块盘还能用多久"。
- 启动期错误数量在 0–3 之间波动，说明还可能存在更深一层的竞态；目前 ext2 已不再被写坏，
  但还不能断言根因已全部消除。
- `/home/asterinas/.mozilla/` 下有两个今天复位产生的损坏配置目录（213 MB，内容已删，
  剩下 3–5 个删不掉的 ESTALE 目录项）——这是硬复位 + ext2 无日志的必然结果，
  与驱动修复无关，不会自行消失。
- **单点仍在**：内核镜像只存在于 p2，p2 全坏则 U-Boot 无内核可载。把一份内核 +
  initramfs 放到 p1（需要在 U-Boot 里写一次）是当前唯一能消除它的手段。

## 6. 第二根因：读-改-写把 ToDevice 的段交给 Read bio（修复于同日）

第 5 节留下的"e2fsck 最后一步写不回超级块"在同一天查清并修好了。它不是介质问题，
也不是超时问题，而是一个**方向校验**的必然失败。

现象：`e2fsck` 前 5 个 pass 全部正常（18 秒），最后
`Error writing file system info: Input/output error`，内核日志里对应一条
`failed to do Read ... (sectors 2..3)`。

定位过程（加两行日志即可，`kernel/comps/block/src/impl_block_device.rs`）：

```
[block] write needs read-modify-write: offset=1036 len=2 first=true last=false
[block] preserved-sector read failed: sector=2 (read-modify-write path)
```

`offset=1036` 正是超级块里 `s_free_blocks_count` 的位置——**e2fsprogs 只写 2 个字节**，
属于非扇区对齐写，内核为此走"保留扇区"路径：先把该扇区读进来，改动后再整体写回。

`read_sector_for_write()` 用 `alloc_write_segment()` 分配了 **ToDevice** 方向的段，
然后把它交给一个 **Read bio** 去填。而 `BioSegment::write_from_device()` 的第一句是：

```rust
if self.inner.direction != BioDirection::FromDevice {
    return Err(Error::AccessDenied);
}
```

所以**任何非扇区对齐的块设备写都 100% 失败**，和介质、和超时都无关。

修复：读段用一个 `FromDevice` 的临时段，读完后经一个扇区大小的缓冲区复制进
`ToDevice` 的写段（一个段只携带一个 DMA 方向）。多一次 512 字节拷贝，只在非对齐
写时发生。

验证：

| 检查 | 修复前 | 修复后 |
| --- | --- | --- |
| `e2fsck` p3 | 每次停在 `write file system info` | 完整跑完，`Filesystem state: clean` |
| `mount` p3（可写） | `EUCLEAN: requires repair before writable mount` | `ext2 (rw,relatime)` |
| p3 写 32 MB | — | 14.9 MB/s，md5 一致，0 错误 |
| 启动期块错误 | 1 | 0 |

**这同时解释了历史上最吓人的现象**：p2 的两次离线 `e2fsck` 都卡在
"write file system info"、rc=9——那时以为是 eMMC 寿命到了，实际上根分区**从来就没
被真正修复过**，因为这条路径根本走不通。修好之后，根分区重新具备可修复性。

诊断用的 `[block] write needs read-modify-write` 日志保留：这条路径罕见，出现即说明
有工具在写不满一个扇区，值得知道。
