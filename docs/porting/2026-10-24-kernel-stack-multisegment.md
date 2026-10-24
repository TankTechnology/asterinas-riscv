# 内核栈多段映射（order 7 → order 4）

日期：2026-10-24　基线：`d9550a0a6`（本改动之前）

## 结论摘要

- 内核栈从**单次 128 页连续分配（order 7）**改为 **8 段 × 16 页（order 4）**，
  虚拟地址仍然连续；**栈大小（128 页 = 512 KiB）与 guard page 语义不变**。
- **原来的 panic 消失**：上一轮在 guest **3340.2 s / 1,671,266,304 B 空闲**时
  `spawn task failed: NoMemory`；本轮在几乎相同的状态（**3335.9 s /
  1,670,078,464 B / 7168 次 spawn**）没有任何 panic，并继续跑到 **3531 s**。
- **升级进度更高**：333/341 解包、78 配置（上一轮 panic 时 303 或 321）。
- 但升级仍未完成：t=3531.5 s 出现**新的、与内核栈无关的失败**——4 KiB
  （order 0）分配失败，随后页错误返回 `ENOMEM`、堆分配器报
  `Failed to allocate a large slot` / `Heap allocation error`，控制台终止。
  该失败的根因与修复候选见
  [`2026-10-24-frame-allocator-starvation.md`](2026-10-24-frame-allocator-starvation.md)。
- 回滚路径：分配失败时由 **RAII** 释放已分配的段（`segments` 数组在提前
  返回时析构）；没有做故障注入测试（分配器无故障注入接口），回滚路径由
  代码结构保证，正常路径由 QEMU 实测。

## 改动

`ostd/src/task/kernel_stack.rs`：

| 项 | 改动前 | 改动后 |
|---|---|---|
| 物理分配 | 1 × `alloc_segment_with(128)` | **8 × `alloc_segment_with(16)`** |
| 连续块要求 | order 7（512 KiB） | **order 4（64 KiB）** |
| 虚拟布局 | 132 页区、offset 2 页、映射 128 页 | **不变** |
| guard page | 下 2 页 + 上 2 页不映射 | **不变** |
| 栈大小 | 128 页 | **不变** |
| `end_vaddr` | `range.start + 2 页 + 128 页` | **不变** |

实现要点（`kernel_stack.rs:76-122`）：

- `segments: [Option<Segment<KernelStackMeta>>; 8]`，循环按
  `min(剩余页, 16)` 分配；最后一段允许小于 16 页（兼容
  `OSTD_TASK_STACK_SIZE_IN_PAGES` 不是 16 的倍数）；
- 任何一段失败时 `?` 提前返回，**数组析构会释放此前所有段**——注释里明确
  写了“不要改成手写清理路径”；
- 全部成功后，把各段 `into_iter()` 展平成帧迭代器交给
  `KVirtArea::map_frames`，按页映射到同一段连续虚拟区；
- 物理地址不再连续，但全仓没有依赖栈物理连续的代码（`KernelStack` 只保存
  `KVirtArea` 与 `end_vaddr`；上下文切换只用虚拟地址）。

### 失败回滚

没有可用的故障注入接口来模拟“第 5 次分配失败”（`FrameAllocator` 是全局
单例，没有测试钩子）。回滚由 Rust 所有权保证：

```
segments: [None; 8]
  ├─ 第 1..4 段 Some(...)
  ├─ 第 5 段 alloc_segment_with(...) 返回 Err
  └─ `?` 提前返回 → segments 数组 Drop → 4 个 Segment Drop → 帧归还
```

正常路径（8 段全部分配并映射）已在 QEMU 里跑通：LTP 40 个用例与两次
L6 启动都经过内核栈创建。

## 验证

| 项 | 命令 | 结果 |
|---|---|---|
| 内核构建 | `cargo osdk build --target-arch x86_64 --scheme microvm` | 成功；ELF sha256 `5644aff792f48c0dc3eb9ef2ac5bd8cb…`，`e_machine=EM_386`，22.83 MB |
| 内核类型检查 | `cargo check -p ostd` / `-p aster-core`（x86_64-unknown-none） | 通过 |
| LTP 基线 | `make test_x86_debian_ltp LTP_PHASE=run` | **40 case：34 pass / 6 fail / 0 skip**，失败集合与基线完全相同（pipe06、pipe07、open02、open04、mmap04、mmap08） |
| L6 | `make test_x86_debian_full_upgrade`（90 分钟预算） | 不再 panic，越过原失败点；在 3531 s 遇到新的 4 KiB 分配失败 |

### L6 结果细节

环境：4 vCPU / 4 GiB、KVM；基础镜像 sha256 `450ebe3b…a083e0`，副本 sha 相同
（`upgrade/copy.txt`）；`apt-get update` 297 个可升级包。

帧余量序列（29 点，完整数据：
[`evidence/2026-10-24-kstack-multisegment/frame-series.tsv`](evidence/2026-10-24-kstack-multisegment/frame-series.tsv)）：

| guest t (s) | spawn | free (MiB) |
|---:|---:|---:|
| 40.6 | 256 | 3229.3 |
| 498.5 | 2048 | 3069.5 |
| 1010.6 | 4096 | 2796.8 |
| 1684.7 | 5376 | 2668.2 |
| 2615.2 | 6656 | 1992.0 |
| 3208.6 | 6912 | 1600.0 |
| **3335.9** | **7168** | **1592.7**（上一轮在此处 panic） |
| 3471.3 | 7424 | 1589.7 |

关键对比：

| | 上一轮（单段 128 页） | 本轮（8 × 16 页） |
|---|---|---|
| panic | **有**，t=3340.165 s / free 1,671,266,304 B | **无**（改在更晚的 4 KiB 分配上失败） |
| 越过 3335 s | — | 7168 spawn，继续运行 |
| 解包 / 配置 | 303 / 78（或 321 / 78） | **333 / 78** |
| 结束方式 | `spawn task failed: NoMemory` | 4 KiB 分配失败 → 页错误 ENOMEM → 堆错误，控制台终止 |

结论：**order-7 的失败点已被消除**；升级能多推进约 30 个包。

## 剩余碎片风险

新的失败不再是内核栈：`size=4096 align=4096`，失败瞬间：

```
free_local=200302592  chunks_local=[40058, 3108, 657, 0, 0, 0, ...]
free_global=890032128 chunks_global=[3, 49429, 29608, 0, 0, 0, ...]
```

即 order 0/1/2 有大量空闲块，但 **order ≥3 为空**。这不是“没有内存”，也
不是“没有单页”，而是分配路径的**回填请求要求 8 个连续页**（见下一份文档）。
因此：

- **不需要更小的栈段**：栈已经降到 order 4，而且本轮的失败发生在 4 KiB；
- 下一步应修分配器的回填/降级路径（或引入回收），不是继续拆栈；
- 若只做收尾验证，4 KiB 失败一旦消失，升级仍可能因 page cache 无回收
  在更高阶上再次失败，属于同一族问题的后续观测。

## 产物

- [`evidence/2026-10-24-kstack-multisegment/frame-series.tsv`](evidence/2026-10-24-kstack-multisegment/frame-series.tsv)：29 点帧余量；
- [`evidence/2026-10-24-kstack-multisegment/failure.txt`](evidence/2026-10-24-kstack-multisegment/failure.txt)：首个分配失败起的完整控制台尾部；
- [`evidence/2026-10-24-kstack-multisegment/progress.txt`](evidence/2026-10-24-kstack-multisegment/progress.txt)：333/78 进度与失败计数；
- [`evidence/2026-10-24-kstack-multisegment/env.txt`](evidence/2026-10-24-kstack-multisegment/env.txt)：内核/initramfs/镜像 sha；
- `target/debian-upgrade-kstack/console.txt`（10.4 MB，原始串口）。

## 限制

- L6 没有跑完，也没有做二次启动检查；
- 回滚路径没有故障注入测试，只有所有权结构保证 + 正常路径实测；
- 本轮只改了内核栈分配，没有动分配器（分配器问题另见下一份文档）。
