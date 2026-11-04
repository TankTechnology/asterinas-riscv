# 文件系统/页缓存锁序检查单

日期：2026-11-04

用途：改动内核里任何会嵌套取锁的代码前，先看这张表；新增锁或新增嵌套时，
把它加进 `tools/riscv/concurrency-models/lock-order/` 并跑一遍模型。

## 总序（由外到内）

```text
futex bucket（按桶索引 0..63，只在 futex syscall 内出现）
  → Vmar::inner                      （fork/mmap/munmap 写；缺页读）
    → ext2 Inode::inner              （文件 I/O、截断、回写）
      → VMO pages（XArray）           （查/装页，短暂持有）
        → 页锁 LockedCachePage        （一次只锁一页）
          → BlockPtrTree              （页缓存 backend 的块查找）
            → IndirectBlockManager
              → Ext2::super_block
                → BlockGroup::metadata
```

模型的常量与此一一对应（`tools/riscv/concurrency-models/lock-order/src/lib.rs`）。

## 各层与证据

| 层 | 位置 | 谁持有 | 备注 |
| --- | --- | --- | --- |
| futex bucket | `futex.rs` `lock_bucket_pairs` | `FUTEX_WAIT/WAKE/REQUEUE` | 两桶按索引排序；同桶只锁一次；`futex_wait_bitset` 在缺页前**释放**桶锁 |
| `Vmar::inner` | `vmar_impls/fork.rs`、`page_fault.rs` | fork（写）、缺页（读） | `cow_copy_pt` 持写锁完成保护+复制+TLB flush |
| `Inode::inner` | `ext2/inode/mod.rs` | 文件读写、截断、回写 | ext2 模块文档已有跨层顺序，但**漏了页锁与 XArray**（本检查单补全） |
| VMO `pages`（XArray） | `page_cache/vmo/mod.rs` | `commit_on*`、`resize`、`collect_pages` | 只短暂持有：查/装页后即释放，再锁页 |
| 页锁 | `page_cache/cache_page.rs` | 页缓存读写、回写、等待回写 | 数据路径中一次只锁一页（`for … drain` 循环逐页 drop） |
| `BlockPtrTree` | `ext2/inode/block_manager` | 页缓存 backend `lookup_block` | 在页锁内取；不反向 |
| `super_block` / `BlockGroup::metadata` | `ext2/fs.rs`、`block_group.rs` | 块分配/释放 | `BlockGroup::inode_cache` 独立，不参与本序 |

## 已按此序核对过的路径

1. **私有文件页缺页**：VMAR 读锁 → 页表游标 → XArray → 页锁 → `BlockPtrTree`。
2. **缓冲写/读**：`Inode::inner`（写）→ 页锁 → `BlockPtrTree` → …, metadata。
3. **截断（shrink）**：`inner`（写）→ 页缓存 resize（XArray → 页锁 → 释放）→ `BlockPtrTree` → metadata。
4. **回写等待/移除**：页锁（等待回写）→ 释放 → XArray（移除页）。
5. **共享 mmap 写缺页**：VMAR 读锁 → `mark_mmap_writable_page`（XArray → 页锁）→ 页表。
6. **futex 等待**：桶锁 → 释放 → 缺页路径（VMAR → …）。

第 1–6 条都以 `Step` 序列写进模型，任何一条违反总序测试即失败；同时有 7 条
"必须被拒绝"的反向序列作为阴性对照（页锁内取 XArray、`BlockPtrTree` 内取页锁、
页锁内取 inode/VMAR、VMAR 内取 futex 桶等）。

## 未覆盖 / 已知缺口

1. **`PageCache::resize` 没有反向映射锁**（源码自带 TODO）：截断与并发缺页之间
   没有同步，属于**功能层**缺口而非锁序倒置；当前靠 `Inode::inner` 串行文件
   操作，但缺页路径不经过 `inner`。
2. **`write_page_async` 的 TODO**：并发截断与回写不支持（`idx >= npages` 返回
   `EINVAL`）。
3. **未审计的锁**：dentry/mount（`RwMutex`/`RwLock`）、notify、fs registry、
   socket 注册表、设备/块层锁。它们不在本表内，新增嵌套前需要单独补表。
4. **模型是策略级**：全序成立 ⇒ 无死锁环，但不枚举调度交错；`lock_bucket_pairs`
   这类"两把锁"的路径另有 futex-bucket loom 模型覆盖行为。

## 改动规则

- 新增嵌套取锁：在总序中插入一层，更新本表与模型常量；若无法插入（存在两种
  顺序），就是死锁风险，必须先改代码。
- 释放后重取的锁不算嵌套（如 resize 里 XArray → 页锁 → XArray），模型用
  `Release` 步骤表达。
- 跨子系统（FS ↔ 网络 ↔ 进程）的调用不得在持锁时反向回调。
