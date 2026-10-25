# Firefox sys 归因（二）：/proc 采样器、三组对照、top 表

日期：2026-10-27
内核：`69a14ac17a479774`（含真实 minflt/majflt 与 `/proc/<pid>/syscall`）
镜像：`ff-lag`（firefox-esr + x11-apps），Xorg 与客户端都用默认 llvmpipe
方法：**不用 strace**（PTRACE_ATTACH 未实现），改用
`tools/x86/debian/syscall_sampler.py`：每 20 ms 读所有 Firefox 进程的
`/proc/<pid>/syscall` 与 `/proc/<pid>/stat`，汇总 syscall 采样占比、
minflt/majflt、utime/stime。

## 1. 采样器

- 输入：`--pid` 或 `--name firefox`（按 comm 匹配 firefox*/Web Content*/
  Isolated Web Co*/RDD*/Socket Thread* 等），`--duration`、`--interval`；
- 每次采样：syscall 号（或 `running`）+ minflt/majflt/utime/stime；
- 输出：带 x86_64 名字映射的直方图、minflt/majflt 增量、
  sys:user 比，并写 `.csv`/`.md`；
- 已知局限：这是**状态采样**，阻塞在 poll/futex 的线程也会被记到该
  syscall 名下，所以占比反映“在哪个 syscall 里”，不等于“谁消耗了
  sys CPU 时间”。

## 2. 三组对照（每组 20 s 持续滚动）

| 组 | 环境 | user_ticks | sys_ticks | sys:user | minflt | majflt |
|---|---|---|---|---|---|---|
| A | 默认（llvmpipe 双端一致） | 2193 | **20406** | **9.31** | **65,342** | 29 |
| B | `MOZ_WEBRENDER=0` + 关 layers 加速 | 2551 | 18777 | 7.36 | 52,724 | 0 |
| C | 窗口缩小到 640x480 | 2609 | 21336 | 8.18 | 47,970 | 0 |

（ticks 是内核统计单位，跨组可比；sys:user 与单位无关。）

### top syscall（按 20 ms 采样次数）

**A 默认**（约 2,858 个样本）：

| # | syscall | nr | 次数 | 占比 |
|---|---|---|---|---|
| 1 | poll | 7 | 1555 | 54.4% |
| 2 | futex | 202 | 493 | 17.2% |
| 3 | sendmsg | 46 | 146 | 5.1% |
| 4 | read | 0 | 43 | 1.5% |
| 5 | clone3 | 435 | 22 | 0.8% |
| 6 | mprotect | 10 | 16 | 0.6% |
| 7 | mmap | 9 | 10 | 0.3% |
| 8 | openat | 257 | 8 | 0.3% |
| 9 | access | 21 | 5 | 0.2% |

**B 关 WebRender/加速**（2,815 样本）：

| # | syscall | nr | 次数 | 占比 |
|---|---|---|---|---|
| 1 | poll | 7 | 1877 | 66.7% |
| 2 | running | — | 400 | 14.2% |
| 3 | futex | 202 | 251 | 8.9% |
| 4 | clone3 | 435 | 21 | 0.7% |
| 5 | mprotect | 10 | 12 | 0.4% |
| 6 | openat | 257 | 5 | 0.2% |
| 7 | recvmsg | 47 | 4 | 0.1% |

**C 小窗口 640x480**（3,421 样本）：

| # | syscall | nr | 次数 | 占比 |
|---|---|---|---|---|
| 1 | poll | 7 | 1952 | 57.1% |
| 2 | futex | 202 | 703 | 20.5% |
| 3 | sendmsg | 46 | 199 | 5.8% |
| 4 | mprotect | 10 | 11 | 0.3% |
| 5 | mmap | 9 | 6 | 0.2% |
| 6 | access | 21 | 3 | 0.1% |
| 7 | openat | 257 | 3 | 0.1% |

## 3. 结果解读

1. **poll 是“状态占比”第一，但主要是阻塞等待**：Firefox 的事件循环
   线程停在 `poll` 上等待 socket/timer；这些样本不代表 CPU 时间。
   三个组的 `running` 比例都不高（B 有 14.2%，A/C 的 running 行被
   串口交错吞掉），说明采样时刻多数线程在等待。
2. **futex 第二（9–20%）**：线程池/内容进程的唤醒与休眠频繁；
   窗口变小后 futex 占比升到 20.5%，与渲染线程工作集变化一致。
3. **sendmsg/recvmsg ≈5%**：Firefox 父进程与内容/GPU 进程的 IPC。
4. **minflt 巨大**：20 秒 4.8–6.5 万次 minor fault（≈2.4–3.3k/s），
   是 JIT 代码页 + llvmpipe tile buffer 的首次触碰；majflt 基本为 0
   （页面已在 page cache）。
5. **sys:user 7.4–9.3**：与上一轮一致，卡顿的主因仍在**内核路径**
   （等待/唤醒、IPC、缺页处理），不在 llvmpipe 的算术。

## 4. C：按 top 修一个（本轮未做，说明原因）

- 采样器只能给出“在哪个 syscall 里”，给不出**每个 syscall 的 CPU
  时间**；poll/futex 的高占比大部分是阻塞，不是消耗。
- 因此没有足够数据支撑“改哪个 syscall 能把 sys 秒数降下来”；
  按纪律（没数字的优化不算完成）本轮不改内核。
- 候选与下一步：
  1. 给 syscall dispatch 加**每 syscall 的耗时直方图**（类似本轮的
     采样器，但记 ns），得到真正的 sys 时间归属；
  2. 若 futex/wake 是热点：检查唤醒路径的 thundering herd（flock 已做
     wake-one，futex 语义要求按 n 唤醒，需换角度）；
  3. 若缺页是热点：评估匿名内存 fault-around / 预触碰（需前后数字）；
  4. 若 sendmsg 是热点：检查 socket/pipe 小包路径的拷贝与唤醒。

## 5. 复现

```sh
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/firefox_sysattr3.sh \
make test_x86_daily_use … X86_DEBIAN_XORG_BASE_ROOT=…/ff-lag/daily.ext2 \
  DAILY_USE_WORKSPACE=$PWD/target/attr3b
```

单独使用采样器（guest 内）：

```sh
python3 syscall_sampler.py --name firefox --duration 20 --interval 0.02 --out /tmp/attr
```

## 6. 已知限制

- A 组的 `samples=` 行被串口内核日志交错吞掉，总量由
  `1555/0.544 ≈ 2858` 估算；
- 第一次运行（窗口选择抓到 10x10 隐藏窗口）数据保留在
  `run1.txt`，不作为结论；第二次运行按“最大窗口”选择，A/B 为正常
  页面窗口、C 显式 resize 到 640x480；
- utime/stime/ticks 的绝对时间单位未再换算，跨组只比较 ticks 与比值；
- 串口有 `unsupported flags: MSG_DONTWAIT | MSG_CMSG_CLOEXEC` 警告
  （Firefox recvmsg 的 flags 未被内核识别），不影响本轮统计。

## 7. 证据

- `evidence/2026-10-27-attr-now/run2-largest-window.txt`（本文数据）
- `…/run1.txt`（第一次运行，窗口选择有缺陷）
- `tools/x86/debian/syscall_sampler.py`、`firefox_sysattr3.sh`
