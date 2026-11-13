# 缓冲读没有命中页缓存（2026-11-14，待修）

## 现象

同一个 64 MiB 文件，用 `mmap` 触碰所有页与用 `read()` 读，第二遍的行为完全不同：

| 遍 | `mmap`（逐页触碰） | `read()`（1 MiB 缓冲） |
|---|---|---|
| 第 1 遍 | 1409 MiB/s | 196 MiB/s |
| 第 2 遍 | **85002 MiB/s** | **196 MiB/s** |

`mmap` 第二遍 85 GB/s 说明**页确实驻留在内存里**；而 `read()` 两遍都是 196 MiB/s，
即每次调用都按 4 KiB 页重新走了一遍存储侧的开销（约 20 µs/页）。

同一批基准里 `fs_read` 也只有 193 MiB/s，而 Linux 同板同 fs 同二进制是 644 MiB/s
（Linux 侧那 64 MiB 是刚写完的，走页缓存）。

## 复现

```bash
# 板上
/tmp/mmapprobe      # 写 64 MiB + fsync，mmap 两遍、read 两遍
```
源码：`tools/riscv/perf/probes/mmapprobe.c`（另附 `cacheprobe.c`，三遍读）。

## 代码位置（下一步从这里查）

`kernel/src/vm/page_cache/vmo/mod.rs`：

- `Vmo::read` → `collect_pages(..., CommitMode::Read)` → `try_operate_on_range_internal`
  → `try_commit_with_cursor`：只有在 `page.is_uninit()` 时才返回 `WaitUntilInit`。
- 因此「已驻留且非 Uninit」的页不应触发 I/O；`read()` 仍然慢，说明要么页在
  读路径之后被移出 XArray，要么每次 `commit_range` 都重新提交了 I/O。
- 相关：`wait_for_writeback_and_remove_pages`（写回后移除页）、
  `CachePage::set_up_to_date` / `set_dirty` 的状态迁移。

## 为什么没有当场修

- 时间在凌晨 5 点，VMO 提交状态机一旦改错会直接影响文件内容正确性
  （错页/丢写），而板上没有可用的 fsck 快速回滚路径。
- 现有证据只覆盖「写后读」和「冷读」，还没有区分「写回时移除页」与
  「读路径重复提交 I/O」两种可能；需要在内核里加一个每页计数探针再判断。

## 建议的下一步（按顺序）

1. 在 `try_commit_with_cursor`（backed VMO 版本）里计数：命中驻留页 /
   返回 `NeedIo` / `WaitUntilInit` 各多少次，读一次 64 MiB 文件看分布。
2. 若大量 `NeedIo`：检查写回后页是否被移除（`wait_for_writeback_and_remove_pages`
   的调用点），只在 truncate/invalidate 时移除才正确。
3. 若命中驻留但每次仍慢：测单页 `read_fallible` 的拷贝成本，确认瓶颈在拷贝
   还是锁/`Arc` 操作。
4. 修好后用 `cacheprobe`（三遍读）与 `fs_read` 验收，并更新
   `board_al_baseline.json` 的 `fs_read` 门禁。

预计收益：顺序读 193 MiB/s → 接近内存带宽；目录查找（每块 15 µs 的页缓存读）
也会一并受益，N2 里那 76–110 µs 的目录查找还能再降一个量级。
