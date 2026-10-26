# madvise(MADV_DONTNEED) 的 90 µs：三种情形 + 内核路径审计

日期：2026-10-26
内核：`e73d5b8e3`（未改内核代码）
探针：`/tmp/vb/madvise_probe.sh`（A1/A2 各 1000 次 create+join，A3 300 次 fork+wait4）

## 0. 结论

- **A1 8 MiB 栈：madvise 1000 次 / 90.7 µs 每次**——完整复现上一轮的 89.7 µs。
- **A2 128 KiB 栈：18.4 µs 每次（5 倍便宜）**；**A3 裸 fork+wait4：madvise 0 次**。
  ⇒ 成本确实是 glibc 归还线程栈时触发的，且**随范围增大而增大**；
  两点拟合给出「**约 18 µs 固定成本 + 约 70 ns/页**」的形状。
- 内核路径（`vm/vmar/vmar_impls/unmap.rs:68-118` → `VmSpace::unmap`）对**每个已映射页**发一条
  `TlbFlushOp::for_single(va)` 并挂 RCU drop，最后一个 mapping 结束时
  `dispatch_tlb_flush()` + **`sync_tlb_flush()`**（等 IPI 全部返回）。
  固定那 18 µs 与这次**同步 shootdown 的往返**吻合。

## 1. 任务 A：三个用例（原始行）

```
PROBE a1_out=MB a1 n=1000 total_ns=740764293 per_op_ns=740764
PROBE a1_madvise=count=1000 cpu_ns=90657965 avg=90657
PROBE a1_clone=435:count=1000 cpu_ns=305073279 avg=305073|56:count=2 ...
PROBE a2_out=MB a2 n=1000 total_ns=693224688 per_op_ns=693224
PROBE a2_madvise=count=1000 cpu_ns=18368693 avg=18368
PROBE a2_clone=435:count=1000 cpu_ns=285648033 avg=285648|...
PROBE a3_out=MB a3 n=300 total_ns=953671345 per_op_ns=3178904
PROBE a3_madvise=                      ← 0 次
PROBE a3_clone=56:count=302 cpu_ns=412809947 avg=1366920
```

| 用例 | 栈/映射大小 | madvise 次数 | **单次** | clone 单次 | 每次迭代总耗时 |
|---|---|---|---|---|---|
| A1 pthread，默认栈 | 8 MiB | 1000 | **90.7 µs** | 305.1 µs | 740.8 µs |
| A2 pthread，128 KiB 栈 | 128 KiB | 1000 | **18.4 µs** | 285.6 µs | 693.2 µs |
| A3 fork + wait4 | — | **0** | — | 1 366.9 µs（fork） | 3 178.9 µs |

**判据回答**：成本**不完全 ∝ 栈大小**——范围缩小 64 倍，成本只降 4.9 倍。
拟合：固定 ≈ 18 µs（A2 几乎全是这一项），其余 ≈ 72 µs 随 8 MiB 范围内**已映射页数**增长
（≈ 70 ns/页；本轮没有单独统计已映射页数，标为估算）。

## 2. 任务 B：内核路径审计（文件:行号）

| 位置 | 行为 |
|---|---|
| `kernel/core/src/syscall/madvise.rs:8-49` | `MADV_DONTNEED` → `vmar.discard_pages(addr_range)` |
| `kernel/core/src/vm/vmar/vmar_impls/unmap.rs:68-118` | 对每个相交 VMA：`cursor.unmap(len)` → `dispatch_tlb_flush()` → **`sync_tlb_flush()`** |
| `ostd/src/mm/vm_space.rs:472-530` | `unmap` 用 `take_next` **逐个 fragment（页）** 取下；每个已映射页 `issue_tlb_flush_with(TlbFlushOp::for_single(va), RcuDrop)` |
| `ostd/src/mm/tlb.rs:82-120` | `dispatch_tlb_flush`：把 ops 推给目标 CPU 并**发 IPI**，本 CPU 自己 flush |
| `ostd/src/mm/tlb.rs:134-147` | `sync_tlb_flush`：`pending_ipis.wait()` —— **同步等待所有 CPU 应答** |
| `ostd/src/mm/tlb.rs:199-215` | 存在 `TlbFlushOp::for_range`，但 `unmap` 目前**逐页**用 `for_single` |

与 Linux 的差别：Linux 在 `zap_page_range` 里用 `tlb_gather_mmu` 收集，最后
`tlb_finish_mmu` → `flush_tlb_mm_range` **一次范围 flush**；我们已经是"每个 VMA 一次 IPI"，
但 flush 内容仍是**每页一条 `for_single`**，并且 `sync_tlb_flush` 是同步等待。

**任务 B 的优化没有落地**：余下时间不够在改内核后完成「编译 + pthread 基准 + 7 域矩阵」的
完整验收，按纪律「没数据不许改代码 / 一次一变量」，本轮只交审计与数据。下一步按 ROI：

1. **把逐页 `for_single` 合成一条 `for_range`**（`tlb.rs:212` 已有 API，`vm_space.rs:498` 逐页发）——
   这是最集中的一处，预期砍掉随页数增长的那部分的大部分；
2. **评估 `sync_tlb_flush` 能否延后**（Linux 用 `tlb_finish_mmu` 的延迟 flush）——固定 18 µs 的大头；
3. 若两者都不够，再看 glibc 侧：A2（128 KiB 栈）已经只有 18.4 µs，说明**栈越大这项越贵**，
   与上一轮"内核栈不能缩"无关（这是用户栈）。

## 3. 任务 C：未做

reap 段仍是 220 µs（>100 µs 门槛），但时间用尽。线索已在
`2026-10-27-join-and-enqueue.md` §4 列出（全局 `PID_TABLE` 锁 `pid_table.rs:19`、
`exit.rs:57-83` 的 O(n) 扫描、`exit.rs:145-156` 的 CLEARTID 唤醒）。

## 4. 证据

- `evidence/2026-10-27-madvise-and-pidlock/MD1-madvise-probe-lines.txt`（25 行）
- 控制台 `~/vb-ws/MD1/console.txt`
- 探针 `/tmp/vb/madvise_probe.sh`（含 `mb.c`：a1/a2/a3 三种模式）
