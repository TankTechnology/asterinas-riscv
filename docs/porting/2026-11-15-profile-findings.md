# 用新 profiler 定位三个性能问题（2026-11-15）

工具：`/proc/trace` 采样器（HZ≈1000、全 CPU）＋ `sched_wakeup/switch` 事件＋板上的
`/root/prof.py`（按窗口清空环形缓冲后累加，避免只看到最近 0.5 s）。

## 1. MMC 小 I/O：`wait_sdma` 轮询是绝对大头

先排掉两个假象：`/tmp` 是 tmpfs；`dd` 不带 `oflag=direct` 时测的是 page cache。
下面是 `/data`（/dev/mmcblk0p3）上的真实数字：

| 负载 | 吞吐 | `wait_sdma` 占非空闲样本 |
| --- | --- | --- |
| 直写 4 KiB × 512 | **1.1 MB/s** | **73%** |
| 直写 64 KiB × 64 | 4.6 MB/s | 54% |
| 直读 4 KiB × 512 | — | 不显著（读走另一条路径） |

- 4 KiB 写：每请求 3.9 ms；64 KiB 写：每请求 14 ms（合并生效，单位吞吐 4 倍）。
- 真正空闲时（桌面不产生 I/O）`wait_sdma` 只有 0.1%——之前看到的"空闲 9%"其实是
  桌面周期写盘时的数字，**不是常驻轮询开销**。
- 结论：瓶颈是**每个请求的 DMA 完成等待**，不是带宽。方向是继续合并/加大请求
  （沿用写回合并那条路），而不是再试中断模式（memory 里已实测更慢）。

## 2. fork / exec

`/root/spawnbench`，500 次一轮，target `/bin/true`：

| 模式 | 每次耗时 | 子进程 sys | 子进程 minflt |
| --- | --- | --- | --- |
| fork | 1 043 µs | 161 ms | 3 001 |
| **fork + exec** | **3 513 µs** | 1 088 ms | **31 615** |
| vfork | 403 µs | 28 ms | 0 |
| vfork + exec | 2 927 µs | 984 ms | 28 638 |
| forke → 静态二进制 | 2 543 µs | 737 ms | 18 856 |

- `fork` 只占 1 ms，**exec 独占 ~2.2 ms 子进程内核时间与 63 次缺页**。
- vfork 省掉页表拷贝（403 µs），但 exec 仍是主要成本。
- 静态目标省 0.9 ms（动态链接器）。
- 采样热点（800 次 forke）：页表 `Cursor`（5.3% 非空闲）、`UserContext`（4.4%）、
  `PageTablePageMeta`（3.7%）、`TlbFlusher`（3.0%）、`memmove`（2.9%）、
  `FrameAllocator::alloc`（2.7%）、`remote_fence_i_all_online_harts`（2.4%）、
  页表锁 `dfs_acquire_lock`（2.1%）。
- 结论：没有单一热点，成本分散在"缺页 + 页表 + TLB/IPI"。可做的：
  exec 时批量建立映像映射（减少缺页次数）、页表锁粒度、以及 I-cache IPI 的合并。

## 3. 桌面交互：不是 CPU 饱和，是每次事件的延迟

- **X 往返延迟 775 µs/次**（单进程内 300 次 `xdotool getactivewindow`，232 ms）。
- **wakeup→运行**：中位数 75–99 µs，但尾部很长：空闲任务 3 ms，Firefox 一次 8.8 ms
  （采样量受环形缓冲限制，只抓到少量配对；需要更长的窗口工具才能给分布）。
- 交互期间 CPU 并不忙：300 次指针移动只有 16.5% 非空闲样本；其中用户态约 45%
  （Xorg/Firefox，未符号化），内核约 55%。
- 内核侧热点（拖动 300 次）：`UserContext` 切换 24%（占内核样本）、`TlbFlusher` 21%、
  定时器/中断 ~30%、`Pollee::notify` + 堆分配 + 调度 ~25%。
- 结论：体感卡顿来自**每个事件 ~0.8 ms 的往返**与偶发的毫秒级调度尾部，而不是吞吐。
  方向：减少事件路径上的内核工作（上下文切换、TLB 刷新、堆分配），并压掉调度尾部；
  图形栈本身（fbdev 软件渲染）是用户态时间，要动它才轮得到 DRM/KMS。

## 4. 复现方式

```
scp /root/prof.py board:  # 或直接看 docs 里引用的脚本
python3 /root/prof.py 5 "dd if=/dev/zero of=/data/x bs=4k count=512 oflag=direct conv=fsync"
python3 /root/prof.py 6 "/root/spawnbench forke 800 /bin/true"
python3 /root/prof.py 5 "xdotool <300 个 mousemove>"
```

采样率约为每 CPU 1 kHz（HZ=1000），环形缓冲 512 条/CPU，`prof.py` 每 0.4 s 清空并累加。
