# 「跳过空区间」这条路被数据否掉了：0 页 madvise 只走 0.26 个 fragment

日期：2026-10-26
内核基线：`3bcaaf50e`
本轮：只做测量，**没有内核代码入库**（插桩已全部 revert，默认内核 20:44 重编译）

## 0. 结论（与任务书假设相反）

任务书假设 78.6 µs（8 MiB、0 页映射）是「逐 4 KiB 遍历空区间」走出来的。**实测否掉了这个假设**：

- 8 MiB、**0 页映射**的 `madvise`：200 次调用期间 unmap 循环总共只走了 **131 次**迭代
  （同窗口 510 次 unmap 调用，含 `munmap`），即 **≈0.26 个 fragment / 调用**。
- 对照：8 MiB、2048 页映射时，约 **515 fragment / 调用**（103 100 迭代 / ~200 次 madvise）。

也就是说 **78.6 µs 里几乎不包含遍历**。`find_next_impl` 的「空子树跳过」
（`cursor/mod.rs:213-231`：`nr_children()==0` 时直接 `move_forward`）本来就在工作。

⇒ 按任务 A 的判据（0 页 78.6 → <10 µs）改遍历**不可能达成**，本轮没有做这个改动。

## 1. 测量与证据

### 1.1 遍历次数（US1，插桩版内核）

```
before_t0=calls=17466 iters=23602 pages=510842
after_t0 =calls=17976 iters=23733 pages=514081      ← t0：0 页，200 次 mmap+madvise+munmap
before_t2048=calls=18058 iters=23832 pages=516469
after_t2048 =calls=18542 iters=126932 pages=928348  ← t2048：2048 页
```

| 场景 | 窗口内 unmap 调用 | 窗口内循环迭代 | 迭代/调用 |
|---|---|---|---|
| 8 MiB，0 页 | 510 | 131 | **0.26** |
| 8 MiB，2048 页 | 484 | 103 100 | ≈213（含 madvise 与 munmap 均摊） |

### 1.2 时间形状（SH1，基线内核，上一轮已归档）

| 8 MiB 范围内映射页数 | madvise 单次 |
|---|---|
| 0 页 | 78.6 µs |
| 1 页 | 96.8 µs |
| 512 页 | 183.8 µs |
| 2048 页 | 692.1 µs |

两条合起来：**0 页仍要 78.6 µs，但循环几乎不迭代** ⇒ 成本在「建立/释放这条范围访问」的
准备工作上，而不是在遍历里。

### 1.3 准备好的阶段计时**没装上**（如实记录）

我随后给 `VmSpace::unmap` 加了 `rdtsc` 阶段计时（loop / dispatch）并暴露到 `/proc/unmap_stats`，
但 US2 读出 `cyc_loop=0 cyc_flush=0 cyc_calls=0`——原因是那次文本替换**没有匹配上**（Python
`replace` 未断言，静默失败），只有计数器部分生效。**所以 78.6 µs 的阶段拆分本轮没有数据**，
不编造。

## 2. 任务 B：下一步该查什么（有代码依据，未验证）

按代码阅读，成本最可能落在 **cursor 建立**这一步：

- `discard_pages`（`vm/vmar/vmar_impls/unmap.rs:100-112`）对每个相交 VMA 调
  `vm_space.cursor_mut(&preempt_guard, &intersected_range)`；
- `CursorMut::new` → `locking::lock_range`（`ostd/src/mm/page_table/cursor/locking.rs:21-51`）：
  对**整个范围**做 `dfs_acquire_lock`，并且注释明确写着「如果节点（或其祖先）不存在，
  需要锁住父节点并**创建它**」。

这解释了「范围越大越贵、与映射页数无关」：8 MiB 范围会沿页表层级铺开并锁住/创建覆盖该范围的节点。
**下一轮的第一件事**是把 §1.3 的计时真正装上（用 `assert` 保证替换命中），确认
`lock_range` 占 78.6 µs 的比例；若确认，再考虑「先探测 VMA 内是否有驻留页，没有就直接返回」
（这正是任务 B 给的第二条方向：先查 VMA 内实际驻留范围）。

## 3. 本轮改动状态

- US1/US2 的计数器与 `/proc/unmap_stats` 插桩**已全部 revert**（
  `ostd/src/mm/vm_space.rs`、`kernel/core/src/fs/fs_impls/procfs/{mod.rs,unmap_stats.rs}`）。
- 默认内核已重编译（`22945600` 字节，20:44），与 `3bcaaf50e` 完全一致，因此未重跑 7 域矩阵。

## 4. 证据

- `evidence/2026-10-27-skip-empty/US1-unmap-iterations.txt`（8 行，遍历计数）
- `…/US2-phase-timers-failed.txt`（9 行，计时未生效的原始读数）
- `…/SH1-shape.txt`（24 行，成本形状，来自上一轮同内核）
- 控制台 `~/vb-ws/{US1,US2,SH1}/console.txt`
- 探针 `/tmp/vb/unmapstats_probe.sh`、`/tmp/vb/madvise_shape_probe.sh`
