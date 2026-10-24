# L6 H1 验证：帧分配余量观测

## 结论摘要

- **观测点 2 处**：每 256 次用户任务创建打一条 `task spawn count=… free_frames_bytes=…`；
  `TaskOptions::build()` 失败时的 panic 消息内嵌同一个计数器（保留原 `NoMemory` 语义）。
- **帧余量序列**（27 个点，完整数据见
  [`evidence/2026-10-24-l6-h1/frame-series.tsv`](evidence/2026-10-24-l6-h1/frame-series.tsv)）：
  峰值 **3238.2 MiB**（t=44.2 s）→ panic 时 **1593.8 MiB**（t=3340.2 s），
  净降 **1644.4 MiB**，平均 **−0.50 MiB/s**；下降过程中还有回升，且**从未接近 0**。
- **判定：H1 不成立（作为 panic 的直接原因）；H2 成立**。
  panic 时分配器仍有 **1,671,266,304 B（1593.8 MiB）** 空闲，而失败的是内核栈的
  **128 页连续分配（order-7，512 KiB）**——空闲量约为请求量的 **3188 倍**。
  H1 预言的“4 GiB 帧被吃光、free≈0”没有出现。
- **H1 里“没有压力回收”的那一半是成立的**：55 分钟内空闲下降 1.6 GiB，
  没有任何回收把余量拉回；但这只是**压力来源**，不是本次 panic 的**触发机制**。
- **H2 的两个子机制仍未分离**：物理外部碎片（全系统找不到 128 个连续页）
  vs 跨 CPU 本地池滞留（高 order 块存在，但只躺在别的 CPU 的 local pool 里，
  当前 CPU 的分配路径不看）。需要 per-order / per-CPU 观测才能定论。
- **最需要的一处机制**：不是 page cache shrinker，而是**分配失败路径上的
  “跨 CPU 本地池归并 + 重试”**。理由见文末：本次失败发生在系统仍有 1.59 GiB
  空闲时，shrink 再多也解决不了“有内存但拿不到”；归并恰好对上这个状态。
  page cache shrinker 仍应做（约束 1.6 GiB 的增长），但排在归并之后。

## 观测点位置

| # | 位置 | 触发 | 输出 |
|---|---|---|---|
| 1 | `kernel/core/src/thread/task.rs:40-47` | 每 256 次 `create_new_user_task` | `WARN: task spawn count=<n> free_frames_bytes=<bytes>` |
| 2 | `kernel/core/src/thread/task.rs:151-162` | `TaskOptions::build()` 返回 `Err` | panic 消息 `spawn task failed: NoMemory free_frames_bytes=<bytes>` |
| — | 数据来源 | `osdk/deps/frame-allocator/src/lib.rs:53-56` 的 `load_total_free_size()` | 汇总所有 CPU 的 `TOTAL_FREE_SIZE` |

实现说明：

- 周期观测只加在任务创建路径（升级期间 spawn 的主要来源），不改变任何分配行为；
- panic 点把原来的 `.build().expect("spawn task failed")` 展开成 `match`，
  **失败仍然 panic、错误类型仍然是 `NoMemory`**，只是消息多了余量；
- 计数器的语义：`alloc` 成功时 `TOTAL_FREE_SIZE.sub(layout.size())`，
  `dealloc` 与 `add_free_memory` 时 `add(size)`（`osdk/deps/frame-allocator/src/lib.rs:65-86`）；
  `get()` 把各 CPU 计数器相加（代码注释说明“可能短暂不精确”）。
  **它是“分配器全局空闲”，包含各 CPU 的 local pool 和 1–4 页本地缓存，
  不等于“当前 CPU 能拿到的连续块”**——这正是后文区分 H2 子机制的关键限定。

## 帧余量序列

环境：4 vCPU / 4 GiB、KVM；guest 时间以串口 `[  t]` 为准。

| guest t (s) | spawn 计数 | free (MiB) | 区间变化 (MiB) |
|---:|---:|---:|---:|
| 41.896 | 256 | 3231.5 | — |
| 44.173 | 512 | 3238.2 | +6.7（峰值） |
| 45.879 | 768 | 3237.6 | −0.6 |
| 47.530 | 1024 | 3237.3 | −0.2 |
| 49.223 | 1280 | 3238.0 | +0.6 |
| 149.837 | 1536 | 3192.1 | −45.8 |
| 383.724 | 1792 | 3122.4 | −69.7 |
| 537.337 | 2048 | 3068.1 | −54.3 |
| 834.723 | 2304 | 2970.7 | −97.3 |
| 841.437 | 2560 | 2980.9 | **+10.1（回升）** |
| 844.639 | 2816 | 2977.0 | −3.9 |
| 847.654 | 3072 | 2977.1 | +0.1 |
| 850.588 | 3328 | 2977.1 | −0.0 |
| 853.487 | 3584 | 2977.1 | +0.0 |
| 957.451 | 3840 | 2969.7 | −7.4 |
| 1029.089 | 4096 | 2795.6 | −174.1 |
| 1134.163 | 4352 | 2742.6 | −53.0 |
| 1227.143 | 4608 | 2724.6 | −18.0 |
| 1373.040 | 4864 | 2720.2 | −4.5 |
| 1520.291 | 5120 | 2700.8 | −19.4 |
| 1751.526 | 5376 | 2650.2 | −50.6 |
| 1930.497 | 5632 | 2618.1 | −32.1 |
| 2100.824 | 5888 | 2600.9 | −17.2 |
| 2312.403 | 6144 | 2530.2 | −70.7 |
| 2501.992 | 6400 | 2487.0 | −43.2 |
| 2628.543 | 6656 | 1994.9 | **−492.1（最快区间，−3.9 MiB/s）** |
| 3219.143 | 6912 | 1603.0 | −391.9 |
| **3340.165（panic）** | — | **1593.8** | −9.2 |

几个至少要说的点：

1. 升级刚开始（update 完成、解包前）约 3.2 GiB 空闲；
2. 解包推进到中途（t≈2500 s）仍有 2.4 GiB；
3. 最后一小时下降加速，t=3340 s panic 时仍有 **1.59 GiB**。

## panic 现场

原始记录：[`evidence/2026-10-24-l6-h1/panic.txt`](evidence/2026-10-24-l6-h1/panic.txt)。

```
[ 3340.165] ERROR : Uncaught panic:
	spawn task failed: NoMemory free_frames_bytes=1671266304
	at /home/arch-anjie/Program/asterinas-riscv/kernel/core/src/thread/task.rs:156
	on CPU 0 by thread Some(Thread { ... cpu_affinity: AtomicIdSet { bits: [15] } ... })
Printing stack trace:
```

同一轮的升级进度（从副本的 `/var/tmp/upgrade/upgrade.log` 经 debugfs 读出，
见 [`evidence/2026-10-24-l6-h1/progress.txt`](evidence/2026-10-24-l6-h1/progress.txt)）：

| 项 | 值 |
|---|---|
| `Unpacking` | **303** 条 |
| `Setting up` | **78** 条 |
| 最后一条 | `Unpacking libsm6 …` |
| negative dentry 警告 | **74,998** 条，计数最高 **43,066** |
| `ext4 journal transaction is too large` | **0** 条 |
| 回包阶段 | `UPG stage=upgrade` 从未返回（panic 终止） |

与上一轮（`docs/porting/2026-10-24-l6-panic-attribution.md`，321 解包 / 78 配置 /
t=3427.5 s）相比：本轮 panic 早 **87 s**、解包少 **18 个包**。同一个失败点、
不同的到达时刻，说明它取决于分配器的**运行时状态**，不是一个固定的进度阈值。

## 判读：为什么 H1 的“耗尽”不成立

失败请求的尺寸是确定的：

- `KernelStack::new` 用 `DEFAULT_STACK_SIZE_IN_PAGES = 128`
  （`ostd/src/task/kernel_stack.rs:38`），即 **128 × 4 KiB = 512 KiB**；
- `alloc_segment_with` 把 `nframes × PAGE_SIZE` 交给帧分配器
  （`ostd/src/mm/frame/allocator.rs:79-96`）；
- 512 KiB 对应的 order 是 **7**（`greater_order_of`，`chunk.rs`）；
- `pools::alloc` 的处理顺序是：先当前 CPU 的 `LOCAL_POOL`（order < 18），
  再 `GLOBAL_POOL`；**不会看其它 CPU 的 local pool**；两边都没有 order-7 块
  就直接返回 `None`
  （`osdk/deps/frame-allocator/src/pools/mod.rs:54-75`）。

而 panic 时：

- `free_frames_bytes = 1,671,266,304` → **1593.8 MiB**；
- 1593.8 MiB ÷ 0.5 MiB ≈ **3188**。

所以 **不是“没有内存”**，而是 **“有内存，但当前分配路径在那一刻拿不到 128 个连续页”**。
H1 预言的“4 GiB 帧耗尽（free≈0）”被证伪；H2 描述的现象被直接观测到。

另外两点排除：

- **H3（泄漏）**：序列不是单调下降（t=841 s 回升 10 MiB；t=844–853 s 基本平台期），
  panic 时仍有 1.59 GiB；若是任务/内核栈泄漏，free 会持续单调逼近 0；
- **E2BIG 回潮**：同一轮 journal 事务错误 **0 条**，与 H1 无关。

## 判定

| 假设 | 判定 | 依据 |
|---|---|---|
| **H1：page cache 无压力回收 → 4 GiB 帧耗尽** | **不成立（作为直接原因）**；其中“无压力回收、余量持续下降”被证实 | panic 时 free=1593.8 MiB，是请求块（512 KiB）的 ~3188 倍；但 55 分钟净降 1.6 GiB 且无回收把余量拉回 |
| **H2：连续帧分配失败（碎片/可用性）** | **成立** | 失败的正是 order-7 的 128 页连续分配，而全局空闲充足 |
| H2a：物理外部碎片 vs H2b：跨 CPU 池滞留 | **证据不足** | `TOTAL_FREE_SIZE` 只给全局总量，没有 per-order / per-CPU 分解 |
| **H3：任务/内核栈泄漏** | 不支持 | 序列非单调、有回升、panic 时仍余 1.59 GiB |

## 最需要的机制

**第一优先：分配失败路径上的“跨 CPU 本地池归并 + 重试”。**
`pools/mod.rs:73-75` 已经写着这个 TODO：

```rust
// TODO: On memory pressure the global pool may be not enough. We may need
// to merge all buddy chunks from the local pools to the global pool and
// try again.
```

为什么先做它：

1. **它精确命中观测到的状态**：失败时系统有 1.59 GiB 空闲，只是不在
   “当前 CPU local pool + global pool 的 order-7 链表”上。把各 CPU 的 local
   pool 倒进 global set 时，`BuddySet::insert_chunk` 会与相邻 buddy 合并
   （`set.rs:30-60`），有机会当场拼出 order-7；这一步只发生在**分配失败之后**，
   对正常路径零影响，改动小、可回退。
2. **page cache shrinker 解决不了本次 panic**：panic 时本来就有 1.59 GiB 空闲；
   先做 shrinker 的话，同一个失败仍会发生。shrinker 的价值是**约束 1.6 GiB 的
   增长**（否则迟早变成真正的总量耗尽），属于第二优先。
3. **栈分配降级（128 → 16/32 页）是最省事的验证手段，但不应当作修复**：
   它把 order-7 降到 order-2/3，很可能直接绕开 panic，适合作为**证伪 H2 的
   对照实验**（`OSTD_TASK_STACK_SIZE_IN_PAGES=16` 重跑）；但 512 KiB 内核栈
   是现有设计的安全余量，砍掉只是掩盖分配器可用性问题。

顺序建议：

1. **诊断**（便宜）：在 panic 点打印每个 CPU 的 local pool 与 global pool 的
   per-order 空闲统计，区分 H2a（全系统无高 order 块）与 H2b（高 order 块在
   别的 CPU 池里）；
2. **修复**（小）：失败时归并全部 local pool → global → 重试一次；
3. **对照**：`OSTD_TASK_STACK_SIZE_IN_PAGES=16` 重跑，验证“连续分配”是唯一变量；
4. **长期**：page cache 压力回收/上限 + negative dentry 淘汰（43,066 条警告、
   7.5 万行日志本身就是噪音源）。

## 复现

```sh
make test_x86_debian_full_upgrade \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/systemd-health/daily.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm \
  FULL_UPGRADE_WORKSPACE=$PWD/target/debian-upgrade-h1
```

| 项 | 值 |
|---|---|
| 内核 | `target/osdk/asterinas-osdk-bin.elf`，sha256 `00ded8d5b733abaf…`（HEAD `831b7bd79` + 本轮观测改动） |
| initramfs | `target/debian-x86/stage1/initramfs.cpio`，sha256 `fb9c5b1765911cf0…` |
| 基础镜像 | `target/systemd-health/daily.ext2`，sha256 `450ebe3b…a083e0` |
| 副本 | `target/debian-upgrade-h1/daily.ext2`，sha256 与基础相同（`upgrade/copy.txt`） |
| 预算 | `UPG_UPGRADE_TIMEOUT=5400`（90 分钟）；panic 发生在 **3340 s**，远未到上限 |
| 宿主磁盘 | 8.0 GB 可用（高于 800 MiB 护栏） |

## 产物

- [`evidence/2026-10-24-l6-h1/frame-series.tsv`](evidence/2026-10-24-l6-h1/frame-series.tsv)：27 点完整序列；
- [`evidence/2026-10-24-l6-h1/panic.txt`](evidence/2026-10-24-l6-h1/panic.txt)：panic 前 25 行至 console 末尾；
- [`evidence/2026-10-24-l6-h1/progress.txt`](evidence/2026-10-24-l6-h1/progress.txt)：解包/配置计数与最后 12 条升级日志；
- [`evidence/2026-10-24-l6-h1/env.txt`](evidence/2026-10-24-l6-h1/env.txt)：内核/initramfs/镜像 sha、copy.txt、console 事实；
- `target/debian-upgrade-h1/console.txt`（10.2 MB，原始串口）。

## 限制

- 本轮 panic 后 QEMU 退出，**没有做二次启动**（上一轮同型 panic 已验证副本可启动，
  本轮未重复；H1 任务书也未要求）；
- 未做 8 GiB 内存或小内核栈的对照实验——那是判定 H2a/H2b 的下一步，不在“只加观测”范围内；
- `load_total_free_size()` 是跨 CPU 汇总的近似值（代码注释自述），但 1.59 GiB 与 512 KiB
  之间的数量级差距远大于任何计数误差，结论不受影响。
