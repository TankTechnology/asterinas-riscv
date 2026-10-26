# 缺页账：只占内核时间 5 %，自证失败——超过 80 % 的内核 CPU 在别处

日期：2026-10-26
内核基线：`a966812e2`
本轮：加临时缺页计时（RAII guard，热点只做 rdtsc + 原子加）→ 测 → **已 revert**

## 0. 结论

1. **缺页不是那 90 % 的去处**：3 轮里缺页总计 **61–65 ms 每 8 s**（热轮 61.3 ms），
   仅相当于主进程内核 CPU（**1.25 s**）的约 **5 %**。
2. **自证没通过**（任务书要求「缺页 + syscall ≈ stime ±30 %」）：
   热轮 `pf 61.3 ms + syscall 134.8 ms = 196 ms`，只占 stime 1.25 s 的 **16 %**。
   按任务书的判据，这说明**还有大量非 syscall、非缺页的内核活动**。
3. 因此任务 B 的验收（缺页时间降 30 % / 冷启动降 300 ms）**从量级上就不可能达成**：
   把缺页全部消掉也只能省 61 ms。任务 B 未执行。
4. 新目标：剩下 **≈84 % 的内核时间**要么在**调度/上下文切换**，要么在中断/IPI、
   要么被计到了别的进程。下一轮应直接给 `switch_to`/调度路径加同样的轻量计时。

## 1. 数据（PF1，3 轮 × 固定 8 s，无采样器）

```
run1_pf=b0:count=65   total_ns=322573     avg_ns=4962
         b1:count=933  total_ns=9859961    avg_ns=10568
         b2:count=1751 total_ns=243533483  avg_ns=139082
         b3:count=3990 total_ns=78287176   avg_ns=19620
         total:count=6739 total_ns=332003194 avg_ns=49265      ← 冷轮，被 run1 的换入污染
run2_pf= b0:66/233665  b1:941/7584562  b2:1772/29359564  b3:3988/24141476
         total:count=6767 total_ns=61319269 avg_ns=9061       ← 热轮，取这组
run3_pf= total:count=6769 total_ns=64558949 avg_ns=9537
run2_ffstat utime=28 stime=125     run3_ffstat utime=25 stime=134
run2_syscall_total cpu_ns=134839681 count=6679
```

桶定义：`b0` 匿名读、`b1` 匿名写、`b2` 文件读、`b3` 文件写
（按 `file.is_some()` × `is_write` 分类；「写」包含了 COW/write-protect 触发的路径，
细分需要再看 PTE 是否已存在，本轮没有细分）。

| 桶 | 次数（热轮） | 总时间 | 单次 |
|---|---|---|---|
| b0 匿名读 | 66 | 0.23 ms | 3.5 µs |
| b1 匿名写 | 941 | 7.58 ms | 8.1 µs |
| **b2 文件读** | **1 772** | **29.36 ms** | **16.6 µs** |
| b3 文件写 | 3 988 | 24.14 ms | 6.1 µs |
| 合计 | 6 767 | **61.3 ms** | 9.1 µs |

## 2. 自证（任务书的硬性要求，未通过）

| 项 | 数值 | 占 stime(1.25 s) |
|---|---|---|
| 缺页合计 | 61.3 ms | 4.9 % |
| syscall 合计 | 134.8 ms | 10.8 % |
| **两者相加** | **196.1 ms** | **15.7 %** |
| stime | 1.25 s | 100 % |

要求「≈ stime ±30 %」，实测 15.7 % ⇒ **判定：存在其它大量非 syscall、非缺页的内核活动**。
（计数口径差异不能解释这个缺口：pf 与 syscall 都是**全系统**累计，而 stime 只是主进程一个进程；
即使用全系统口径，两者相加仍远低于单进程的 stime。）

## 3. 下一步（按本报告数据）

1. **给调度路径加同样的计时**：`switch_to`（`ostd/src/task/scheduler/mod.rs` 的 `resched`/`switch_to`）
   入口出口 rdtsc + 每桶累加，看「上下文切换 + 内核栈 TLB flush」占多少。
   `KernelStack::flush_tlb`（含 IPI 可能）和 `sched` 队列操作都是候选。
2. 若调度路径仍不够，再查中断/IPI 与 idle。
3. 缺页侧（若下一轮仍要做）：文件读缺页单次 16.6 µs 是最贵的一档，可与上一轮落地的
   readahead 衔接（`handle_page_faults_around` 是否走了批量读）。

## 4. 插桩说明

临时插桩：`vm/vmar/pf_stats.rs` 四桶原子计数 + `PfTimer` RAII + `/proc/pf_stats`（支持 `reset`），
**已全部 revert**，默认内核 22:28 重编译为基线字节 `22945600`。

## 5. 证据

- `evidence/2026-10-27-pagefault/PF1-fault-accounting.txt`（13 行）
- 控制台 `~/vb-ws/PF1/console.txt`
- 探针 `/tmp/vb/pf_probe.sh`
