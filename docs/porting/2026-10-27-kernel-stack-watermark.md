# 内核栈真实水位：峰值 131 KiB（拿到数据了）

日期：2026-10-26
内核基线：`a697fe607`
结论一句话：**峰值 134 464 B（131 KiB），且在 systemd 引导阶段就已达到；因此不能按 <64 KiB 的判据缩栈，
也解释了上一轮 32 页（128 KiB）内核为什么起不来。**

## 1. 任务 A：水位数据（WM4，最终版）

```
PROBE wm_available=1
PROBE wm_boot=max_used_bytes=134464 max_used_kib=131 scanned_tasks=159
PROBE wm_after_shell=max_used_bytes=134464 max_used_kib=131 scanned_tasks=225
PROBE wm_with_ff=max_used_bytes=134464 max_used_kib=131 scanned_tasks=503
PROBE ff_procs=9
PROBE wm_after_ff_exit=max_used_bytes=134464 max_used_kib=131 scanned_tasks=716
```

| 阶段 | 已扫描任务 | 运行峰值 |
|---|---|---|
| 引导完成（systemd + 登录） | 159 | **131 KiB** |
| shell 活动后（30 次 fork/exec + `ls -R`） | 225 | 131 KiB |
| Firefox 运行中（9 进程） | 503 | 131 KiB |
| Firefox 退出后 | 716 | 131 KiB |

**判读**：
1. 最深的内核栈用量出现在**引导阶段**，不是 Firefox；Firefox 的线程没有超过 systemd 已创下的记录。
   这符合预期：深调用链在 VFS/驱动/中断路径，而不是用户线程本身。
2. 131 KiB **大于** 128 KiB（32 页），这就是上一轮 `OSTD_TASK_STACK_SIZE_IN_PAGES=32` 编出来的内核
   "systemd 早期卡住、无 panic" 的原因——栈已经越界，不是偶发。
3. 131 KiB 也**远大于**任务书要求的 64 KiB 门槛 ⇒ **不缩栈**，任务 B 不执行。

## 2. 取到数据前踩的三个坑（都修了才拿到数）

| # | 现象 | 原因 | 修法 |
|---|---|---|---|
| 1 | 水位一行都不打印 | `crate::info!` 在门禁 `--loglevel 3` 下被过滤 | 改为写 `/proc/stack_watermark`，完全不依赖日志级别 |
| 2 | 改 `warn!` 后仍然 0 行 | 扫描挂在 `kernel_task_entry` 的 `task_func()` 返回之后，而任务退出走调度器 `exit_current`（`ostd/src/task/scheduler/mod.rs:330-356`） | 改挂 `impl Drop for KernelStack`（任务销毁必到，且此时栈仍映射） |
| 3 | 读数恒为 524288（整栈），明显是假值 | KVirtArea 复用虚拟地址，但物理页是新的；在未 `flush_tlb` 的 CPU 上写入/读取命中了陈旧 TLB 项 | 填之前、扫之前各调一次 `self.flush_tlb(&irq::disable_local())` |

第 3 条尤其值得记住：**任何通过 kernel VA 直接读写新映射内存的测量，都要先处理 TLB 一致性**，
否则会读到上一个使用者的数据（WM3 的 `max_used_bytes=524288` 就是这种假值）。

## 3. 任务 B：不执行（数据不允许）

- 判据要求「三组峰值全 ≤64 KiB 才谈缩栈」，实测 131 KiB；
- 32 页（128 KiB）版本已在上一轮验证过：引导失败、无 panic，与 131 KiB 峰值吻合；
- 若要缩，安全下限应在 131 KiB 之上留余量（例如 256 KiB / 64 页），但按本轮判据不由我决定，
  且 64 页只能把段数从 32 降到 16，预计收益有限（见下）。

## 4. 任务 C：未做（per-CPU 栈缓存池）

时间用在了把水位测量做对上（三次返工）。设计要点先记下来，便于下次直接实施：

- 现在每线程要做 **32 次 4 页段分配 + 128 页映射**（`kernel_stack.rs:92-114`）；
  段大小不能放大到 128 页（注释记录过 order-7 在碎片化下失败），也不宜放大到 16 页（历史上 order-4 也失败过）。
- 因此正确方向不是"少分段"，而是**复用**：把退出的 `KernelStack` 归入每 CPU 的空闲池（有上限，
  例如 8 个/CPU），新任务优先取池；池为空才走分配。需要解决：
  1. `Drop` 里把 `self` 交还池子（Rust 中要用 `ManuallyDrop` 或改成 `Option<KernelStack>` 持有）；
  2. 复用时是否清零（Linux 会清，防止跨任务数据泄漏）；
  3. 池的 TLB 一致性维护（复用同一 VA 时物理页不变，可省一次 flush）。
- 预期：稳态下建线程可省掉全部分配/映射，`d12` 应显著下降；但这是下一轮的事。

## 5. 插桩状态

本轮所有插桩已还原（`git checkout`，并删除临时 `/proc/stack_watermark` 实现），默认内核已重新编译
（`22945600` 字节，19:47）。本轮**没有内核代码改动入库**。

## 6. 证据

- `evidence/2026-10-27-kernel-stack-watermark/WM4-watermark-probe-lines.txt`（最终水位，89 行）
- `…/WM3-tlb-bogus-probe-lines.txt`（未处理 TLB 时的假值 524288）
- `…/watermark-counts.txt`（四轮汇总：WM1/WM2 无行、WM3 假值、WM4 131 KiB）
- 控制台 `~/vb-ws/WM{1,2,3,4}/console.txt`
- 探针 `/tmp/vb/wm_probe.sh`（boot / shell / firefox 三个读数点）
