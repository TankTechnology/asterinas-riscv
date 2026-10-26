# madvise 六段计时：P3（unmap 循环 / find_next_impl）占 ~80 %

日期：2026-10-26
内核基线：`a6f3f474e`
本轮：只做测量 + 计时自证；**改动已全部 revert**，内核 21:01 重编译为基线字节

## 0. 结论

1. 六段计时这次**确实装上了**（P1..P6 全部非零），自检通过：
   合计 **≈63 µs vs 整调用 span 73.4 µs ≈ 86 %**（要求 ±20 % 内）。
2. **最大段是 P3：unmap 循环（含 `find_next_impl` 内部扫描）≈ 58.9 µs，占 ~80 %**。
3. 这**修正了上一轮（`a6f3f474e`）的判断**：我当时用自己加的 `take_next` 计数器得出
   「0 页只走 0.26 个 fragment ⇒ 遍历不是成本」。真相是：`take_next` 返回 `None` 之前，
   `find_next_impl` 已经在**函数内部**逐项走完了整个范围，而那段开销不在我的计数器里。
   ⇒ 上一轮「跳过空区间」的方向其实是对的，只是我的度量放错了位置。

## 1. 计时数据（PH2，原始 cycles）

窗口 before_t0 → after_t0 = 200 次 `madvise(8 MiB, MADV_DONTNEED)`（0 页映射），
同一窗口里还有 200 次 `munmap`（所以 ostd 计数器要按 unmap 次数摊）。
TSC 频率由 span 反推：160 566 cyc/call ÷ 73 358 ns ≈ **2.189 GHz**。

| 段 | 原始 cycles（Δ） | 每 madvise | 占 span |
|---|---|---|---|
| P1 syscall 入口 → discard | 35 107 | **0.08 µs** | 0.1 % |
| P2 `cursor_mut`/`lock_range` | 1 005 855 | **2.30 µs** | 3.1 % |
| **P3 unmap 循环（含 find_next_impl）** | 64 306 108 / 499 unmap | **≈58.9 µs** | **≈80 %** |
| P4 `dispatch_tlb_flush` | 1 568 232 / 499 unmap | **1.44 µs** | 2.0 % |
| P5 `sync_tlb_flush` | （P4+P5 = 3 530 cyc/call） | **≈0.2–1.5 µs** | ≤2 % |
| P6 其余（返回等） | 由 span 差得 | **0.32 µs** | 0.4 % |
| 合计 | | **≈63 µs** | **86 %**（自检通过） |
| 整调用 span | 32 113 264 | **73.4 µs** | 100 % |

（span 73.4 µs 与 `/proc/syscall_time` 独立测得的 78.6 µs 一致，两种方法互证。）

## 2. 自证（任务 A 的硬要求）

- P1..P6 **每项 > 0** ✓（PH2 行里 `p1=80 p2=2297 p3=13117 p4=7448 p5=1530 p6=316`，
  这是**按错误分母**算出来的 ns 平均值，已作废；正确值见上表，由原始 cycles 重算）
- 六段合计 / span = 63 / 73.4 = **86 %**，在 ±20 % 内 ✓
- 期间 `Uncaught panic` = 0

**上一版（PH1）为什么对不上**：P3/P4 是 ostd 计数器，覆盖**所有** unmap（含 `munmap`），
我却按 madvise 次数去除，导致偏小约 2.5 倍；PH2 改为输出原始 cycles 后按同一窗口折算才自洽。

## 3. 任务 B：为什么没有动手

任务书给的两条分支是「`lock_range` 最大」或「TLB 最大」，而实测最大的是 **P3（循环/遍历）**，
两者都不适用；而且按验收要求（<40 µs 或 create+join <500 µs）需要改 `find_next_impl` 的扫描策略，
再走一遍「编译 + 基准 + 7 域矩阵」，剩余时间不够安全完成，按纪律本轮不改代码。

**下一轮的落点（已明确）**：
1. 在 `find_next_impl`（`ostd/src/mm/page_table/cursor/mod.rs:189-259`）内部加计时，
   区分「下降/上浮层级」与「逐 PTE 扫描」；
2. 若确认是逐 PTE 扫描：当父节点 `nr_children()` 为 0 时的跳过逻辑（`:225-231`）对
   「节点存在但子项全空」的情形是否失效——这正是 0 页 + 8 MiB 的形态；
3. 修完后用同一份 PH 探针复测，要求 P3 显著下降且总 span <40 µs。

## 4. 改动状态

所有计时插桩已 `git checkout` 还原（含临时 `/proc/madv_phases`、`unmap.rs`/`vm_space.rs`/
`madvise.rs` 的计时点、以及为暴露模块而改的三处 `pub(crate) mod`），默认内核重新编译
（`22945600` 字节，21:01），与 `a6f3f474e` 完全一致，故未重跑 7 域矩阵。

## 5. 证据

- `evidence/2026-10-27-phase-timers/PH1-averaged-probe-lines.txt`（6 行，分母错误的版本）
- `…/PH2-raw-cycles-probe-lines.txt`（6 行，原始 cycles，本文数据来源）
- 控制台 `~/vb-ws/{PH1,PH2}/console.txt`
- 探针 `/tmp/vb/phases_probe.sh`
