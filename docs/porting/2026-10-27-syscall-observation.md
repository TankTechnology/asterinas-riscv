# 补观测能力：真实缺页计数 + /proc/<pid>/syscall（+ PTRACE_ATTACH 分析）

日期：2026-10-26/27　内核：`69a14ac17a479774`（A+B）　前序：`f3299cd5`
镜像：`toolchain4`（矩阵）与 `ff-lag`（Firefox）

## 结论摘要

| 项 | 改前 | 改后 |
|---|---|---|
| `/proc/<pid>/stat` minflt/majflt | **恒为 0**（两次实测：`minflt=0 majflt=0`） | 真实计数：`683/32` → 触碰 64 MiB 后 **`17068/32`**，再触碰不增长 |
| `/proc/<pid>/syscall` | **不存在**（ENOENT） | 可读；100 次采样得到 **6 种值**（`0,3,5,8,257,running`） |
| `PTRACE_ATTACH` | EIO | 未改；**已定位原因**（请求号不在枚举里，见 §3） |

7 域矩阵在 A+B 内核上保持全 ok、panic 0（见 §4）。

## 1. A：真实 minor/major fault 计数

### 改动

- `kernel/core/src/thread/mod.rs`：`Thread` 新增
  `min_flt: AtomicU64`、`maj_flt: AtomicU64`，方法
  `record_page_fault(major)` / `min_flt()` / `maj_flt()`。
- `kernel/core/src/vm/vmar/vm_mapping.rs`：`handle_single_page_fault`
  在“需要 `commit_on`（I/O）”时记 `is_major = true`，最终解析成功时按
  `is_major` 记一次 major 或 minor（避免重试路径重复计数）。
- `kernel/core/src/fs/fs_impls/procfs/pid/task/stat.rs`：字段 10/11/12
  输出线程的 `min_flt`/`cmin_flt`/`maj_flt`；`cmin_flt`/`cmaj_flt`
  仍是 0（子进程累计未实现，已在代码注释注明）。

### 验收（实测）

同一探针（Python 读 `/proc/self/stat` + 触碰 64 MiB 匿名内存）：

| 时刻 | minflt | majflt |
|---|---|---|
| 进程启动后（未触碰） | 683 | 32 |
| 触碰 64 MiB 每一页后 | **17,068** | 32 |
| 再触碰同一区间 | 17,068（不增长，页已驻留） | 32 |

- 增量 **+16,385** ≈ 64 MiB / 4 KiB = 16,384 页；
- `majflt=32` 来自解释器/动态库的首次 I/O，之后不再增长；
- **改前**同一内核路径的对照是上一轮 sysattr 里的
  `minflt=0 majflt=0`（字段存在但没实现）。

### 已知限制

- `cminflt/cmajflt`（子进程累计）仍为 0；Linux 语义需要 RUSAGE_CHILDREN
  式累计，本轮不做。
- 多线程进程的 `/proc/<pid>/stat` 只报主线程计数（Linux 会汇总）；
  `/proc/<pid>/task/<tid>/stat` 逐线程准确。

## 2. B：`/proc/<pid>/syscall`

### 改动

- `Thread` 新增 `syscall_state: SpinLock<SyscallState>`：`active`、
  `nr`、`args[6]`、`sp`、`pc`；入口/出口方法。
- `kernel/core/src/syscall/mod.rs::handle_syscall`：进入时记录
  `(nr, args, rsp, rip)`，`syscall_dispatch` 返回后清 `active`。
- 新增 `kernel/core/src/fs/fs_impls/procfs/pid/task/syscall.rs`：
  读取返回
  `<nr> 0x<a0> ... 0x<a5> 0x<sp> 0x<pc>\n`，不在 syscall 中返回
  `running\n`；非阻塞采样。已注册进 pid/task 目录表。

### 验收（实测）

```
cat /proc/self/syscall
0 0x3 0x7ffffed1c000 0x40000 0x0 0x0 0x0 0x7ffffff79a30 0x7ffffee563a7
```

对一个忙循环子进程采样 100 次（每次约 20 ms）：

```
syscall_samples 100 unique 6 ['0', '257', '3', '5', '8', 'running']
sample 257 0xffffffffffffff9c ...   # openat
sample 5   0x3 ...                 # fstat
sample running
```

`0=read, 3=close, 5=fstat, 8=lseek, 257=openat`，以及 `running`
（用户态执行中）——满足“≥2 种 syscall 号”的验收。

### 用途

Firefox sys 归因现在可以用一个 100 ms 采样器统计
`/proc/<pid>/syscall` 的第一列，得到 syscall 分布，不再依赖 strace。
（采样是“瞬时在做什么”，不是精确计数；对高频 syscall 的占比是近似。）

## 3. C：`PTRACE_ATTACH` 为什么 EIO（已定位，未实现）

- `kernel/core/src/syscall/ptrace.rs:26-27`：
  `PtraceRequest::try_from(request).map_err(|_| Error::with_message(Errno::EIO, "invalid ptrace request"))?`
- `PtraceRequest` 枚举（同文件 ~:196-237）里 **没有 `PTRACE_ATTACH = 16`**，
  它和 `PTRACE_DETACH = 17` 一起被注释掉（`// TODO: Support other operations.`）。
- 因此 `PTRACE_ATTACH` 在 `try_from` 处就失败并返回 **EIO**，根本没有进入
  attach 逻辑——这解释了 strace `attach: ptrace(PTRACE_ATTACH, 134):
  Input/output error`。
- 现有基础：`do_ptrace_attach()`、`PosixThread::attach_to()`、
  `detach_tracer()`、`PTRACE_SYSCALL`、`GETREGS/SETREGS`、`SETOPTIONS`
  都已存在，缺的是：
  1. 枚举加 `ATTACH/DETACH`，按 tid 查 tracee 线程并做权限检查；
  2. ATTACH 后向 tracee **投递 SIGSTOP** 并让 tracer 的 `wait4` 看到
     ptrace-stop（Linux 语义）；
  3. DETACH 清理（`detach_tracer()` 已有）。
- 估计工作量 > 30 分钟且涉及信号/等待语义；本轮按约定只交分析。
  strace 还需要 syscall-stop 的稳定实现（我们的 `PTRACE_SYSCALL` 已有，
  但未在 attach 场景验证）。

## 4. 回归

7 域矩阵（toolchain4 镜像、A+B 内核）：

```
APT/DESKTOP/APP/CLIPBOARD/AUDIO/USB/SUSPEND = ok
INPUT_METHOD（额外域）= fail engine--no-text
panic = 0, allocation failed = 0
```

## 5. 复现

```sh
# A
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/proc_fault_probe.sh make test_x86_daily_use …

# B
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/proc_syscall_probe.sh make test_x86_daily_use …

# 7 域
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/daily_use_checklist_regress.sh make test_x86_daily_use …
```

## 6. 证据

- `evidence/2026-10-27-observation/proc-fault.txt`（A：683→17068）
- `…/proc-syscall.txt`（B：6 种采样值）
- `…/matrix.txt`（7 域 + panic 0）
