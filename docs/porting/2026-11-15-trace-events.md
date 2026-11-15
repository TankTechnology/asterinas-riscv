# 关键埋点（阶段 2，2026-11-15）

阶段 1 的骨架之上，把真正有用的事件接上。现在 `/proc/trace/available_events` 有 11 个：

```
block:block_rq_issue      block:block_rq_complete
irq:irq_handler_entry     irq:irq_handler_exit
mm:page_fault_user
net:net_poll
sched:sched_switch        sched:sched_wakeup
syscall:sys_enter         syscall:sys_exit
tracing:trace_marker
```

## 1. 每个事件埋在哪、为什么

| 事件 | 位置 | 说明 |
| --- | --- | --- |
| `sched_switch` | ostd 新增的 `inject_switch_handler`，在 `before_switching_to` 调用 | 记录里自己的 pid/tid 是"让出者"，payload 的 `next_pid/next_tid` 是"下一个" |
| `sched_wakeup` | 内核调度器 `ClassScheduler::enqueue`，`EnqueueFlags::Spawn` 不记 | 与 Linux 一致：创建不算唤醒 |
| `sys_enter` / `sys_exit` | `handle_syscall` 入口与出口 | 出口带调用者看到的返回值 |
| `irq_handler_entry/exit` | ostd 的 `call_irq_callback_functions`，新增 `inject_irq_hook` | 中断上下文，只做 ring push |
| `page_fault_user` | `Vmar::handle_page_fault` 入口 | `address` + 是否写 |
| `block_rq_issue/complete` | `aster-block` 的 `Bio::submit` / `Bio::complete`，新增 `inject_bio_hook` | 所有读/写都走这两个点，不用改每个文件系统 |
| `net_poll` | 内核 `poll_ifaces` 与后台轮询线程 | 见下面的偏差说明 |

两个"注入钩子"沿用 ostd 已有的 `inject_*_handler` 模式：库 crate 不能反过来依赖内核，
所以内核在 `crate::trace::init()` 里注册回调。

## 2. 与目标的一处偏差（诚实记录）

- **`net` 只有 `net_poll`，没有逐包的 `net_rx/net_tx`**：收发包循环在 `aster-bigtcp`
  里，那是库 crate，看不到内核事件。逐包事件需要在 `poll_ingress/poll_egress` 里注入
  类似的钩子，属于阶段 2 的后续项。现有的 `net_poll` 已经能回答"哪个接口、什么时候被
  轮询、多频繁"。
- **`sched_migrate` 未做**：迁移决策在内核 `select_cpu`，但目前没有稳定的"迁移发生"信号，
  先留空。

## 3. 验收（板上）

一个"地标包围的负载"是这套东西的正常用法：

```
for e in sched/sched_switch sched/sched_wakeup syscall/sys_enter mm/page_fault_user; do
    echo 1 > /proc/trace/events/$e/enable
done
echo trace-demo-start > /proc/trace/trace_marker
sh -c "ls /usr/share/doc | wc -l" > /dev/null
echo trace-demo-end > /proc/trace/trace_marker
cat /proc/trace/trace
```

结果（1941 行、时间戳精确到微秒、CPU/pid/tid 齐全）：

```
--- counts: switch=54 wakeup=24 sysenter=405 faults=601
   58.408564 [  0] 635/635 tracing:trace_marker: trace-demo-start
   58.408582 [  0] 635/635 syscall:sys_enter: nr=24
   ...
   58.408740 [  0] 635/635 syscall:sys_enter: nr=79
```

各事件族单独验证过：

| 事件族 | 触发方式 | 结果 |
| --- | --- | --- |
| sched | 4 个线程 sleep/join | 673 次 switch、346 次 wakeup，`next_pid` 链正确 |
| mm | Python 触摸 8 MiB | 1360 次 `page_fault_user`，地址/读写标志正常 |
| irq | 空闲 0.3 s | 1026 次 entry（timer 为主） |
| block | `dd` 写 256 KiB + 读回 | 1023 次 issue、1026 次 complete，`sector/nr_sectors/kind` 正常 |
| net | `curl https://www.baidu.com` | 2 次 `net_poll`（`iface=eth0`） |

稳定性：以上负载跑完 `--failed` 为空、桌面正常、无 panic。

## 4. 用法备忘

- 打开事件：`echo 1 > /proc/trace/events/<sub>/<event>/enable`
- 全局暂停：`echo 0 > /proc/trace/tracing_on`
- 只看地标段：`echo mark > /proc/trace/trace_marker` 前后包住负载
- 事件全关时开销只有一次 relaxed load + 分支（阶段 1 实测 200k getpid：8.1 vs 10.3 µs/次）
