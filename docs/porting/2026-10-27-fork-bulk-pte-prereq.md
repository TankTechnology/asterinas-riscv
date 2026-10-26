# 整页 PTE memcpy 的两个前提：huge page 与引用计数泄漏

日期：2026-10-27
设计文档：`docs/porting/2026-10-27-fork-page-table-bulk-copy-design.md`（`271ec65aa`）
基线内核：`c00db36af` 源码构建；PoC 仅在本地 `wip/pte-bulk`

## 1. 任务 A：huge page 行为（结论：**否，当前没有 2 MiB 用户大页**）

### 1.1 证据链

| # | 位置 | 事实 |
|---|---|---|
| 1 | `ostd/src/mm/frame/mod.rs:158-161` | `Frame::map_level()` **恒返回 1**，注释写明 "Currently, the level is always 1, which means the frame is a regular page frame" → 帧分配器从不产生大页帧 |
| 2 | `ostd/src/mm/vm_space.rs:721-736` | `UserPtConfig::item_raw_info()` 用 `frame.map_level()` 作为 PTE 层级 → 用户页表的 tracked 映射层级恒为 1 |
| 3 | `ostd/src/mm/vm_space.rs:680-684` | `VmItem::new_untracked_io(..., level: 1)` → MMIO 映射层级也恒为 1 |
| 4 | `ostd/src/mm/page_table/cursor/mod.rs:431-447` | `map()` 的层级取自 item，断言 `level <= HIGHEST_TRANSLATION_LEVEL && level <= guard_level`；`split_if_mapped_huge` 只是防御性代码 |
| 5 | `kernel/core/src/syscall/mmap.rs:257,269-280` | `MAP_HUGETLB` **只出现在 `LEGACY_MMAP_FLAGS` 掩码里**（用于识别"已知但可忽略的旧 flag"），没有任何大页实现 |
| 6 | `kernel/core/src/syscall/memfd_create.rs:39-41` | `MFD_HUGETLB` 显式 `warn!` 不支持 |
| 7 | `kernel/core/src/vm/vmar/vm_mapping.rs:598,612` | 匿名缺页分配一律 `FrameAllocOptions::new().alloc_frame()`（4 KiB 基页） |

### 1.2 判定

**安全**。用户页表的 PMD 项只可能是"指向下级页表"或 absent，**不可能出现
Mapped 的大页**；因此把 4 KiB PTE 页整体 memcpy 不会把 PMD 当 PTE 处理。

**但实现必须保留运行时检查**：框架层（启动页表 `BootPt`、`split_if_mapped_huge`）
存在大页语义，将来若支持 `MAP_HUGETLB`，批量路径必须在按块检查时发现
`PteScalar::Mapped` 出现在 level 2 就回退到慢路径——设计文档 §2.1 已包含该检查，
不能省。

## 2. 任务 B：引用计数泄漏（基线对照）

`fork_bench_check.sh` 新增 `leak` 模式：fork 1000 次（子进程立即 `_exit`、
父进程 `waitpid`），读 `/proc/meminfo` 的 `MemFree` 前后差值。

| 内核 | fork 次数 | MemFree 前 | MemFree 后 | 差值 |
|---|---|---|---|---|
| 基线 `c00db36af`（run 1） | 1000 | 3,949,548 KiB | 3,949,316 KiB | **−232 KiB** |
| 基线 `c00db36af`（run 2） | 1000 | 3,949,476 KiB | 3,949,800 KiB | **+324 KiB** |
| fork-exec ×100（run 2） | 100 | — | — | +1,356 KiB |

两次 1000-fork 的差值一负一正（−232 / +324 KiB），说明基线本身没有单调增长，
**噪声带约 ±350 KiB**；判据"每次 >4 KiB 持续增长"在基线上不成立 → 基线无泄漏。

**PoC 侧结论**：`wip/pte-bulk` 分支目前只有 refcount 探针（`/proc/bulk_probe`），
**没有批量实现**，所以"PoC 侧泄漏"无法测——这一项必须作为批量实现的第一个
验收项。测试协议（实现后照此跑）：

```sh
/tmp/fork_bench leak 1000    # 期望 |delta| < 1 MiB，与基线同量级
/tmp/fork_bench forkexec 100
```

判据：若 `delta_kb` 随 fork 次数单调增长（例如每次 > 4 KiB），即 refcount
多算/漏减 → 必须修，不能合入。

## 3. 任务 C：边界用例（基线内核）

| 用例 | 结果 |
|---|---|
| 稀疏 VMA（256 MiB span、64 KiB 条带）fork | 2,048 条成功映射（128 MiB 已触碰），fork **70.8 / 72.9 ms**（两次），无 panic |
| 混合映射（栈 + heap + 8 MiB 文件私有映射） | fork 5.46 ms；子进程看到父数据并写 COW 页，**父进程仍看到自己的值**（无 `parent_visible`）；无 `child_bad` |
| fork-exec ×100（`/bin/true`） | MemFree +1,356 KiB（噪声），无 panic |
| COW 正确性（1 MiB 匿名 + heap） | `child_ok=1 parent_ok=1 heap_ok=1` |

> 过程中发现并修正了一个**测试自身**的 bug：`mixed` 首版把父进程期望值写成 0，
> 而该页本来就被填成 1，于是误报 `parent_visible`。修正后（父期望 1、子写 2）
> 连续三次运行都没有再出现，COW 语义正确。

## 4. 结论

**bulk memcpy 方案已被否决（用户 2026-10-26 红线：不得破坏 ostd 抽象）**

本文档仅作历史记录，不得据此实现；fork 保持在 `c00db36af`（512 MB = 204.7 ms）。




| 前提 | 结论 | 依据 |
|---|---|---|
| A. huge page | **通过（当前无 2 MiB 用户大页，整页 PTE memcpy 安全）** | `frame.map_level()` 恒为 1；`MAP_HUGETLB` 未实现；用户 PMD 只可能是页表指针 |
| B. 引用计数泄漏 | **基线无泄漏（±350 KiB 噪声带）；批量实现侧尚无法测** | 1000-fork 差值 −232 / +324 KiB；PoC 分支无批量实现 |
| C. 边界用例 | **全部通过** | 稀疏 VMA 128 MiB / 混合文件映射 / fork-exec×100 |

**结论：可以落地。** 但正式实现必须同时满足两条硬约束：

1. 保留"按 2 MiB 块检查 level"的运行时判定（未来若支持 `MAP_HUGETLB` 立即回退）；
2. **把 `fork_bench leak 1000` 作为批量路径的第一验收项**——基线参考值已就绪
   （|Δ| ≤ ~350 KiB），实现后的差值若超出该带并随次数增长即为 refcount 账目错误。

另外记录：本轮的 `sparse` 用例按 64 KiB 条带、隔一条映射一条，共 2048 条全部映射成功
（128 MiB 已触碰），fork 70.8–72.9 ms，与"按页线性"一致；实现批量路径时仍建议用真正中间有空洞的 VMA（不同 `mmap` 段之间留空）
来验证回退。
