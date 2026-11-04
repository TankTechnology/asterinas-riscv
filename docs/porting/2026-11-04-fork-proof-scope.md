# `fork` 的验证范围：现在证明了什么，完整证明还差什么

日期：2026-11-04

## 结论

**已经机器检验的是设计层面的 fork 规范，不是 Rust 实现。** 要把"代码实现
某个规范"变成定理（refinement proof），需要程序逻辑 + 模型化的 Rust 语义 +
硬件页表模型，当前仓库的工具链不具备，工作量是研究项目级别（参照 seL4：
约 1 万行 C 的核心内核约 20 人年）。本文把差距写清楚，避免把"设计验证"误当成
"实现证明"。

## 现在已有、可复跑的部分

| 模型 | 工具 | 证明的性质 | 规模 |
| --- | --- | --- | --- |
| `cow-fork/` | loom | 多线程交错下，可写映射的帧没有其他引用（PTE 或 page cache） | 4 个交错场景 |
| `process-lifecycle/` | TLA+/TLC | 回收者、状态发布、僵尸不孤儿、三态互斥、init 存活 | 259 个状态 |
| `fork-spec/` | TLA+/TLC | 设计级 fork：CoW 隔离、引用计数准确、生命周期规则 | 2,855,358 个状态 |

`fork-spec/` 把"不可观察性"表达成 `CoWIsolation`：同一帧被两个进程映射时，
两边都不可写——这是"父进程写不会出现在子进程里"的机制保证；`RefsAccurate`
保证决策所依赖的计数与真实映射一致。三个错误变体（去掉引用计数检查、不重新
挂父、任意进程可回收）都被对应的不变式抓到。

## "完整证明"需要什么

1. **形式规范**：POSIX/Linux 对 `fork` 的语义（✅ 这一步本仓库已有设计级规范）。
2. **程序逻辑**：证明 Rust 代码在该规范下精化——需要 Verus、RefinedRust、
   Creusot 这类工具，或为内核手写分离逻辑（seL4 路线）。
3. **内存模型的桥接**：`fork` 依赖 `unsafe` 的页表/帧操作、架构相关的
   `sfence.vma`、MMIO 与并发锁；必须把这些建模成公理并证明其安全。
4. **环境假设**：硬件页表遍历、TLB 一致性、DMA、固件行为，都要以明确的
   公理进入证明。

## 具体阻塞点（已核对本仓库）

| 阻塞 | 事实 |
| --- | --- |
| 工具链 | Kani 0.68.0 + CBMC 6.11.0 已安装并用于决策函数（见下节）；Verus / Creusot / Prusti / TLAPS 仍不可用 |
| `unsafe` 密度 | fork 与页表路径中 `unsafe` 出现 88 处，页表游标直接操作硬件结构 |
| 构建约束 | 内核是 `no_std` + 自定义 target + 内联汇编 + 全局分配器；Kani 需要一个可编译的 harness，必须为帧分配、页表、TLB 提供桩 |
| 规模 | `kernel/src/vm` + `kernel/src/process` 的 fork 相关路径是数千行，且跨模块（VMA、VMO、文件表、信号、调度器） |
| 并发语义 | 锁顺序、抢占禁用、TLB shootdown 与 IPI，都需要进入模型 |

## 已落地：用 Kani 验证真实的决策函数

`kernel/libs/cow-decision` 是一个无 `unsafe`、无依赖的 crate，
`decide(is_write, writable, is_shared, refcount) -> CowAction` 就是
`handle_single_page_fault` 用的那条规则：

```rust
if !is_write || writable { return None; }
if is_shared || refcount == 1 { Upgrade } else { Copy }
```

`kernel/src/vm/vmar/vm_mapping.rs` 现在**直接调用它**，所以 Kani 验证的是内核
实际执行的代码，不是另写一份复制品。三个 harness（`cargo kani -p cow-decision`，
共 29 条 CBMC 检查，0 失败）：

| Harness | 证明的性质 |
| --- | --- |
| `upgrade_only_for_shared_or_sole_reference` | 对私有映射选择原地升级 ⇒ `refcount == 1` |
| `a_shared_private_frame_is_always_copied` | `!shared ∧ refcount > 1` ⇒ 必然复制 |
| `shared_mappings_never_copy` | 共享映射永不走复制路径 |

对全部输入（两个布尔 + `usize`，约定 `refcount ≥ 1`）有界验证。这套性质正是
`fork-spec` 模型里 `CoWIsolation` 的"机制版"：设计层把它写成不变量，Kani 把它
证明到真实位宽的函数实现上。

**仍然没证明的**：调用方收集的四个参数是否真实（`is_write`、PTE 的 W 位、
`is_shared`、`reference_count()`），以及 `Upgrade`/`Copy` 之后动作的正确性
（`protect_next`、`duplicate_frame`、TLB flush）。这条边界写在
`cow-decision` 的 crate 文档里。

## 现实可行的分阶段路线

1. **抽取纯决策函数**（✅ 已完成）：`handle_single_page_fault` 的判定已抽到
   `kernel/libs/cow-decision`，Kani 在真实位宽与全部输入上证明
   "私有映射只有在独占帧时才原地升级"。下一步是把 `Upgrade`/`Copy` 两个分支
   的动作（`protect_next`、`duplicate_frame`、TLB flush 的调用序列）也纳入
   验证，需要把页表游标抽象成可验证的接口。
2. **验证数据结构不变式**（数月）：`Vmar`/`FileTable` 的并发不变量（例如
   "引用计数 = 映射数"）用 loom 或 Kani 逐条验证，`fork-spec` 的
   `RefsAccurate` 就是这个不变式的规格版本。
3. **精化证明**（研究项目）：用 RefinedRust/Verus 证明实现精化规格，需要
   先为内核的 `unsafe` 层建立分离逻辑接口。

回到 seL4 的对照：它用 20 人年证明了约 1 万行 C 内核的功能正确性，且其
内核比本仓库简单得多（无文件系统、无网络、无 SMP 锁层次）。因此对 "fork 的
完整证明"，诚实的回答是：当前交付的是**可机器检验的设计级证明 + 明确的
实现级缺口清单**，而不是"实现已被证明"。
