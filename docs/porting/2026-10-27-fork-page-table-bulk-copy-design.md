# 设计：fork 时页表页整页复制（PTE-page bulk copy）

> **bulk memcpy 方案已被否决（用户 2026-10-26 红线：不得破坏 ostd 抽象）。**
> 本文档仅作历史记录，不得据此实现；fork 保持在 `c00db36af`（512 MB = 204.7 ms）。

日期：2026-10-27　状态：**已否决（见上方红线）——仅存档**

## 1. 动机与负结果回顾

| 版本 | 512 MB fork | 每页 | 备注 |
|---|---|---|---|
| 原始（`e783db15f` 之前） | 272.8 ms | 2.08 µs | 逐页 find/query/protect/jump/map |
| `e783db15f` | 226.7 ms | 1.73 µs | 去掉每页一次 find + 冗余 jump |
| `c00db36af` | **204.7 ms** | **1.56 µs** | 连续页免搜索（当前基线） |
| `377bd598`（A1 批量） | 249.9 ms | 1.91 µs | **更慢，已回退**（`1337a9dc1`） |

插桩拆解（512 MB，`docs/porting/2026-10-27-fork-batch-pte.md` §1.1）：
`find 433 ns`｜`query 290 ns`｜`protect_cur 304 ns`｜`dst.map 510 ns`。

A1 的负结果说明：**只要还是逐条碰 PTE，省不掉**。剩下的量级差距只能靠
"页表页整页复制"（Linux 的 `copy_page_range` 思路）。

## 2. 方案

### 2.1 前置条件（不满足即回退慢路径）

对每个 2 MiB 对齐的块（512 个 PTE）：

1. 源、目的游标都位于 leaf level（`path[0]` 是 PTE 页 guard）；
2. 该块的 PMD 项不是巨大的 mapped page（`PteScalar::Mapped` at level 2）；
3. 块内没有 `AVAIL1`（MMIO）项；
4. 块的 VA 在本次 fork 的 mapping 范围内且 2 MiB 对齐（首尾部分页走慢路径）。

### 2.2 数据流（伪代码）

```rust
// ostd/src/mm/page_table/cursor/mod.rs（新增，unsafe 局部化在 ostd 内部）
pub(crate) unsafe fn copy_cow_page(&mut self, src: &mut Self, va: Vaddr) -> Option<usize> {
    // 1. 两端定位到 va 所在的 PTE 页，拿到 leaf guard 与页内索引
    let src_guard = src.leaf_guard_mut();      // path[0]
    let dst_guard = self.ensure_leaf_page_mut(va)?;  // 必要时分配页表页并挂到父级

    // 2. 读源 PTE 页（4 KiB）
    let mut buf: [C::E; 512] = src_guard.read_page();
    let mut present = 0;

    for e in buf.iter_mut() {
        if let PteScalar::Mapped(paddr, mut prop) = e.to_repr(1) {
            if prop.priv_flags.contains(AVAIL1) { return None; }  // MMIO：回退
            inc_frame_ref_count(paddr);        // 每帧一次引用计数（§3.2）
            prop.flags -= W;                   // 源、目的都只读
            *e = C::E::from_repr(&PteScalar::Mapped(paddr, prop), 1);
            present += 1;
        }
    }

    // 3. 写回源（写保护，COW 触发）+ 写入目的页
    src_guard.write_page(&buf);
    dst_guard.write_page(&buf);
    dst_guard.set_nr_children(present as u16); // 目的页的 present 计数
    Some(present)
}
```

关键接口（现有代码位置）：

| 接口 | 现状 | 说明 |
|---|---|---|
| `PageTableGuard::read_pte / write_pte` | `pub(super)`，`ostd/src/mm/page_table/node/mod.rs:208/229` | 单条读写；批量版需要 `read_page/write_page`（4 KiB memcpy 到 `[C::E; 512]`） |
| `Cursor::path` / `level` / `guard_level` | `ostd/src/mm/page_table/cursor/mod.rs:60-84` | leaf guard 即 `path[0]`，页内索引由 `pte_index(va, 1)` 得出 |
| `inc_frame_ref_count(paddr)` | `ostd/src/mm/frame/mod.rs:369` | 已存在，`pub(in crate::mm)`，直接可用 |
| `C::E` / `PteScalar` | `ostd/src/mm/page_table/...` | 需要 `from_repr/to_repr`，已在 entry.rs 内部使用 |

### 2.3 引用计数怎么批量加

没有批量 API；`inc_frame_ref_count` 就是对 `MetaSlot::ref_count` 的一次
`fetch_add`（`ostd/src/mm/frame/meta.rs:78`，`inc_ref_count` 在 `meta.rs:310`）。
批量版就是**每帧调用一次**——无法合并，因为每个帧的元数据是独立缓存行。

成本取决于元数据是否在缓存：同为顺序分配、顺序映射的 PTE 页，通常能命中
L2/L3（元数据页按 paddr 哈希到固定页），估计 30–80 ns/帧；最坏 200 ns。

### 2.4 与现有 map 的差别

现有 `dst.map` 每页做：`item_raw_info` → `VmItem::new_tracked`（UFrame 已 +1）
→ `PteState::Mapped` → `replace`（nr_children ±1）→ `write_pte` → `move_forward`，
510 ns/页。批量版把这些压成：一次 4 KiB memcpy（~500 周期，摊到 512 页 ≈ 1
ns/页）+ 每帧一个原子加 + 一次 `nr_children` 写入。

## 3. 抽象破坏面

| 被绕过的抽象 | 具体类型 | 暴露方式 |
|---|---|---|
| `Item` 所有权 | `PteState::Mapped(RcuDrop<C::Item>)` | **不构造 item**，直接写裸 PTE；refcount 手工维护 |
| `PageTableConfig::Item/ItemRef` | `VmItem` / `VmItemRef` | 完全不经过；因此不需要 `Clone` 约束（上一轮卡点） |
| 页表页读写 | `C::E`、`PteScalar` | 新增 `read_page/write_page`（`C::E` 数组 memcpy）与 leaf guard 获取 |

**安全性论证（关键）**：页表项的所有权语义是"PTE 持有一个 item"。批量路径
手工 `inc_frame_ref_count` 后写入 PTE，使得每个目的 PTE 也"持有一个引用"；
将来 unmap/fork 失败清理时，代码经 `PteState::from_pte` → `item_from_raw(paddr)`
→ `Frame::from_raw(paddr)` → drop 时 `Frame::drop` 减一（`frame/mod.rs:260`），
账目平衡。**前提：每次复制恰好加一次，且目的页表页不与任何人共享**（fork 每次
新建页表，满足）。

**unsafe 边界**：全部新代码放在 `ostd/src/mm/page_table/cursor/`（该模块本来
就是 unsafe 内部实现）；kernel 侧只调用一个**安全**方法
`CursorMut::copy_cow_page(&mut self, src, va) -> Option<usize>`，`aster-core`
的 `#![deny(unsafe_code)]` 不受影响。

## 4. 正确性风险清单与测试矩阵

| # | 风险 | 处理 |
|---|---|---|
| 1 | 稀疏 VMA / 空洞 | 空洞项原样复制（absent），`present` 计数只统计 mapped；不影响 |
| 2 | 部分映射块（mapping 未 2 MiB 对齐） | 首尾非对齐部分走现有慢路径；中间对齐块才用批量 |
| 3 | **huge page**（PMD 级映射） | 先检查该块 PMD 项：若为 mapped huge，整块回退慢路径（慢路径会 split） |
| 4 | MMIO（`AVAIL1`） | 块内发现即回退（`cow_copy_pt` 已有 `MappedIoMem` 分支，`fork.rs:298` 有 ktest） |
| 5 | COW 位 | 源、目的都清 `W`；源页写保护后沿用到 fork 末尾的 `TlbFlushOp::for_all`（`fork.rs:62`） |
| 6 | TLB 一致性 | 批量写完源页后仍需一次全局 flush（现有逻辑不变）；目的页表未激活，无需 flush |
| 7 | 页表页共享 | 本设计 fork 不共享页表页；若引入 vfork/CLONE_VM 共享必须禁用批量 |
| 8 | `nr_children` 计数 | 目的新页必须设置 `present`；否则页表页 drop 时断言或帧泄漏 |
| 9 | 引用计数泄漏/重复 | 每帧恰好一次；同一帧被映射两次（两个 VA）也各加一次，正确 |
| 10 | 并发修改 | cursor 已持有该 VA 范围的页表锁（2PL），批量不越出 locked range |

测试矩阵：

1. 现有 ktest：`cow_copy_pt_basic`（`fork.rs:172`）、`cow_copy_pt_iomem`（`fork.rs:298`）；
2. `fork_bench_check.sh` 的 `cow` 用例（父子同写 1 MiB + heap）；
3. 新增：稀疏 VMA（多个中间有空洞的 mmap）、混合映射（文件映射 + 匿名 + 栈/堆）、
   父子交替写；
4. 压力：fork 循环 10k 次后比较 `free` 内存（检测帧泄漏）+ 7 域矩阵；
5. 灰度高危项：`huge page` 路径（若当前内核不产生 huge 映射，则此项为空操作，
   但必须在代码里显式检查）。

## 5. 回退与灰度策略

- **按块灰度**：批量只处理满足 §2.1 的 2 MiB 块，块内任何异常立即返回 `None`，
  由 `cow_copy_pt` 的慢路径接手 —— 同一 mapping 内天然"混合路径"，风险局部化。
- **规模门槛**：只对 `map_size ≥ 64 MiB` 的 mapping 启用（小 mapping 用不上，
  且能避开大部分边界情况）。
- **开关**：`static BULK_COW: AtomicBool`（默认开，可运行时关）或编译期常量；
  出问题只需一个开关回退，无需回滚代码。
- **观测**：fork 微基准（512/256/64 MB）+ Firefox clone 的 cpu_ns；帧泄漏用
  `free` 前后差值与 `/proc/meminfo` 对比。

## 6. 预估收益

以 `c00db36af` 的 1.56 µs/页为基线，批量后每页成本 ≈：

| 成分 | 估计/页 | 依据 |
|---|---|---|
| 页表页 memcpy（读+写回源+写目的，3×4 KiB / 512 页） | 2–4 ns | 8–12 KiB 内存流量 / 512 |
| W 位处理（buf 内 512 项） | 1–2 ns | 每项一次位运算 |
| 每帧 `inc_frame_ref_count` | **30–80 ns**（差：200 ns） | 元数据缓存命中情况 |
| 每 512 页一次页表页分配/挂接 | 2–4 ns | 1 帧分配（~1–2 µs）/512 |
| 游标/循环/统计 | 5–10 ns | 每块一次 |
| **合计** | **~40–100 ns/页**（差 200–260 ns） | |

**PoC 实测（`wip/pte-bulk`，`/proc/bulk_probe`）**：对 512 个真实分配的帧、
20 轮 `dup_frame_ref` + drop（即一次 `inc_ref_count` 原子加 + 一次减），
三次读数 **52 / 53 / 51 ns**。因此每页的 refcount 成本取 **~52 ns**，
而不是保守估计的 150–250 ns。修正后的每页成本：

| 成分 | 实测/估计 |
|---|---|
| refcount bump | **52 ns**（实测） |
| 页表页 memcpy（3×4 KiB / 512 页） | 2–4 ns |
| W 位处理 | 1–2 ns |
| 每 512 页一次页表页分配/挂接 | 2–4 ns |
| 游标/循环/统计 | 5–10 ns |
| **合计** | **~62–76 ns/页** |

→ 512 MB（131072 页）≈ **8–10 ms**（相对 204.7 ms 是 **20–25×**）；
Firefox 的 clone（82.6 ms）≈ **1.7 ms 固定开销 + ~2.5 ms 页表复制 ≈ 4–5 ms**。

对应 512 MB（131072 页）：**5–13 ms**（差：26–34 ms），相对 204.7 ms 是
**8–40×**。Firefox 的 clone（82.6 ms，其中 ~1.7 ms 固定开销）预计降到
**~5–15 ms**。

## 7. 决策建议：~~建议做，但有前置条件~~ → **否决**

> 用户在 2026-10-26 划下红线：不得破坏 ostd 抽象。方案 §2/§3 需要绕开
> `Item`/`ItemRef`、直接写裸 PTE，正属于破坏抽象，因此**否决**；以下内容
> 仅作为记录保留。fork 的优化停在 `c00db36af`（512 MB = 204.7 ms）。

**建议做**，理由：收益 **20–25×**（PoC 实测 refcount 52 ns/页，合计
~62–76 ns/页 → 512 MB ≈ 8–10 ms），且改动可完全限制在 ostd 页表内部 +
fork 一个调用点，风险靠"按块回退 + 规模门槛 + 开关"可控。剩余工作量
约 2–3 天（实现 1 天 + 测试 1–2 天）。

PoC 已完成（本分支 `wip/pte-bulk`，未合并、未 push）：内核侧加了
`ostd::mm::frame::dup_frame_ref` 与 `/proc/bulk_probe`，读数 51–53 ns/次，
**消除了本设计唯一的不确定性**。仍需在正式实现前确认：

1. **huge page 行为**：当前内核匿名映射是否产生 PMD 级 huge 映射；若会，
   批量路径必须先处理或显式跳过（否则 split 语义被绕过）；
2. **帧引用计数平衡**：用 fork 循环 + `free` 前后对比做泄漏检查（§4 测试
   矩阵第 4 项），这是本方案最危险的失效模式（漏 inc 或重复 inc）。

补充判断（对"用户体验"的边际收益）：Firefox 的 `clone` 单次 82.6 ms，但只在
启动内容进程时发生（每次进程启动一次），不是滚动热路径；若目标是滚动流畅，
`read` 190 µs/次、`ioctl` 209 µs/次 的量级问题更值得先查。**因此建议把本
设计排在"clone 固定开销"与"read/ioctl 单次成本"之后再投入。**
