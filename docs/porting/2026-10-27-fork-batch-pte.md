# fork 页表批量：A 现状、B 已落地的一步与卡点、C 数字

日期：2026-10-27
基线：`e783db15f`（fork 游标优化，512 MB = 226.7 ms）
本轮：见 §3

## 1. 任务 A：CursorMut 的批量能力（结论：没有）

| 问题 | 答案 |
|---|---|
| 有 `as_slice` / 迭代器 / 批量 map？ | **没有**。公开 API 只有 `find_next` / `query` / `jump` / `map` / `map_iomem` / `unmap` / `protect_next` / `protect_cur`（上一轮新增）/ `flusher` |
| 一次 `map()` 最少做什么 | 层级调整（必要时 `alloc_if_none` 分配页表页，每 512 页一次）→ `PteState` 转换、维护 `nr_children`、`write_pte` → `move_forward`。**没有每页 TLB 操作**（fork 结束统一 flush） |
| 目的端连续 PTE 是否在同一页表页 | 是。2 MiB 内 512 项同属一个 PTE 页；源端同理，物理上可以整体批量写 |
| 能不能"克隆一个已有映射"用于 COW | **不能**：`PageTableConfig::Item` 只要求 `Send`，没有 `Clone`/`from_ref`；但 COW 需要给子进程复制一份带引用计数的 item |

### 1.1 每页成本实测（rdtsc 插桩，512 MB 进程 fork）

| 阶段 | ns/页 |
|---|---|
| `src.find_next` | 433 |
| `src.query` | 290 |
| `src.protect_cur`（含 `UFrame::clone`） | 304 |
| `dst.jump + dst.map` | 510 |
| 循环/RSS/其它 | ≈360 |
| **合计** | **≈1.9 µs/页**（226.7 ms / 131072 页 = 1.73 µs，实测含插桩开销略高） |

结论：**成本均匀分布在四次游标调用上**，没有单点；要 2× 必须把"找页 + 读属性 + 保护"和"写目的 PTE"都批量化。

## 2. 任务 B：落地与卡点

### 2.1 已落地（B1 的简化版）：连续页不重新搜索

旧循环每页执行 `find_next(remain_size)`（433 ns）——但上一轮的 `protect_cur`
已经把游标推进到下一页，连续映射时下一页就在游标下。新循环先试
`src.query()`，只有遇到空洞（返回 `None`）才退回 `find_next`。

语义不变：空洞、巨页、MMIO 都仍走原路径。

### 2.2 卡点：真正的"512 项批量"需要先扩展 ostd

一次 512 项需要：

1. **页内条目迭代**：`CursorMut` 要能拿到当前 PTE 页的 `PageTableGuard` 与
   起始索引（内部有 `read_pte`/`write_pte`/`entry(idx)`，但不对游标使用者暴露）；
2. **从 `ItemRef` 生成 owned `Item`**：COW 给子进程复制映射需要
   `item.clone()`（引用计数 +1），而 `PageTableConfig::Item` 没有 `Clone`
   约束，也没有 `from_ref` 钩子；
3. 源端整页写保护（B2）同样依赖 1 才能避免逐项 `cur_entry()`。

改动面：`PageTableConfig` 增加 `fn dup_item_from_ref(&Self::ItemRef) -> Self::Item`、
`CursorMut::cow_copy_run(src, dst, max)`（两侧在同一页表页内批量读写、遇空洞/
巨页停止并回退），以及 fork 路径按页表页调用。这是三个文件、带所有权的
unsafe 改动；本轮剩余时间不足以安全实现并验证（错误的引用计数直接导致 COW
数据损坏），因此**没有强行落地**。

下一步方案（已成型，约 0.5 µs/页 → 512 MB ≈ 65 ms）：
`cow_copy_run` 内对当前 PTE 页做一次 `read_pte` 扫描，批量改写源 PTE 的 W 位、
批量写入目的 PTE，仅对空洞/巨页回退到现有慢路径。

## 3. 任务 C：数字

### 3.1 fork 微基准（本轮的连续页快路径）

| 指标 | 上一轮 `e783db15f` | 本轮（连续页快路径） | 变化 |
|---|---|---|---|
| 512 MB fork | 226.7 ms | **204.7 ms** | **−9.7%** |
| 256 MB fork | 114.0 ms | 101.6 ms | −10.9% |
| 64 MB fork | 30.6 ms | 28.9 ms | −5.6% |
| 每页（512 MB） | 1.73 µs | **1.56 µs** | −9.7% |
| 小进程 fork（×10k） | 1.52 ms | 1.58 ms | +4%（在 1.51–1.71 的run间噪声内） |
| reserve 512 MB | 1.65 ms | 1.62 ms | 不变 |

**目标（<113 ms）未达成**：204.7 ms，只走完了"省掉每页一次 find"这一步
（占每页成本约 433 ns / 1.9 µs）。再要 2× 必须做 §2.2 的页表页批量，
属于跨 ostd 三个文件的所有权改动，本轮未做。两轮累计：272.8 → 204.7 ms
（**−25%**）。

### 3.2 COW 正确性（新增 `cow` 用例）

父进程写满 1 MiB 匿名页 → fork → 子进程校验后整页改写 → 子进程退出；
父进程校验自己的数据未被污染、再改写并校验；另测 heap（sbrk）页。
结果：**`child_ok=1 parent_ok=1 heap_ok=1`**（1 MiB 匿名页 + 64 KiB heap
页，父子互不可见、各自写入后数据完整）。

### 3.3 7 域矩阵

内核改变（fork 快路径），矩阵重跑（工具链镜像 `toolchain4`）：

| 域 | 结果 | 细节 |
|---|---|---|
| DESKTOP | ok | Xorg+DRI3 |
| APP | ok | xterm |
| APT | ok | xclip+xterm+alsa-utils-installed |
| CLIPBOARD | ok | xclip-round-trip |
| AUDIO | ok | aplay-hw:0,0 |
| USB | ok | mounted-/dev/sda-read-ASTERINAS-USB-OK |
| SUSPEND | ok | woke-up-disk-and-io-ok |

INPUT_METHOD 仍 fail（既有缺口）；`DAILY_DONE=ok`、panic 0、allocation failed 0。
