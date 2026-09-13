# Firefox 页面阻塞：代码关系与观测缺口

## 范围和结论边界

本记录响应“代码分析、系统调用关系、有效日志、有信息量的真机实验”的排查要求。
本轮只检查源码和已有实验产物，没有启动 QEMU、操作串口、修改内核或重新安装工具。
下面的观测和实验建议尚未实现，也不代表已经定位 Firefox 的根因。

分析源码为 `codex/megrez-physical-graphics-current-main`，HEAD 为
`cc66b9d0b2b90c046abd7ffa6db2fb0e86a9e2d0`。
工作区已有未提交的验收脚本修改；内核与 OSTD 没有工作区修改。
运行时输入、历史对照和验收边界见[原始实验记录](2026-09-08-physical-graphics-setup-diagnostics.md)。
内核映像 SHA-256 不是 Git 提交号，不能拿映像哈希直接做源码差分。

## 重新核对的运行证据

证据目录为工作区的 `target/current-main-physical-graphics/`。

| 证据 | 已观察到的事实 | 不能据此认定的结论 |
| --- | --- | --- |
| `witness-current-host.log` | NewSession 在 457.646 秒返回；Navigate 在 464.843 秒返回；下一条 ExecuteScript 在 849.872 秒前未返回 | 不能区分请求未处理、页面进程失去响应、IPC 等待或返回传输阻塞 |
| `witness-historical-host.log` | 同一 witness root 下，历史内核能返回页面检查并进入 READY | 不是完整交互通过，也没有定位到单个内核改动 |
| 当前串口日志第 435 行 | Firefox 报告 `waiting for process 355 failed with error 10` | 尚不知道 355 的角色、父进程、退出原因以及是否已被其他等待者回收 |
| 同一轮历史对照串口日志 | 没有找到上述进程等待报错；仍有 glxtest、WaitFlushedEvent 和 BackupService 报错 | 一次日志差异不是因果证明；共同报错也不代表相应子系统完全正确 |
| 当前内核原始输入诊断 | VirtIO 键鼠事件到 evdev、X11 的一次事务成立 | 不覆盖 Firefox 内容处理、真实 USB 或 HDMI |

当前失败轮的实际启动参数包含 `loglevel=off`，没有启用 syscall/futex profile。
因此，串口中没有相关内核诊断，不能解释为没有发生 syscall 错误或缺页错误。
从保留的运行磁盘用只读 `debugfs` 查看时，Firefox stderr 和 Mozilla 日志文件显示为零字节，
但串口中实际存在 Firefox stderr 内容。
这说明离线磁盘文件不足以构成完整日志证据；尚未确定是哪个持久化环节造成差异。

## 源码已经确认的问题与风险

### 1. 现有 profiler 不能定位一个仍未返回的调用

[`syscall_profile_slot`](../../../kernel/src/syscall/mod.rs) 只覆盖 21 类调用。
其中有 futex、ppoll、epoll_pwait、read/write，
没有 socketpair、sendmsg/recvmsg、sendto/recvfrom、readv/writev、epoll_ctl、wait4/waitid。
`syscall_profile_end` 才记录慢调用详情和每进程完成统计。
全局 entered/completed 差值可以反映未完成数量，但没有每个未完成线程的调用身份。
这尤其不适合定位本次“长时间没有返回”的症状。

此外，累计 syscall 时间包含睡眠和不同线程重叠的时间，不能当作 CPU 消耗。
每进程表采用 PID 取模且碰撞时跳过；后续证据必须明确记录丢失/碰撞，而不是默认为完整。

### 2. socket 缓冲区修复的作用范围和入口覆盖不一致

[`CurrentUserSpace::prefault`](../../../kernel/src/context.rs) 在实际 I/O 前遍历整个用户缓冲区。
[`recvmsg`](../../../kernel/src/syscall/recvmsg.rs) 和
[`sendmsg`](../../../kernel/src/syscall/sendmsg.rs) 没有按 TCP 与 Unix socket 区分这一步。
[`read`](../../../kernel/src/syscall/read.rs) 和
[`write`](../../../kernel/src/syscall/write.rs) 对所有 socket 启用它。
所以即使 Firefox 控制连接走 TCP，浏览器内部 Unix IPC 也可能受到同一改动影响。

但 [`readv`](../../../kernel/src/syscall/preadv.rs) 和
[`writev`](../../../kernel/src/syscall/pwritev.rs) 逐个 iovec 调用 `file.read/write`，没有对应的预缺页步骤。
这不是要求立即给它们也加全缓冲区预缺页；应该先验证需要复制的实际范围、短读写、EOF 和错误优先级。
TCP 中的用户复制仍发生在持有流状态保护的路径内，见
[`StreamSocket::try_recv/try_send`](../../../kernel/src/net/socket/ip/stream/mod.rs) 和
[`ConnectedStream`](../../../kernel/src/net/socket/ip/stream/connected.rs)。
这些是需测试的风险与覆盖差异，不是当前 Firefox 已触发这些路径的证据。

### 3. procfs 采集脚本依赖尚不存在的接口

[`TidDirOps::STATIC_ENTRIES`](../../../kernel/src/fs/fs_impls/procfs/pid/task/mod.rs)
没有 `syscall`、`io`、`wchan`、`stack`。
[`browser_web_firefox.sh`](../../../tools/riscv/debian/rootfs/browser_web_firefox.sh)
的可选采样却尝试读取前两项，并把读不到的内容归并为 `none`。
需要区分“接口未实现”“进程已退出”“权限不足”“采样超时”和“数据为空”。
已有 `status`、`comm`、`cmdline`、`fd`、`fdinfo`、`maps` 可以作为有限快照的基础，
但具体字段完整性仍需按实现检查，不能假设等同 Linux。
也不应导出完整环境变量或用户数据来代替必要的诊断元信息。

### 4. 进程等待报错需要独立于 TCP 假设追踪

[`Errno`](../../../kernel/src/error.rs) 中 10 是 `ECHILD`。
[`sys_wait4`](../../../kernel/src/syscall/wait4.rs) 涉及调用者 PID namespace 的目标转换；
[`do_wait`](../../../kernel/src/process/wait.rs) 在没有匹配子进程或 tracee 时返回 `ECHILD`。
应该记录创建、父子关系、namespace 身份、退出、回收和等待者，而不是只记录 errno。
正常的重复回收、子进程重新托管也可能产生等待错误，不能直接归因于 wait 实现。

Mozilla 的[ESR 140.15 进程清理源码](https://raw.githubusercontent.com/mozilla-firefox/firefox/FIREFOX_140_15_0esr_RELEASE/ipc/chromium/src/chrome/common/process_watcher_posix_sigchld.cc)
显示，该消息来自进程终止检查；SIGCHLD 通过 pipe 通知 I/O 线程再处理清理。
这是上游代码关系的佐证，不是已核实 Debian 补丁后全部行号与二进制行为一致。

## 系统调用关系：应追踪“谁在等谁”

本地验收客户端通过 loopback TCP 发送 Marionette 请求。
在 Mozilla 的[ESR 140 驱动实现](https://raw.githubusercontent.com/mozilla-firefox/firefox/esr140/remote/marionette/driver.sys.mjs)中，
页面脚本执行最终委托给 browsing-context actor。
所以控制请求进入 Firefox，不等于页面执行请求已被处理。

| 边界 | 相关调用和内核路径 | 下一步需要的关联证据 |
| --- | --- | --- |
| 验收客户端 ↔ Firefox 父进程 | socket/connect、read/write、send/recv、poll；TCP stream | 命令 ID、两端 PID/TID、连接身份、发送完成、帧长度与已接收字节数 |
| Firefox 父进程 ↔ 页面/辅助进程 | socketpair、fcntl、sendmsg/recvmsg、SCM_RIGHTS；Unix stream 与控制消息处理 | 两端 fd 对象身份、非阻塞标志、复制字节数、控制消息类型/数量、错误 |
| I/O 就绪 ↔ 事件循环 | epoll_ctl/epoll_pwait、ppoll；Pollee → ReadySet → Waker | 注册的 fd/事件、notify、等待开始、唤醒、恢复执行、返回事件 |
| 工作线程 ↔ 条件变量 | futex WAIT/WAKE、信号和超时；FutexKey/Waiter | 等待键身份、等待者、唤醒者、位集、超时类型和返回原因 |
| 内容数据 ↔ 虚拟内存 | mmap/mprotect/munmap、用户复制、缺页、文件页读取 | 当前 syscall、缺页阶段、访问权限、映射类型、处理完成或失败 |
| 父进程 ↔ 子进程生命周期 | clone/exec、exit、SIGCHLD、wait4/waitid、pipe | 子进程角色和身份、创建者/父进程、退出原因、哪个等待者回收了它 |

Unix IPC 的 socketpair、非阻塞设置、sendmsg/recvmsg 和 SCM_RIGHTS 关系可在
[Mozilla ESR 140 IPC 源码](https://raw.githubusercontent.com/mozilla-firefox/firefox/esr140/ipc/chromium/src/chrome/common/ipc_channel_posix.cc)中核对。
该源码关系决定应观测哪些调用，但不能证明本次具体 Firefox 进程已经执行了每条路径。
内核的等待链可从 [`Pollable::wait_events`](../../../kernel/src/process/signal/poll.rs)、
[`EpollFile`](../../../kernel/src/events/epoll/file.rs) 和
[`futex_wait_bitset`](../../../kernel/src/process/posix_thread/futex.rs) 继续追踪。

## 待确认的观测范围

推荐扩展现有诊断设施，而不是只打开现有聚合统计或直接全量 strace。
现有统计改动小但缺少阻塞身份；全量 ptrace 会改变调度和 syscall 时序。
第一批观测应默认关闭，仅跟踪 Firefox、相关子进程和验收客户端，限制事件量并报告丢失。
目标包括进入/返回配对、尚未返回调用的有限快照、关键 IPC 和进程退出边界。
错误应带 PID/TID、对象身份、返回值和阶段，不能把正常 `EAGAIN` 都当作故障。

主机使用单调时间记录接收时刻，guest 的时间只在同一时钟域内比较；
不同 CPU 的事件需要序列号或明确的关联标识，不能仅靠日志排列猜因果。
关键线程状态应在安全上下文导出；不在自旋锁内分配、等待或大量串口打印。
按项目规范使用 OSTD 日志设施，不继续扩大现有 `early_println!` 诊断用法。
诊断开启/关闭需要对照测量，以识别观测造成的时序变化。
输出先保存到主机；不能依赖结束时一次 guest 磁盘刷写，也不依赖卡住的 Marionette 连接导出自己。

## 建议的实验顺序与可区分结果

以下是实验问题清单，不是已经实现或执行的测试。

| 实验 | 最小变量与观测 | 什么结果会改变判断 |
| --- | --- | --- |
| Socket 冷缓冲区与短 I/O | 分别测试 TCP、Unix socket；read/write、readv/writev、sendmsg/recvmsg；逐项比较冷/热页、未使用的不可访问尾部、EOF/非阻塞情况 | 最小程序出现错误优先级、复制范围或缺页问题，可先修内核语义而不启动 Firefox |
| Unix IPC 事件握手 | socketpair → epoll 注册 → 发送带一个有效 fd 的消息 → 接收确认；记录 MSG_CTRUNC 和实际字节数 | 有数据但未通知、通知但未恢复、恢复但复制失败，可分层定位 |
| 子进程生命周期 | 创建/退出与 wait4/waitid、SIGCHLD；分别控制同进程不同等待线程、namespace 和先后回收 | 判断 ECHILD 是合法重复等待、身份错误还是生命周期实现问题 |
| 同一 rootfs 的 Firefox 对照 | 当前/历史内核各保留跳转前、跳转后、无进展时三个检查点；其余设置固定 | 确定首个不同的进程状态、IPC 进展或未返回调用，再选择相关内核单变量实验 |
| 真机有界诊断 | 复用已验证产物，在一次启动中先做相关微测试，再收集 Firefox 的同类检查点 | 区分 QEMU 共性问题与板级特有问题；保留自动恢复、安全时限和全部失败证据 |

微测试必须有 Linux 参照或明确的 ABI 依据，并记录架构差异；不能以本实现的行为反过来定义正确性。
独立微测试可以共享一次启动，但应分进程、清理资源并记录顺序；
浏览器实验的缓存状态和系统负载需要保持可比，必要时使用单独基线启动。
仍未进展时导出有限证据并退出，不以增加等待时限代替分析。
真机操作前写明假设、相反结果的含义、产物哈希、停止条件、回收流程和预计耗时。
如果 QEMU 已能重现相同软件阻塞，下一次板上实验需要额外的板级问题或验证目的。

## 下一里程碑

先让一次失败能回答：命令是否到达、哪个进程/线程未进展、在等什么对象、
该对象是否被通知、是否已有子进程退出。
在证据达到这一程度后，再选择一个内核改动和对应回归测试。
本轮不宣称完成这个里程碑，不合并 PR，也不放宽原有三轮可信输入与 HDMI 验收要求。
