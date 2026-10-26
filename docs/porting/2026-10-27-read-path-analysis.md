# 冷读为什么慢：页缓存缺预读、缺页按页同步取

日期：2026-10-26
内核：`e8b4a8194c0918ac`
数据来源：`2026-10-27-read-microbench.md`（Asterinas 冷读 libxul 132.5 µs/4 KiB，Linux 5.3 µs）

## 1. 调用链（文件:行号）

| 层 | 位置 | 行为 |
|---|---|---|
| ext2 文件读 | `kernel/core/src/fs/fs_impls/ext2/inode/file.rs:329` | `self.page_cache().read(offset, writer)?` |
| 页缓存适配 | `kernel/core/src/vm/page_cache/mod.rs:348` | `VmIo::read` 转调 VMO |
| VMO 读 | `kernel/core/src/vm/page_cache/vmo/mod.rs:534` | 分页 `collect_pages`，每页一次 `read_fallible`（单次用户拷贝） |
| 批量上限 | `kernel/core/src/vm/page_cache/vmo/mod.rs:531` | `PAGE_BATCH_CAPACITY = 32`，**只对已在缓存的页生效** |
| 缺页 | `kernel/core/src/vm/page_cache/vmo/mod.rs:818-829` | `handle_commit_error` → `backend.read_page(index, …)`，**单页、同步** |
| 块设备提交 | `kernel/core/src/vm/page_cache/mod.rs:676-694` | `read_page_async` → 每页一个 bio segment |

## 2. 缺预读（决定性）

```sh
$ grep -rni "readahead\|read_ahead\|prefetch" kernel/core/src/vm/page_cache
（无输出）
```

顺序冷读因此退化成「每次 4 KiB `read()` → 一个 4 KiB bio → 等待」，实测 132.5 µs/页，
整份 164 MB 的 libxul 需要 5.31 s（31 MB/s）；Linux 同条件 0.21 s（780 MB/s）。

## 3. 修复方向（本轮不做，只记录）

1. 顺序检测 + 预读：按 `read` 流的连续性，对后续 N 页发起批量 `read_pages_async`
   （`PageCacheBackend::read_pages_async` 已存在，`page_cache/mod.rs:687`）；关键是**首读也走批量**
   ——现在批量只覆盖已在缓存中的页。
2. 缺页批量：`handle_commit_error` 现在一次一页，可改为把 `current_range` 内连续缺页合并成
   `PageRun` 一次提交。
3. 固定开销（热读 4.6 vs 0.59 µs）另立一项：VFS 全链路 + 页缓存查页的常数成本。

## 4. 影响面

- Firefox 冷启动 `read` 5710 次 / 3.19 s CPU 的主因；
- 任何顺序读大盘文件的场景（dpkg 安装、apt、解压）都会命中同一条路径。
