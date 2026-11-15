# 采样 profiler（阶段 4，2026-11-15）

阶段 4 复用阶段 1 的环形缓冲：定时器中断里采一次 PC，按符号聚合成直方图。它已经能直接
回答"哪个函数在吃 CPU"——第一轮就在空闲的板子上找出 `MmioHost::wait_sdma` 长期占 9%。

## 1. 设计

```
定时器中断（每个 tick、每个 CPU）
  └── ostd::irq::call_timer_sample_hook(trap_frame.sepc, user?)
        └── kernel trace::profile::sample()
              ├── events/profile/sample 关闭 → 返回（一次 relaxed load）
              └── ring::push_at(SAMPLE, pid, tid, pc + user 标志, Jiffies)
```

- **采样源**：osdt 在 RISC-V 定时器中断入口新增 `inject_timer_sample_hook(fn(pc, user))`，
  采样发生在处理器运行之前，所以样本是"被打断的上下文"，不含中断自身的工作。
- **开关**：就是事件开关本身——`echo 1 > events/profile/sample/enable` 开始采样，
  `echo 0` 停止。没有额外旋钮。
- **内核/用户**：用户态样本记录 pc 但不做符号化（内核不读用户符号表），仍按 pid 计入
  直方图，用来回答"这段时间是谁在吃 CPU"。
- **时间戳用 Jiffies**：与函数跟踪同样的原因——采样发生在时钟源更新路径附近。

## 2. 接口

```
/proc/trace/events/profile/sample/enable   采样开关
/proc/trace/trace                         原始样本行（pc=<符号>+偏移 / pc=<hex> user）
/proc/trace/profile                       当前缓冲区里样本的直方图（内核在前，用户按 pid 在后）
```

## 3. 验收（板上）

**空闲 3 秒**（能看到常驻的轮询线程与空闲循环）：

```
# samples: 2048 (kernel 1003, user 1045)
     185    9.0% <aster_mmc::arch::MmioHost>::wait_sdma
     170    8.3% <aster_kernel::init::ap_idle_loop ...>
      81    4.0% <ostd::mm::tlb::TlbFlusher<...>>
      79    3.9% ostd::irq::bottom_half::process
```

**单核忙等（Python）**：内核空闲样本下降，用户样本按 pid 出现（`<user> pid 544` 17.6%）。

**MMC 写（`dd` 256 KiB）**：`wait_sdma` 升到 19.1%，与已知的"MMC 轮询 DMA 完成"
瓶颈一致。

**fork/exec（300 次）**：热点落在 `TlbFlusher`（4.0%）、页表 `Cursor`（3.1%）、
`irq::bottom_half::process`（4.0%）、`UserContext` 切换（2.6%）——与进程创建的
实际工作吻合。

**与 `/proc/stat` 对账**：同一窗口的 user/系统时间比例与直方图里 user/内核样本比例
同量级（采样是系统级、所有 CPU 合计，`/proc/stat` 也是）。

## 4. 已知限制（下一轮再做）

- 采样是**系统级**的，没有按 pid/CPU/符号的过滤器；桌面进程的样本会混进来（直方图里
  用户样本按 pid 分开列，便于排除）。
- 缓冲区 512 条/CPU，覆盖写：一次只能看最近约 0.5 秒（4 CPU、1 kHz 定时器）；要看更久
  需要周期性把 `/proc/trace/profile` 拿去外部聚合。
- 没有调用链（栈回溯）：只有 PC。阶段 3 的函数跟踪可以补因果，但两者不能同时开
  （插桩内核 + 采样会互相淹没缓冲区）。
- 用户态不做符号化。

## 5. 用法

```sh
echo 1 > /proc/trace/events/profile/sample/enable
<跑要分析的工作负载>
echo 0 > /proc/trace/events/profile/sample/enable
head -20 /proc/trace/profile
```
