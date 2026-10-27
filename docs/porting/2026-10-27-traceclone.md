# `PTRACE_O_TRACECLONE`：让 `strace -f` 能跟多线程程序

日期：2026-10-27
内核基线：`aff0f522a`
任务书：`/tmp/tp/TRACECLONE.md`

## 0. 结论

- 被 trace 的进程在 `PTRACE_O_TRACECLONE`（以及 `TRACEFORK`/`TRACEVFORK`）打开后，
  `clone`/`clone3` **不再返回 `EOPNOTSUPP`**；新建的子线程/子进程**自动挂到同一个
  tracer**，并在**执行第一条用户指令之前**进入 ptrace-stop（`SIGSTOP`），
  tracer 的 `wait4` 能收到该 tid 的 stop。
- `strace -f` 可以跟踪多线程程序：**能看到 `clone3` 事件与子 tid**，fork 出的子进程
  也被跟踪（原始输出见 §3.2）。
- **与 Linux 的差异（如实记录）**：
  - **未实现父进程的 `PTRACE_EVENT_CLONE` 事件停**：tracer 的 `wait4` 不会对父进程
    返回 `SIGTRAP|0x80`、`status>>16` 也不是事件码；`PTRACE_GETEVENTMSG` 拿不到新 tid。
    strace 依据 **clone 系统调用本身**（返回值即新 tid）与子进程的初始 `SIGSTOP` stop
    来认识新 tracee。
  - 不设置该 flag 时行为完全不变（子进程不被自动 trace）。
  - `SETOPTIONS(TRACECLONE)` 在改前**本来就返回 0**（选项位一直是被接受的），
    真正失败的是被 trace 的 clone —— 任务书的 A2 假设与实测不符，以实测为准。

## 1. 任务 A：失败用例（改前，基线 ELF）

可复跑脚本：`tools/x86/proc/traceclone_check.sh` + `traceclone_check.c`。

```
TC compile=ok
SETOPTIONS traceclone rc=0 errno=0 (Success)
CLONE pthread_create_rc=95 new_tracee_stops=0 child_exited=1 read=4
RESULT traceclone FAIL
TC harness_exit=1
TC stracef_python_rc=1 clones=1 out=RuntimeError: can't start new thread
TC stracef_log: 153 clone3({flags=CLONE_VM|...|CLONE_THREAD|...}, 88) = -1 EOPNOTSUPP (Operation not supported)
TC stracef_sh_rc=0 out=+++ exited with 0 +++
TC strace_plain_rc=0 out=
```

判读：

- C harness：`pthread_create` 返回 **95（EOPNOTSUPP）**，tracer **一个** 新 tracee stop
  都看不到（`new_tracee_stops=0`）；
- 真实 `strace -f`：python 线程启动失败（`RuntimeError: can't start new thread`），
  trace 里能看到**失败的那次调用本身**：`clone3(...) = -1 EOPNOTSUPP`；
- 不带 `-f` 的 strace 正常（对照组，证明是 TRACECLONE 特有缺口）；
- `strace -f /bin/sh -c true` 改前也 rc=0：dash 对 `true` 不 fork，**不是判别用例**，
  如实列出。

## 2. 任务 B：实现

| 文件 | 位置 | 改动 |
|---|---|---|
| `kernel/core/src/process/clone.rs` | 原 `:243-249` | **删除**“被 trace 的 clone 直接 `EOPNOTSUPP`”的检查 |
| 同上 | `:290-316` | 新增 `ptrace_trace_cloned_child()`：cloner 被 trace 且对应 `PTRACE_O_TRACE{CLONE,FORK,VFORK}` 打开时，把子任务 `attach_to` 同一 tracer，并给子任务投递 `SIGSTOP` |
| 同上 | `:331` / `:377` | 线程分支与进程分支都在 `run()` **之前**调用上述函数 |
| `kernel/core/src/thread/task.rs` | `:91` | 任务入口在**第一次 `execute` 之前**调用一次 `handle_pending_signal()`，把排队的 `SIGSTOP` 落实为 ptrace-stop；无挂起信号时为空操作 |

没有 `unsafe`、没有改 ostd、没有引入新抽象（复用 `attach_to`、`TraceeStatus`、既有的
信号/等待机制）。

## 3. 任务 C：验证

### 3.1 C1：失败用例改后（同一脚本）

```
TC compile=ok
SETOPTIONS traceclone rc=0 errno=0 (Success)
CLONE pthread_create_rc=0 new_tracee_stops=1 child_exited=1 read=4
RESULT traceclone PASS
TC harness_exit=0
TC stracef_python_rc=0 clones=1 out=
TC stracef_log: 154 clone3({flags=...|CLONE_THREAD|...} => {parent_tid=[155]}, ...) = 155
TC stracef_sh_rc=0 out=+++ exited with 0 +++
TC strace_plain_rc=0 out=
traceclone: OK      after_rc=0
```

对比改前：`pthread_create_rc` 95 → **0**，`new_tracee_stops` 0 → **1**，
`strace -f` 下 python 从报错变为 rc=0 且 trace 里出现**成功**的 `clone3`（带新 tid）。

### 3.2 C2：`strace -f` 跟踪多线程程序（原始输出）

3 个 python 线程（glibc 走 `clone3`）：

```
C2 py_rc=0 trace_lines=7
C2 clone_lines: 84 clone3({flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|
  CLONE_SYSVSEM|CLONE_SETTLS|CLONE_PARENT_SETTID|CLONE_CHILD_CLEARTID, child_tid=...,
  parent_tid=..., exit_signal=0, stack=..., stack_size=..., tls=...}
  => {parent_tid=[85]}, 88) = 85
  （共 3 条 clone3，每个线程一条）
C2 attached: 0
```

fork 出的子进程也被跟踪：

```
C2 fork_rc=0 lines=5
108 execve("/usr/bin/sh", ["sh", "-c", "true & wait"], ...) = 0
108 clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID|SIGCHLD, ...) = 109
109 +++ exited with 0 +++
108 --- SIGCHLD {si_signo=SIGCHLD, si_code=SI_KERNEL, ...}
```

`attached: 0` 不是问题：strace 从 `clone3` 的返回值直接拿到新 tid，不需要额外的
"attached" 提示；关键证据是子 tid 出现在 trace 里、且子进程的退出被记录。

### 3.3 C3：7 域矩阵

```
DESKTOP ok  APP ok  APT ok  CLIPBOARD ok  AUDIO ok  USB ok  SUSPEND ok
DAILY_DONE=ok   panic=0   allocation failed=0
```

（`INPUT_METHOD=fail` 是 7 域之外的既有缺口。）不带 `-f` 的 strace 在改前/改后都
rc=0（C1 的 `strace_plain` 对照）。

## 4. 与 Linux 的差异（诚实边界）

| 项 | Linux | 我们 |
|---|---|---|
| 父进程事件停 | `PTRACE_EVENT_CLONE`（`SIGTRAP\|0x80`，`status>>16=3`） | **未实现**；父进程不因 clone 停下 |
| `PTRACE_GETEVENTMSG` | 返回新 tid | 无事件，取不到 |
| 子任务初始停 | `SIGSTOP` 的 ptrace-stop | **已实现**（同样 `WSTOPSIG=19`） |
| 不设 flag | 子进程不被跟踪 | 相同（行为完全不变） |
| `CLONE_UNTRACED` | 不自动跟踪 | 相同 |

因此：**`strace -f` 的基本跟踪可用**（clone 事件、子 tid、子进程退出都能看到），
但**事件停语义不完整**——依赖父事件停的 tracer（例如某些 profiler/沙箱）会看到差异。
不宣称"完整支持 `PTRACE_O_TRACECLONE` 语义"。

## 5. 证据

- `evidence/2026-10-27-traceclone/check-before.txt`、`check-after.txt`（同一脚本前后）
- `evidence/2026-10-27-traceclone/strace-f-raw.txt`（clone3 / fork 原始行）
- `evidence/2026-10-27-traceclone/matrix.txt`（7 域矩阵）
- 主机工作区：`target/traceclone-before/`、`target/traceclone-after/`、
  `target/traceclone-c2/`、`target/matrix-tc/`
