# fork 页内批量 PTE：方案对比、实现与数字

日期：2026-10-27
上一轮：`c00db36af`，512 MB fork = 204.7 ms（1.56 µs/页）
本轮：见 §3

## 1. 任务 A：三个方案对比

| 方案 | API 形状 | 借用检查 / 实现难点 | 结论 |
|---|---|---|---|
| **A1 `protect_range_batch`** | `CursorMut::protect_run(max) -> Vec<(Vaddr, Paddr, PageProperty)>`（内部一次扫描当前 PTE 页） | 不需要返回任何借用：只把 `(vaddr, paddr, prop)` **拷贝**出来；游标位置本来就有（`self.0.va` + `pte_index`），无需额外索引。**没有借用冲突**：扫描结束后再改属性/前进。 | **采用** |
| A2 `Frame::entries_mut() -> &mut [Pte]` | 把页表页裸数组交给上层 | 破坏封装；上层（kernel）被 `#![deny(unsafe_code)]` 禁止碰裸 PTE；而且映射的 **item 所有权**（`VmItem` + 帧引用计数）无法在页表层之外构造 | 否 |
| A3 `walk_contiguous()` 迭代器产出 `(vaddr, &mut Pte)` | 迭代器持有 node guard | 借用检查最麻烦：`&mut Pte` 的生命周期绑定在 guard 上，循环体里同时要操作**另一个**游标（dst）并调用 `map`，跨借用的 `&mut` 无法安全表达（要么整段逻辑塞进闭包，要么用 unsafe 裸指针） | 否 |

**选 A1 的理由**：不变式最简单——扫描只读+原地改 W 位，结果全是 owned 值；
不引入新的 unsafe 生命周期，也不动 `PageTableConfig` 的 `Item`/`ItemRef` 约束。

### 1.1 关键发现：`ItemRef` 无法复制成 owned item

`PteStateRef::Mapped` 给的是 `C::ItemRef<'a>`，而 `PageTableConfig::Item` 只有
`Send` 约束（没有 `Clone`/`from_ref`），所以"从源映射复制一份给子进程"不可能
在**通用**层完成。解决办法：绕开 item，直接读**裸 PTE**：

- `Entry::mapped_raw_info() -> Option<(Paddr, PageProperty)>`（新增，内部用
  `PteScalar::Mapped(paddr, prop)`）；
- `ostd::mm::frame::dup_frame_ref(paddr) -> UFrame`（新增）：
  `inc_frame_ref_count(paddr)` + `Frame::from_raw(paddr)`，即"多拿一个引用"，
  交给 `dst.map` 后所有权转移给子进程的 PTE。

## 2. 任务 B：实现

1. **源端批量**（`ostd/.../page_table/cursor/mod.rs`）：`protect_run(max)`
   从游标当前页出发，逐条 `mapped_raw_info()` → 应用 `op`（清 W）→ 返回
   `(va, paddr, 保护后的 prop)`；遇**空洞 / 页表页边界 / 巨页 / MMIO** 停止，
   由调用方回退到原慢路径（语义不变）。
2. **目的端**：仍用 `dst.map(frame, prop)`（每页 510 ns，见 §4 卡点）；
   帧由 `dup_frame_ref(paddr)` 取得，引用计数正确。
3. **保留的语义**：源 PTE 清 W（COW 触发）、目的 PTE 带同样的"只读"属性、
   页表页按需分配（`map` 内部 `alloc_if_none` 不变）、`mprotect` 路径未改。

## 3. 任务 C：数字（结论：**没有收益，已回退**）

| 指标 | `c00db36af`（基线） | 本轮批量 `377bd598` | 变化 |
|---|---|---|---|
| 512 MB fork | **204.7 ms** | 249.9 ms | **+22%** |
| 每页（512 MB） | **1.56 µs** | 1.91 µs | +22% |
| 256 MB fork | 101.6 ms | 124.9 ms | +23% |
| 64 MB fork | 28.9 ms | 31.3 ms | +8% |
| reserve 512 MB（同 run 对照） | 1.62 ms | 1.90 ms | +17%（主机噪声） |
| mprotect 512 MB（同 run 对照） | 106.0 ms | 113.5 ms | +7%（主机噪声） |
| COW 正确性 | child/parent/heap = 1 | child/parent/heap = 1 | 通过 |

即使扣掉同 run 对照的 +7–17% 主机噪声，批量版仍是 **≥1.7 µs/页，比基线的
1.56 µs 慢**。原因：

1. 上一轮的"连续页快路径"（`c00db36af`）已经在常见情况下免掉了每页的
   `find_next`；本轮 `protect_run` 并没有再省掉什么；
2. 它仍然**逐条** `cur_entry()` + 解码 PTE + `protect` + `move_forward`，
   成本与逐页 `protect_cur` 相当，却多了一次 `Vec` 分配（每个页表页一次）
   和一层泛型调用；
3. 目的端仍是 `dst.map`（510 ns/页），没动。

**因此没有把批量版合入**：`fork.rs` 与 ostd 的四处改动已全部回退，工作树
回到 `c00db36af`（512 MB = 204.7 ms、COW 全 ok、7 域矩阵已验证全绿）。

## 4. 卡点与下一步（有数字依据）

本轮没有被**借用检查**卡住（A1 方案确实做到了"无借用冲突"）；卡住的是
**收益**：源端在上一轮之后只剩 ~300 ns/页，而目的端 `dst.map` 510 ns/页 +
逐条 PTE 触碰才是主体。

真正能到 <80 ms 的做法（Linux 级）：**按页表页整体复制**——把源 PTE 页
4 KiB 直接 memcpy 到目的 PTE 页、同时对每个被映射帧做一次引用计数递增
（约 20 ns × 512 = 10 µs/页表页），再对源页整页清 W。估算：
131072 页 × (20 ns 复制/512 + ~80 ns 引用计数) ≈ **15–25 ms**。
代价是必须绕开现有 item 抽象：直接对 `C::E` 数组操作，并保证每 512 项中
遇到空洞/巨页/MMIO 时分裂回单条路径；这是 ostd 页表层的一次结构性改造
（不是 fork 局部改动），风险与工作量都远超本轮窗口。

下一步建议：
1. 先在 ostd 增加 `PageTableConfig::dup_item_from_raw(paddr, prop)` 语义确认
   （现在 `item_from_raw` 假定所有权已存在，配合 `dup_frame_ref` 可用）；
2. 实现 `CursorMut::copy_page_run(dst, src)`：仅当两端都在 base level 且
   页表页对齐时启用，内部用 `read_pte`/`write_pte` 批量循环；
3. fork 只在满足条件时调用，其余一律回退到 `c00db36af` 的路径。
