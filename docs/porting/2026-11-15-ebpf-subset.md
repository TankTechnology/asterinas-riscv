# eBPF：一个能跑真实程序的子集（2026-11-15）

内核侧在 `kernel/src/bpf/`，用户态在 `tools/riscv/bpf/bpftool_lite.py`。
目标是"够用"，不是 Linux 兼容：验证器保守、解释执行、只挂在自己的 trace 事件上。

## 1. 能力面

| 项 | 状态 |
| --- | --- |
| `bpf(2)` 命令 | `MAP_CREATE` / `MAP_LOOKUP_ELEM` / `MAP_UPDATE_ELEM` / `MAP_DELETE_ELEM` / `MAP_GET_NEXT_KEY` / `PROG_LOAD` / `BPF_RAW_TRACEPOINT_OPEN` |
| map 类型 | `ARRAY`(2)、`HASH`(1)、`PERCPU_ARRAY`(6)、`PERCPU_HASH`(5)、`RINGBUF`(27)；值预分配，锁内零分配 |
| 程序类型 | `BPF_PROG_TYPE_TRACE_EVENT`(0x80000001)、`BPF_PROG_TYPE_SOCKET_FILTER`(1) |
| 挂点 | 内核自己的 trace 事件（`subsystem:event`），关闭 link fd 即解除；socket 过滤器走 `SO_ATTACH_BPF`(50) |
| 用户态环形缓冲 | `RINGBUF` 用 `Vmo`，fd 直接 `mmap` 共享；程序用 `bpf_ringbuf_output` 追加 |
| helper | 1 `map_lookup_elem`、2 `map_update_elem`、3 `map_delete_elem`、5 `ktime_get_ns`、6 `trace_printk`、8 `get_smp_processor_id`、14 `get_current_pid_tgid`、130 `bpf_ringbuf_output`、1000 kallsyms 查询（内核自定义） |
| 指令 | ALU64/ALU32（MOV/ADD/SUB/MUL/DIV/MOD/AND/OR/XOR/LSH/RSH/ARSH/NEG）、JMP/JMP32 全部条件跳转、CALL、EXIT、`LDX/STX/ST` 的 B/H/W/DW、`LD_IMM64`（含 map fd 伪指令） |
| 权限 | 仅 root |
| 未实现 | JIT、kprobe/BTF、`bpf_ringbuf_reserve/submit`（只做了 output） |

## 2. 验证器（保守、静态）

- 无环：所有跳转必须**向前**，且必须落在指令边界上（不能跳进 `LD_IMM64` 的第二槽）。
- 程序必须以 `exit` 结尾，长度 ≤ 4096 条。
- 寄存器有类型：scalar / ctx / stack / map / map-value / uninit；mem 访问按来源检查边界。
- 栈读必须先写（512 位初始化位图，分支汇合取交集）。
- 指针算术只允许 `add/sub` 常量（ctx 指针向上、栈指针在 `[-512, 0]`）；map-value 指针不可加减。
- helper 参数类型检查（map 指针、key 必须可读、flags 必须 scalar）。
- 汇合点按**活跃变量**合并：只有后续还会读的寄存器要求一致；指针与非指针汇合时降级为 scalar（解释器会在访存时拒绝，安全由运行期兜底）。

解释器在运行期**再查一遍**所有边界，所以验证器的"不确定"不会变成越界：最多让程序提前以错误结束。

## 3. 事件上下文（48 字节）

```
0  u64 timestamp_ns     8  u32 pid        12 u32 tid
16 u32 event            20 u32 cpu
24 u64 arg0             32 u64 arg1       40 u64 arg2
```

`arg0..2` 是事件自己的参数：syscall 号与其前两个参数、块请求的扇区数/类型/起始扇区、
缺页地址/是否写、wakeup 的目标 pid/tid、切换的下一个 task、中断号、采样 pc 等。
采样 profiler 和函数跟踪也会跑挂在上面的程序，并传 `Jiffies` 时间戳（不能读时钟）。

## 4. 用户态工具

`tools/riscv/bpf/bpftool_lite.py`（纯 ctypes，板上 Python 3 可直接跑）：

```sh
python3 bpftool_lite.py verifier          # 4 个正反例：好的加载、环/未初始化/缺 exit 被拒
python3 bpftool_lite.py count    --event syscall:sys_enter --seconds 5
python3 bpftool_lite.py syscalls --event syscall:sys_enter --seconds 5
python3 bpftool_lite.py blocks   --event block:block_rq_issue --seconds 5
python3 bpftool_lite.py load              # 加载后读 /proc/trace/bpf
```

带一个标签汇编器（只允许前向跳转，所以标签让偏移可读），
两个分析程序：按 (pid, syscall 号) 计数、按 (pid, 大小桶, 读写) 计数。
键用 `pid << 32 | value`（块事件用 `pid << 40 | kind << 32 | bucket`）。

## 4b. P2 的三块新地面

**per-CPU map**：元素按 CPU 分槽（`PERCPU_*`），程序只读写自己那颗 CPU 的槽；
解释器整段运行期间关抢占（`disable_preempt`），所以一次运行内 CPU 不会变。
用户态 lookup 返回所有 CPU 的值拼起来（每 CPU 按 8 字节对齐）。
不属于 per-CPU 的 map 对任何 CPU 都给同一个值——线上这条最早写反了：
非 per-CPU map 在 CPU≠0 上报 EINVAL，`syscalls` 直方图在 4 核板上 2/3 的运行失败，
ktest（只跑在 CPU 0）没抓到时硬件测出来了。

**RINGBUF**：一个 `Vmo`，第一页是头（consumer/producer/容量），后面是数据区；
记录是 `{u32 len, u32 保留, payload, 补齐}`，跨界时用一个 `0xFFFFFFFF` 的标记头跳到 0。
创建时把整个 Vmo 预写零，之后程序追加记录不会缺页、不会在锁里分配。
工具侧就是普通 `mmap` + 轮询 producer（没有事件通知）。

**socket filter**：`SO_ATTACH_BPF` 的载荷是程序 fd（只有 syscall 层能解析），
程序的上下文**就是报文本身**：加载时按 `SOCKET_CTX_MAX`(2048) 校验偏移，运行期超出报文
长度的部分补零（短包是数据而不是错误）。返回值 >0 表示放行。目前挂在经典 cBPF 过滤器
所在的 netlink 收包路径上（内核自己的 uevent 投递），**这条路径在本内核没有生产者**，
所以过滤语义靠 ktest（`run_on_packet`）覆盖，板上只验证 attach/detach/错误码。

## 5. 板上实测

**每进程 syscall 直方图**（桌面空闲、5 秒）：

```
     calls    pid  syscall              comm
      3188    194  futex                firefox-esr
      1986    194  recvmsg              firefox-esr
      1832     60  statx                node-MainThread
      1533    194  ppoll                firefox-esr
```

`node-MainThread` 是板上的 `dsh web`（我们自己的 harness），空闲时仍有 ~180 次
statx/读 每秒——测桌面性能时它是背景噪声源。

**P2 的四条命令**（同一块板、同一版内核）：

```
verifier  3 个反例全部拒绝、正例加载
sockfilter  attach/detach/坏 fd/非程序 fd 四项全对
percpu     4 颗 CPU 各自的计数（约 1/4 各一份）
ringbuf    2 s 抓 12157 条记录、0 丢弃（mmap 共享页）
ksym       "<aster_mmc::arch::MmioHost>::wait_sdma" -> 0xffffffff80595900
```

**块请求直方图**（16 MiB 写，同一文件、同一块大小）：

| 场景 | 吞吐 | 设备请求 |
| --- | --- | --- |
| 新文件，4 KiB 写 | **1.2 MB/s** | 64 次写 → **64 个** 4 KiB 请求 |
| 新文件，64 KiB 写 | 11.5 MB/s | 25 个 32–128 KiB 请求 |
| 已分配块，4 KiB 写 + 每次 fsync | **15.1 MB/s** | — |
| tmpfs，4 KiB 写 | 137 MB/s | 无 |

trace 记录（`sector=... nr_sectors=8 kind=1`）显示新文件每个 4 KiB 写都立刻产生一次
**数据页**的同步设备写，间隔 ~1.7 ms；采样 profiler 同期显示一个 CPU 几乎 100% 在
`MmioHost::wait_sdma`，其余空闲——瓶颈是**每请求延迟 × 请求数**，不是带宽。

结论：ext2 在**为新块分配**时把该页直接写穿到设备（已分配块则留在 page cache）。
要提升小写吞吐，下一刀应落在"分配时不要同步写回，交给 writeback 成批提交"，
而不是 MMC 驱动本身。

## 6. 坑

- 哈希表插入必须**沿 key 自己的探测链**找空槽；用"全表第一个空槽"会让查找在到达条目
  之前就撞上空槽而失败。ktest 当时用满表（查找会扫描全表）掩盖了这个 bug。
- ktest 里传 `Duration::ZERO` 时间戳；trace 事件里不能用 `read_monotonic_time()`
  的路径（采样/函数跟踪）必须用 `Jiffies`。
- `bpf_attr` 的布局按 Linux 前缀读取；工具侧寄存器常量写错（`R10=7`）会让验证器报
  EINVAL，而错误信息不会传到用户态——先跑 `verifier` 子命令排除编码问题。
- **同一个线程的 file table 不能嵌套借用**：`SO_ATTACH_BPF` 里先借到 socket、又借一次
  解析程序 fd，直接 panic，整机被硬件看门狗复位。取 socket 时 `.into_owned()` 再退出
  借用块。
- `map_update` 的 r3 是**值指针**，不是长度；工具里写错过一次，ktest 一跑就露馅。
- socket filter 的上下文是报文，而解释器按小端加载：比较以太网类型这类大端字段时，
  要么在字节层面比较，要么把常量按小端写。
