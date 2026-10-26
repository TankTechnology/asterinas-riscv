# 内核栈水位：两次尝试都取不到数据（如实记录）

日期：2026-10-26
内核基线：`a697fe607`
结论一句话：**没有拿到水位数字，因此没有缩栈、也没有做任务 B 的分配路径改造。**

## 0. 为什么这轮没有数字

任务 A 要求「先量化再缩栈」，我按魔法字方案实现了插桩，但两轮上板都**没有打印出任何水位行**：

```
WM1（info! 级日志，--loglevel 3）：watermark_lines=0  panic=0
WM2（warn! 级日志，--loglevel 3）：watermark_lines=0  panic=0
```

两次失败的原因已经查清，且都不是"测出来很小"，而是**根本没跑到扫描**：

1. **WM1**：水位用 `crate::info!` 打印，而门禁跑 `--loglevel 3`（Linux 编号下 info=6），
   日志被过滤。内核与探针本身正常（`ff_diag_procs=9`、`page_pixels` 67.8 % 白底），说明插桩无害，只是话没说出来。
2. **WM2**：改成 `crate::warn!`（4 级）后仍然 0 行 ⇒ 问题在**钩子位置**，不在日志级别。
   我把扫描挂在 `kernel_task_entry` 里 `task_func()` 返回之后（`ostd/src/task/mod.rs:233` 附近），
   但任务退出的实际路径是调度器的 `exit_current`（`ostd/src/task/scheduler/mod.rs:330-356`
   的文档示例就是这个模式），用户线程根本不会从 `task_func()` 返回到那一行。
   所以 `watermark_scan()` 从未被调用。

代码已在收尾时 `git checkout` 还原（`ostd/src/task/kernel_stack.rs`、`ostd/src/task/mod.rs`），
默认内核重新编译（`22945600` 字节，19:34）。本轮**没有内核代码改动被提交**。

## 1. 任务 A 的实现（已还原，留档）

在 `ostd/src/task/kernel_stack.rs` 里：

```rust
const STACK_WATERMARK_ENABLED: bool = true;
const STACK_WATERMARK_PATTERN: u8 = 0xAA;
static STACK_WATERMARK_MAX: AtomicUsize = AtomicUsize::new(0);
static STACK_WATERMARK_TASKS: AtomicUsize = AtomicUsize::new(0);

fn watermark_fill(&self) {            // 分配后整段填 0xAA
    unsafe { core::ptr::write_bytes(self.stack_start() as *mut u8,
                                    STACK_WATERMARK_PATTERN, KERNEL_STACK_SIZE); }
}
pub(super) fn watermark_scan(&self) {  // 从栈底找第一个非魔数字节
    // used = KERNEL_STACK_SIZE - i；新高则 crate::warn!("stack watermark: used=…")
}
```

`stack_start()` = `kvirt_area.range().start + 2 * PAGE_SIZE`（两个 guard 页之上）。

## 2. 两次上板的原始证据

- WM1：`WM1-probe-lines.txt`（89 行）、`watermark-counts.txt`
  —— 探针完整跑完（`ff_diag_procs=9`、`page_pixels … top=#ffffff 0.678`），水位行 0 条。
- WM2：`WM2-probe-lines.txt`（89 行），同样 0 条；`panic=0`。

两轮都证明：**插桩版内核能正常启动并跑 Firefox**，问题纯粹是扫描没被执行到。

## 3. 任务 B：未做，且有两个先验约束

1. **B1（一次连续分配 512 KiB）与源码注释直接冲突**：`ostd/src/task/kernel_stack.rs:43-50` 明确记录，
   这个 32 段拆分就是为了避开碎片化——"单块 128 页（order-7）在 guest 3340 s 分配失败，
   拆成 16 页（order-4）后 3459 s、3578 s 又失败，最后定在 4 页（order-2）"。
   所以 B1 会重新引入已知故障，**不应做**。
2. **B2（批量映射）其实已经存在**：`KVirtArea::map_frames`（`ostd/src/mm/kspace/kvirt_area.rs:126-150`）
   在**一次** `irq_guard` 内用一个 cursor 遍历映射所有页，不存在"每页单独取锁/单独 mmap"的问题。
   真正剩下的固定成本是 **32 次 4 页段分配**本身。

3. B3（按需缺页映射栈页）改动大，且没有水位数据兜底（不知道最深路径要多少），本轮不做。

## 4. 下一步（有明确落点）

1. 把 `watermark_scan()` 移到**任务退出必到**的位置，二选一：
   - `ostd/src/task/scheduler/mod.rs` 的 `exit_current()` 里、`dequeue_current` 之前（进程上下文，可安全打日志）；
   - 或给 `KernelStack` 实现 `Drop`（此时栈仍映射），只在 Drop 里更新原子值，再由某个定时/周期性路径打印
     （Drop 可能处于原子上下文，不适合直接 `warn!`）。
2. 拿到峰值后，若峰值 ≤ 64 KiB，按"峰值 + 余量"把 `DEFAULT_STACK_SIZE_IN_PAGES` 从 128 降到 64/96，
   再跑 pthread 基准与 7 域矩阵；**在此之前不缩栈**。
3. 分配段数的独立优化（例如段大小 8 页 = 16 段）需要先确认碎片化回归风险，注释里的历史失败点是前车之鉴。

## 5. 证据

- `evidence/2026-10-27-kernel-stack-watermark/WM1-probe-lines.txt`、`WM2-probe-lines.txt`（各 89 行）
- `…/watermark-counts.txt`（两轮都是 `watermark_lines=0 panic=0`）
- 控制台 `~/vb-ws/WM1/console.txt`、`~/vb-ws/WM2/console.txt`
- 插桩源码未入库（已还原），关键片段见本文 §1
