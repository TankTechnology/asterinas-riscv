# 内核栈碎片问题：先证实、再给方案

日期：2026-10-24　基线：`187217c4a`（H1 观测轮）

## 结论摘要

- **panic 路径确认就是内核栈**，不是别的 128 页分配：
  `TaskOptions::build()` 里唯一的 `?` 就是 `KernelStack::new_with_guard_page()?`
  （`ostd/src/task/mod.rs:213`），错误类型 `NoMemory` 由帧分配器返回。
- **精确的物理要求是 128 个连续页（order 7，512 KiB），对齐 4 KiB**：
  `KERNEL_STACK_SIZE = 128 × 4 KiB`（`ostd/src/task/kernel_stack.rs:38-40`），
  经 `alloc_segment_with(128, …)` 变成一个 `Layout { size: 524288, align: 4096 }`
  （`kernel_stack.rs:64-66` → `ostd/src/mm/frame/allocator.rs:79-96`）。
  **guard page 是纯虚拟的**：`map_frames(128+4 页, offset=2 页, 128 帧)`
  （`kernel_stack.rs:72-77`），下 2 页、上 2 页不建立映射，不占物理页。
- **buddy 的最大阶不是瓶颈**：全局 `MAX_BUDDY_ORDER = 32`、每 CPU 本地池
  `MAX_LOCAL_BUDDY_ORDER = 18`（`osdk/deps/frame-allocator/src/pools/mod.rs:40,52`），
  order 7 远低于上限。失败只可能是**两个查找位置（当前 CPU 本地池 + 全局池）
  都没有 order-7 块**。
- 失败时原本**没有任何各阶统计**。本提交只加了一条**只读诊断日志**：
  分配失败时打印 layout 加上当前 CPU 本地池/全局池的**每阶空闲块数**
  （不改分配/回收逻辑）。
- **推荐改法 A：多段映射的内核栈**（每段 16 页 / order 4，8 段拼成 128 页）。
  理由：`KVirtArea::map_frames` 本来就接受任意 `Frame` 迭代器并按页映射
  （`ostd/src/mm/kspace/kvirt_area.rs:124-148`），**不需要新增 VM 基础设施**；
  内核栈只经虚拟地址访问，没有任何代码依赖物理连续。

## 1. 链路证实

调用链（每层都标了文件/行号）：

```
kernel/core/src/thread/task.rs:151-162   TaskOptions::build() 返回 Err → panic
  └─ ostd/src/task/mod.rs:177,213        let kstack = KernelStack::new_with_guard_page()?;
       └─ ostd/src/task/kernel_stack.rs:63-66
            let pages = FrameAllocOptions::new().zeroed(false)
                .alloc_segment_with(KERNEL_STACK_SIZE / PAGE_SIZE, |_| KernelStackMeta)?;
              └─ ostd/src/mm/frame/allocator.rs:79-96
                   let layout = Layout::from_size_align(nframes * PAGE_SIZE, PAGE_SIZE)
                   get_global_frame_allocator().alloc(layout).ok_or(Error::NoMemory)?
                        └─ osdk/deps/frame-allocator/src/lib.rs  FrameAllocator::alloc
                             → cache::alloc → pools::alloc(order 7)
```

各层的实际数值：

| 项 | 值 | 出处 |
|---|---|---|
| 栈页数 | 128 | `DEFAULT_STACK_SIZE_IN_PAGES = 128`（`kernel_stack.rs:38`） |
| 栈字节数 | 524,288 B（512 KiB） | `KERNEL_STACK_SIZE`（`kernel_stack.rs:40`） |
| `alloc_segment_with` 参数 | `nframes = 128` | `kernel_stack.rs:66` |
| `Layout` | size = 524,288，align = 4,096 | `allocator.rs:90` |
| size order | 128 页 → **order 7** | `chunk.rs:21-25` `greater_order_of` |
| align order | 4 KiB → order 0 | 同上 |
| 最终 order | **7**（`max(7, 0)`） | `pools/mod.rs:59-61` |
| 虚拟区 | 132 页（128 页栈 + 下 2 页 + 上 2 页 guard） | `kernel_stack.rs:72-77` |

两个容易搞错的点：

1. **guard page 不参与物理分配**。`map_frames(area_size = 132 页,
   map_offset = 2 页, frames = 128 帧)` 只把 128 个帧映射到 offset 之后的
   连续虚拟页；guard 页只是 KVirtArea 里没有映射的虚拟页，访问即缺页。
   所以物理请求是 **128 页**，不是 129 或 132 页。
2. `alloc_segment_with` 只接受**一个**连续物理段（它把
   `start..start + nframes*PAGE_SIZE` 包成一个 `Segment`），所以 128 页必须是
   buddy 眼里的单块（order 7），无法在 `allocator.rs` 内部自动拆段。

`build()` 里没有别的 `?`：`data`/`local_data` 的 `Box::new` 失败会走
`handle_alloc_error`（“memory allocation failed”），不是 `NoMemory`。
**因此 panic 归因不需要纠正，就是内核栈。**

## 2. buddy 现状与失败诊断

| 项 | 值 | 出处 |
|---|---|---|
| 全局最大阶 | **32**（4 KiB × 2^31 = 8 TiB） | `pools/mod.rs:40` |
| 本地池最大阶 | **18**（512 MiB） | `pools/mod.rs:52` |
| order-7 分配路径 | 当前 CPU 本地池（order < 18）→ 全局池 → `None` | `pools/mod.rs:64-72` |
| 失败时的跨 CPU 归并 | **没有**（代码里是 TODO） | `pools/mod.rs:73-75` |
| 失败时各阶统计 | **原先没有** | —— |

新增的诊断（只读，不改行为）：

| 位置 | 内容 |
|---|---|
| `osdk/deps/frame-allocator/src/set.rs:32-35` | `BuddySet::free_counts() -> [usize; MAX_ORDER]`，每个阶的链表长度 |
| `osdk/deps/frame-allocator/src/pools/mod.rs:99-120` | `log_free_counts()`：先放下本地池/全局池的锁，再打印 |
| `osdk/deps/frame-allocator/src/lib.rs:73-85` | `alloc` 返回 `None` 时调用上面的函数 |
| `osdk/deps/frame-allocator/src/lib.rs`（crate 根） | 补了 `__log_prefix`（日志宏要求） |

失败时的输出形如：

```
frame-allocator: allocation failed: size=524288 align=4096
free_local=1234567 free_global=2345678
chunks_local=[...18 个数...] chunks_global=[...32 个数...]
```

判读方法：

- `chunks_local[7] == 0 && chunks_global[7] == 0`：order-7 链表确实空——
  与 H1 轮的 1.59 GiB 全局空闲并不矛盾，说明空闲都在**更小的阶**，或者
  在**别的 CPU 的本地池**里；
- 如果 `chunks_global` 里有 ≥7 的块却仍然失败，那就是分配路径的 bug
  （目前代码读起来不会发生）；
- 本次日志只读**当前 CPU** 的本地池 + 全局池（这正是失败分配的查找范围）。
  其它 CPU 的本地池没有同步机制，直接读会与对方并发修改 `BuddySet` 竞争，
  **所以故意不读**；要把“物理碎片”与“跨 CPU 池滞留”彻底分开，用小栈对照
  实验（见 §4）更安全。

## 3. 三种改法对比

| | A. 多段映射 | B. 降低单次连续要求（32 页 × 4） | C. boot 预留连续池 |
|---|---|---|---|
| 改动位置 | `ostd/src/task/kernel_stack.rs:63-77` | 同 A（B 只是 A 的参数） | boot 内存初始化 + `kernel_stack.rs` |
| 做法 | 分配 N 段、每段 S 页（N×S=128），帧迭代器交给 `map_frames`，仍映射进同一段 132 页虚拟区 | 与 A 相同，取 S=32（4 段，order 5） | boot 时切出一段连续物理内存，内核栈从池里按 128 页切 |
| 连续要求 | **order log2(S)**（S=16 → order 4） | order 5 | 池内是连续的，但池本身在 boot 时一次性拿到 |
| 能否解决碎片 | **大幅缓解**：8 次 order-4 分配，只要有 8 个 64 KiB 块即可；1.59 GiB 空闲下几乎必然成功 | 缓解（4 次 order-5） | 对“栈分配”能，但**池耗尽就失败**，哪怕系统还有空闲 |
| 风险 | 低-中：栈物理不再连续（已核查无消费者）；一段失败要能回滚已分配的段；每次创建多几次分配 | 同 A | 中：改 boot 内存账本；浪费内存（64 MiB 只能供 128 个任务）；并发任务数被硬上限 |
| 浪费 | 无（按需分配） | 无 | 有（预留即占用） |
| 依赖新基础设施 | **不需要**：`map_frames` 已支持任意帧迭代器 | 同 A | 需要池 + 空闲链表 |

### 为什么 B 不是独立方案

内核栈必须是一段**虚拟连续**的 128 页区间（栈指针从 `end_vaddr` 向下生长，
`kernel_stack.rs:78-84,96-98`）。把 4 个 32 页的物理段“拼”起来，只能靠
虚拟映射把 4 段依次映射到连续的虚拟页——这正是 A 的做法。若不做多段映射，
4 个物理段无法变成一段 128 页的栈。因此 **B = A 取 S=32**，差别只是
“4 次 order-5” vs “8 次 order-4”。

### 推荐：A，S = 16 页（order 4）

理由，按重要性：

1. **已有映射基础设施**：`KVirtArea::map_frames(area_size, map_offset,
   impl Iterator<Item = Frame<T>>, prop)` 对每个帧调用一次
   `cursor.map()`，cursor 每次前进一页（`kvirt_area.rs:124-148`；
   `page_table/cursor/mod.rs:429-485`）。帧的物理地址**不要求连续**。
2. **没有物理连续性的消费者**：`KernelStack` 只保存 `KVirtArea` 和
   `end_vaddr`；`TaskOptions::build()` 用 `end_vaddr - 16` 设置 RSP
   （`task/mod.rs:228`）；`flush_tlb` 用的是虚拟区间
   （`kernel_stack.rs:88-94`）。全仓没有把栈指针反算物理地址的代码。
3. **风险/收益比最好**：S=16 把要求从 order 7 降到 order 4，
   在 1.59 GiB 空闲（且大量低阶块）的场景下失败概率极低；
   按需分配、零浪费；不设硬上限。
   S=32（order 5）也可以，但抵抗碎片的能力弱一档；
   S=8（order 3）需要 16 段，创建开销与回滚逻辑更啰嗦。**先取 16。**
4. 失败回滚简单：各段放进固定长度数组，任何一段返回 `Err` 就直接
   `return Err`，已分配段的 `Drop` 会释放帧；只有全部成功后才交给
   `map_frames`（映射后帧的所有权转移给页表，`MappedItem::Tracked`）。

实现草图（**本轮不落地**）：

```rust
const SEG_PAGES: usize = 16;
const NSEGS: usize = (KERNEL_STACK_SIZE / PAGE_SIZE) / SEG_PAGES; // 8

let mut segs: [Option<Segment<KernelStackMeta>>; NSEGS] = [const { None }; NSEGS];
for seg in &mut segs {
    *seg = Some(
        FrameAllocOptions::new()
            .zeroed(false)
            .alloc_segment_with(SEG_PAGES, |_| KernelStackMeta)?,
    );
}
let frames = segs.iter_mut().flat_map(|s| s.take().unwrap().into_iter());
let kvirt_area = KVirtArea::map_frames(
    KERNEL_STACK_SIZE + 4 * PAGE_SIZE,
    2 * PAGE_SIZE,
    frames,
    prop,
);
```

注意：`OSTD_TASK_STACK_SIZE_IN_PAGES` 的语义（可用栈页数）保持不变；
`NSEGS` 由它推导即可；若它不能被 `SEG_PAGES` 整除，退化为
“尽量用大段、最后补一个小段”（或直接断言，建议断言，保持简单）。

### 备选顺序

- 若 A 因故不能做：**C 的轻量版**——不是预留池，而是把现有 TODO 里的
  “失败时归并各 CPU 本地池 → 全局池 → 重试”补上（`pools/mod.rs:73-75`）。
  它不改变栈的分配方式，只是给 allocator 一次自救机会；成本比 A 小，
  但只有在“高 order 块被别的 CPU 拿着”时有效，物理碎片救不了。
- **不推荐**把 C 当第一方案：它会掩盖问题，并在池耗尽时制造新的、
  与系统空闲无关的失败。

## 4. 验证计划（按成本）

1. **本轮**：带上新日志重建内核、重跑 L6。预期在 panic 前看到
   `frame-allocator: allocation failed: size=524288 align=4096 ...`，
   从中读出 order-7 是否为 0、空闲集中在哪些阶。
2. **对照实验（最便宜）**：当前代码 + `OSTD_TASK_STACK_SIZE_IN_PAGES=16`
   重跑 L6。若 panic 消失或显著推迟 → “连续分配”是唯一变量，
   A 的方向正确；**这个实验只用于定性，不是修复**（栈变小会牺牲余量）。
3. **落地 A**（S=16），重跑 L6：目标是从“panic”变成“341 包解包完成、
   upgrade rc=0”；若仍在别的分配点失败，日志会直接指出新的 order。
4. **若 A 之后仍失败**：说明低阶也紧张了，再考虑 page cache 压力回收
   （H1 文档里的第二优先级）——它约束的是 1.6 GiB 的总量增长，
   不是本次的连续块问题。

## 5. 本轮改动

- 只加了诊断日志（`set.rs`、`pools/mod.rs`、`lib.rs`），**未改任何分配/回收行为**；
- 已用 `cargo check -p osdk-frame-allocator --target x86_64-unknown-none`
  验证通过；
- 未重建内核、未重跑 L6（留给下一轮验证）。

## 6. 相关文档

- 帧余量观测与 H1 判定：[`2026-10-24-l6-h1-verification.md`](2026-10-24-l6-h1-verification.md)
  （任务书里提到的 `2026-10-24-l6-h1-data-and-fragmentation.md` 不存在，
  本轮以该文档为准）；
- panic 归因：[`2026-10-24-l6-panic-attribution.md`](2026-10-24-l6-panic-attribution.md)。
