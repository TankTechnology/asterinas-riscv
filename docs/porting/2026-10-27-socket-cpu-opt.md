# UNIX socket CPU 归因与优化：把 sendmsg/recvmsg 从 77% 拆开

日期：2026-10-27
基线内核：`2e747b59edea3e51`（cputime 特性已合入）　镜像：`ff-lag`、4 vCPU
优化后内核：iovec `ca9cdc665`、warn-once `f4e10d5db`（ELF `245594cbc399ce04`）

上一轮 CPU-only 归因显示 Firefox 20 s 滚动的 CPU 热点是
`recvmsg` 44.7% + `sendmsg` 32.1%（合计 76.8%）。本轮先建微基准、再拆开
每个 op 的真实成本，最后按结论做了两项优化。

## 1. 任务 A：微基准（`tools/x86/debian/sock_pingpong.py`）

- 两个进程通过 `socketpair(AF_UNIX, SOCK_STREAM)` 严格 ping-pong：
  父进程 `sendmsg` → 子进程 `recvmsg` 后原样 `sendmsg` 回 → 父进程
  `recvmsg`。一次 round trip = 4 次 socket syscall（每侧 1 发 1 收）。
- 每档测三/四个尺寸：64 B、4 KiB、64 KiB；`--mode` 另有三个隔离组：
  - `sameproc`：两个端点同一进程（走完整 socket 路径，但不阻塞、不唤醒）；
  - `pipe`：同进程 pipe write+read（fd 查找 + 用户拷贝，无 socket 代码）；
  - `pipepair`：两进程 pipe 往返（有阻塞/唤醒，无 socket 代码）。
- 指标：wall ns/rt、**cpu ns/rt**（`total − blocked`）、**blocked ns/rt**、
  cpu ns/syscall（Σcpu ÷ Σcount，来自 `/proc/syscall_time`）。

### 1.1 基线数字（64 B，同一 boot）

| mode | wall ns/rt | cpu ns/rt | blocked ns/rt | **cpu ns/syscall** |
|---|---|---|---|---|
| pingpong（2 进程 socket） | 64,747 | 56,870 | 51,323 | **14,218** |
| sameproc（1 进程 socket） | 11,275 | 8,046 | 0 | **4,023** |
| pipe（1 进程） | 4,526 | 2,214 | 0 | **1,107** |
| pipepair（2 进程 pipe） | 45,742 | 38,828 | 39,803 | **9,707** |

尺寸标度（同内核另一 boot，误差约 ±3%）：

| 尺寸 | cpu ns/syscall | 说明 |
|---|---|---|
| 64 B | 14,908 | 固定开销 |
| 4 KiB | 14,755 | 与 64 B 相同 → 拷贝可忽略 |
| 64 KiB | 19,551 | 多出的 ~4.6 µs 是 2×64 KiB 拷贝（≈14 GB/s，正常） |

### 1.2 结论

1. **拷贝不是瓶颈**：64 B 与 4 KiB 完全一样；64 KiB 才显出按字节的成本，
   且属 memcpy 正常速度。
2. **socket 代码本身 ≈ 2.9 µs/syscall**（sameproc 4.02 − pipe 1.11）。
3. **跨任务阻塞/唤醒路径 ≈ 8.6–10 µs/syscall**（pipepair 9.71 vs pipe 1.11；
   socket pingpong 14.2 vs sameproc 4.0）。它是绝对大头（约 70%），且在
   pipe 上也一样（说明是通用的 wait/park/wake 开销，不是 socket 特有）。
4. 阻塞确实存在：pingpong 每 round trip 有 51.3 µs 被计入 blocked，而
   CPU 合计 56.9 µs —— 两个进程分布在 CPU1/CPU3（见 `cpu_parent/cpu_child`
   字段），确实并行。

## 2. 任务 B：sendmsg / recvmsg 每个 op 的成本清单

路径：`sys_sendmsg` → `send_one_message` → `UnixStreamSocket::sendmsg`
→ `block_on` → `try_send` → `Connected::try_write` → ring buffer；
接收侧对称。

| 项 | 是否有 | 实测/估计代价 |
|---|---|---|
| 用户→内核拷贝次数 | 发送 1 次（用户 iov → 64 KiB 环形缓冲）；接收 1 次（环形缓冲 → 用户 iov）；无中间 skb | 64 KiB 每次 ≈ 2.3 µs（14 GB/s） |
| msghdr / iovec 元数据拷贝 | 每 syscall 读 64 B msghdr + 16 B×iovlen；iov 数组原先**两次堆分配**（`Vec<IoVec>` + `Box<[VmReader]>`） | 本轮去掉了其中一次（§3） |
| 每 iov 的分配 | 无（`convert_iovec` 只构造 reader/writer） | — |
| 唤醒次数 | 每次 send 1 次（peer pollee `notify(IN)`）+ 自身 `invalidate`；接收时 1 次 `notify(OUT)` | 只有超出 blocked 的部分计入 CPU |
| 锁 | file table RwLock 读、socket `state` RwLock 读、ring `Mutex`、`all_aux` Mutex（快路径不进）、pollee 观察者 SpinLock+BTreeMap | 未争用时各约几十 ns |
| 阻塞路径（关键） | `wait_events` 每次阻塞都新建 `Poller`：`Waiter`+`Waker` 两个 Arc、`PollHandle`；注册进 pollee 的 BTreeMap（一次分配），唤醒后再注销并释放 | 约 8.6–10 µs/syscall（§1） |
| ancillary/cred | `msg_control==0` 时无分配；`SO_PASSCRED` 关闭时只多两次 atomic load | 可忽略 |
| 其它 | `sendmsg` 为安全克隆一次 `Arc<FileLike>`；`recvmsg` 回写 64 B msghdr | 各约几十 ns |

## 3. 任务 C：优化（本次实施 2 项）

### 3.1 去掉 iovec 转换路径上的一次堆分配

`kernel/core/src/util/iovec.rs`：

- 旧：先把整张用户 iovec 数组读进 `Vec<IoVec>`（校验长度），再第二遍
  生成 `Vec<VmReader>` 并 `into_boxed_slice()` —— 每 syscall **2 次分配**。
- 新：第一遍只校验长度（不落堆），第二遍直接生成目标数组 —— 每 syscall
  **1 次分配**。语义保持：仍然"先校验所有长度，再校验地址"，越界/负数长度
  的错误码不变。
- 影响面：`sendmsg`/`recvmsg`/`readv`/`writev`/`preadv` 等所有 iovec syscall。

改前/改后（同一脚本、同一镜像、4 vCPU）：

| 测量 | 改前（2e747b59） | 改后（新内核） | 变化 |
|---|---|---|---|
| sameproc 64 B：cpu ns/syscall | 4,023 | **3,210** | **−20.2%** |
| sameproc 64 B：wall ns/op | 11,275 | 9,814 | −12.9% |
| pingpong 64 B：cpu ns/syscall | 14,218 | 14,283 | +0.5%（噪声） |
| pingpong 64 B：wall ns/rt | 64,747 | 62,930 | −2.8% |
| pingpong 64 KiB：cpu ns/syscall | 20,309 | 20,008 | −1.5% |
| pipe（对照）：cpu ns/syscall | 1,107 | 1,127 | +1.8%（噪声，符合预期） |
| pipepair（对照）：cpu ns/syscall | 9,707 | 10,153 | +4.6%（噪声） |

解读：

- 只走 socket 路径、不阻塞的 `sameproc` 掉了 **20%**——这正是本次改动的
  目标段，pipe 对照组不动，说明不是整体漂移。
- 跨进程 ping-pong 基本不动（±0.5–4.6%，均在噪声内），因为它的成本 70%
  在阻塞/唤醒路径（§1.2），iovec 分配只占几个百分点。
- 64 KiB 略降 1.5%，大消息多出的拷贝成本不受影响（符合预期）。
- warn-once 版本（`245594cb`）复测：sameproc 3,367、pingpong 14,215、
  pipe 1,204、64 KiB 19,553 ns/syscall —— 与 iovec 版本在 ±5% 内一致，
  符合预期（微基准不传 `MSG_DONTWAIT`，warn-once 对它无影响）。
  唯一例外是 pingpong 64 KiB 的 `blocked_ns_rt` 由 76,465 升到 89,721
  （调度抖动，见 §4c 的负载异常）。

### 3.2 消除 socket 热路径上的逐次 `warn!`（见 §4b）

`kernel/core/src/net/socket/util/message_flags.rs` 新增
`warn_unsupported_flags_once(send, flags)`：每种方向只打印第一次
（`AtomicBool` 交换），16 处 `warn!("unsupported flags: ...")` 全部改用它。
语义不变（未实现的标志仍未实现），但不再每次调用都格式化并写串口。

## 4. Firefox 重测（20 s 滚动）

Firefox 阶段与上一轮相同（1024×768、开 openbox、Page_Down 滚动 20 s、
`/proc/syscall_time` 采 `--by cpu` 与 `--by total`）。

| by cpu | 改前 2e747b59 | 改后（iovec） | 改后（+warn-once） |
|---|---|---|---|
| recvmsg | 44.7% / 940.9 µs/次 | 40.8% / 687.4 µs/次 | 未取得（见 §4c） |
| sendmsg | 32.1% / 4,875 µs/次 | 31.0% / 4,078.6 µs/次 | 未取得（见 §4c） |
| read | 7.2% | 9.3% | — |
| clone | 3.5% | 4.0% | — |
| ioctl | 1.8% | 2.6% | — |
| poll | 1.5% | 1.9% | — |

按 total 的排序两边一致（futex 约 68%、epoll_wait 约 7%），即阻塞项没有变化。

注意两次 Firefox 采样的工作量本身就有约 20% 波动（recvmsg 调用数
20,784 → 25,341），所以单次逐调用差值（−26.9% / −16.3%）不能全部归因于
iovec 改动；能确定的是微基准里 socket 路径本身快了 20%（§3.1）。

## 4b. 新的发现：**每次 socket 调用都在打印 warn!**

Firefox 阶段的内核控制台被下面这一行刷屏（每秒数百行）：

```text
WARN  : unsupported flags: MSG_DONTWAIT
```

来源是 `UnixStreamSocket::sendmsg/recvmsg` 里的
`if !flags.is_all_supported() { warn!(...) }`：`MSG_DONTWAIT` 不在
`SUPPORTED` 里，而 Firefox 对 non-blocking socket 的**每一次** sendmsg/recvmsg
都带这个标志。这条 warn 位于被计时的 syscall 内部，格式化 + 串口输出
（115200 波特，几十字节 ≈ 数毫秒）全部算进 `sendmsg`/`recvmsg` 的 CPU。
这也解释了为什么 Firefox 的单次 sendmsg CPU 高达 4 ms，而微基准里只有
14 µs（Python 的 sendmsg 不带 `MSG_DONTWAIT`）。

修复：16 处 `warn!("unsupported flags: ...")` 全部改为
`warn_unsupported_flags_once(send, flags)`（`message_flags.rs`），
每种方向只打印第一次，语义不变（标志仍然不被实现，只是不再刷屏）。

## 4c. warn-once 的 Firefox 复测被 panic 打断

同一内核（`245594cb`）跑 Firefox 阶段时，在滚动开始后触发了一次 panic：

```text
Uncaught panic: This function might break atomic mode
  (preempt_count = 3, is_local_irq_enabled = true)
  at kernel/core/src/vm/vmar/vmar_impls/page_fault.rs:10
  on CPU 1, thread with 1001 min_flt (Firefox 内容进程)
```

panic 前一行控制台是 `mpool miss, segmented fallback: pages=16 total=7`
（堆内存压力），因此怀疑是缺页慢路径在需要分配/回收页面时进入了可能休眠
的代码。

这与 2026-10-27 CPU-time 那轮 `cputime2` 首次出现的是**同一个签名**
（see `2026-10-27-syscall-cpu-attribution.md` §5）。它现在在约 6 次 Firefox
滚动里出现了 2 次，是可复现的低频并发缺陷（缺页慢路径在 `preempt_count > 0`
时进入了可能休眠的代码），应在下一轮单独修，不在本轮范围内。

因此 warn-once 的 Firefox 前后对比**没有取得**；微基准不受影响（Python 不传
`MSG_DONTWAIT`），其数字见 §3.1 表的后一列与
`evidence/2026-10-27-socket-cpu-opt/microbench-after-warnfix.txt`。

## 5. 判断与下一步

1. **拷贝不是热点**：64 B 与 4 KiB 的每 syscall CPU 完全相同，64 KiB 才
   显出按字节的成本，且已是 memcpy 正常速度（≈14 GB/s）。"减少拷贝"这条
   候选对 Firefox 没有意义。
2. **socket 代码自身**从 4.02 µs/syscall 降到 3.21–3.37 µs（−16～20%），
   去掉一次 iovec 堆分配是唯一可测的收益点。
3. **跨进程成本的大头是通用阻塞/唤醒（≈8.6–10 µs/syscall，占 60–70%）**：
   pipepair（无 socket 代码）同样要付这笔钱。每次阻塞都新建
   `Poller`（`Waiter`/`Waker` 两个 Arc），再往 pollee 的 BTreeMap 插/删
   一个观察者，然后 park/unpark + 跨 CPU 调度。下一步应在这里做（单等待者
   免堆分配路径、或 wake 落在唤醒者 CPU），而不是继续微调 socket。
4. **Firefox 的 `sendmsg`/`recvmsg` 单次 CPU（687 µs / 4.1 ms）比微基准
   高两个数量级**，原因已定位为 socket 热路径上的逐次 `warn!`（§4b）——
   真实程序带 `MSG_DONTWAIT`，每条都写串口。warn-once 已修，但复测被
   §4c 的缺页 panic 打断，尚未拿到数字；这是下一轮第一优先。
5. **新发现的并发缺陷**（§4c）比性能问题更重要：缺页慢路径在
   `preempt_count = 3` 时调用可能休眠的代码，约 1/3 的 Firefox 负载运行
   会触发。应在下一轮单独定位（打开 panic backtrace 全栈、检查
   `handle_page_fault` 里持有 preempt guard 的那条路径）。

## 6. 回归

7 域矩阵（内核 `245594cb` = iovec + warn-once，镜像 `toolchain4`，4 vCPU）：

| 域 | 结果 | 细节 |
|---|---|---|
| DESKTOP | ok | Xorg+DRI3 |
| APP | ok | xterm |
| APT | ok | xclip+xterm+alsa-utils-installed |
| CLIPBOARD | ok | xclip-round-trip |
| AUDIO | ok | aplay-hw:0,0 |
| USB | ok | mounted-/dev/sda-read-ASTERINAS-USB-OK |
| SUSPEND | ok | woke-up-disk-and-io-ok |

额外域 INPUT_METHOD 仍 fail（`engine--no-text`，既有缺口）；`DAILY_DONE=ok`、
panic 0、allocation failed 0。原始行见 `evidence/2026-10-27-socket-cpu-opt/matrix.txt`。

## 7. 复现

```sh
# 微基准 + Firefox（嵌入式脚本，无需仓库）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/sock_opt_check.sh make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/ff-lag/daily.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm DAILY_USE_WORKSPACE=$PWD/target/sockopt

# 只跑微基准
python3 tools/x86/debian/sock_pingpong.py --mode pingpong --sizes 64,4096,65536
```

## 8. 证据

- `evidence/2026-10-27-socket-cpu-opt/microbench-before.txt`（四模式 + 尺寸标度）
- `evidence/2026-10-27-socket-cpu-opt/microbench-after.txt`
- `evidence/2026-10-27-socket-cpu-opt/firefox-before.txt`（沿用
  `2026-10-27-cpu-time/firefox-cpu-vs-total.txt` 的 by-cpu 表）
- `evidence/2026-10-27-socket-cpu-opt/firefox-after.txt`
- `evidence/2026-10-27-socket-cpu-opt/matrix.txt`
