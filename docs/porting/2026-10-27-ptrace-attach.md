# PTRACE_ATTACH / PTRACE_DETACH：让 `strace -p` 可用（最小子集）

日期：2026-10-27
内核基线：`34daf75e2` + 本提交
本轮改动：**只补 `PTRACE_ATTACH(16)` 与 `PTRACE_DETACH(17)`**；`strace -p` 需要的其余
request（`GETREGS`/`PEEKTEXT`/`SYSCALL`/`CONT`/`SETOPTIONS`）在基线里**已经实现**，
本轮没有重写、只做了实测确认。

## 1. 背景与现状核查

`e74d914e8` 时观察到 `strace -p` 记录 0 行，原因在
`kernel/core/src/syscall/ptrace.rs` 的请求枚举里，`PTRACE_ATTACH`/`PTRACE_DETACH`
两行被注释掉（旧 `:229`/`:231`），任何 attach 都返回 `EIO`。

核查后发现基础设施比当时判断的完整得多：

| 能力 | 位置（改动前已存在） |
|---|---|
| `PTRACE_TRACEME`、信号断点与 wait4 报告（`WIFSTOPPED`、`WSTOPSIG`） | `syscall/ptrace.rs:36`、`process/posix_thread/ptrace/mod.rs`、`process/wait.rs:246` |
| syscall 进入/退出断点（`SIGTRAP\|0x80`，需 `PTRACE_O_TRACESYSGOOD`） | `thread/task.rs:103-110`、`ptrace/util.rs:219-225` |
| `GETREGS`/`SETREGS`、`PEEKTEXT/PEEKDATA`、`POKETEXT/POKEDATA` | `syscall/ptrace.rs:43-70,95-111` |
| `CONT`/`SYSCALL`/`SINGLESTEP`、`SETOPTIONS`、`GETSIGINFO` | `syscall/ptrace.rs:71-128` |
| 权限检查（同 uid/gid 或 `CAP_SYS_PTRACE`） | `posix_thread/alien_access.rs:15-25` → `security/lsm/modules/capability.rs:50-84` |

## 2. 改动（文件:行号）

### 2.1 `kernel/core/src/syscall/ptrace.rs`

- 枚举新增：
  ```rust
  PTRACE_ATTACH = 16,
  PTRACE_DETACH = 17,
  ```
- `PTRACE_ATTACH`：
  1. `pid_table::pid_table_mut().get_thread(tid)` 取目标线程（不存在 → `ESRCH`）；
  2. 复用已有的 `do_ptrace_attach(&tracer_thread, tracee_thread)`（含
     main-thread tracer 检查、跨进程检查、`check_alien_access_from(ATTACH_WITH_REAL_CREDS)`）；
  3. 给目标投递 `SIGSTOP`，使其在下一个信号处理点进入 ptrace-stop——与 Linux 一致，
     tracer 的 `wait4` 会看到 `WIFSTOPPED` + `WSTOPSIG=SIGSTOP`。
- `PTRACE_DETACH`：
  1. `ctx.posix_thread.get_tracee(tid)`；
  2. `tracee.ptrace_detach(sig_num, ctx)`（见 2.2）；
  3. `ctx.posix_thread.remove_tracee(tid)` 清理 tracer 侧的映射。

### 2.2 `kernel/core/src/process/posix_thread/ptrace/mod.rs`

新增 `PosixThread::ptrace_detach` 与 `TraceeStatus::detach_from_stop`：

- 要求目标处于 ptrace-stop，否则 `ESRCH`（与 Linux 一致）；
- **丢弃**造成这次 stop 的信号（`state.signal.clear()`），这样 detach 后目标继续运行，
  而不会把 `SIGSTOP` 真正投递给它（Linux 的 `PTRACE_DETACH` 语义）；
- 清除 `tracer`、单步标志、`is_tracing_syscall`，置 `is_stopped=false` 并唤醒；
- `data` 非 0 时按 Linux 语义在 detach 后向目标投递该信号。

新增 `PosixThread::remove_tracee(tid)`（tracer 侧映射清理）。

**未引入任何 `unsafe`**，没有改 ostd，也没有新增抽象；全部复用既有的
`TraceeStatus`/`StopDeliverySignal`/wait 机制。

## 3. 验证（三层）

### 3.1 B2：attach 停住 / continue / detach 不杀（ctypes ptrace 客户端）

```
PA py: attach rc=0 errno=0
PA py: wait4 after attach pid=78 WIFSTOPPED=1 WSTOPSIG=19
PA py: tracee state while stopped: t
PA py: detach rc=0 errno=0
PA py: alive after detach: True state: S
PA py: target final WIFEXITED=1 exit=0 signal=-1
PA py: B2b attach rc=0 stop=1 sig=19
PA py: B2b cont rc=0 errno=0
PA py: B2b final WIFEXITED=1 exit=0
```

- attach 后目标 `state=t`（stopped, tracing stop），`wait4` 给出 `WSTOPSIG=19`（SIGSTOP）；
- detach 后立刻恢复 `state=S` 并正常跑完（exit 0），**没有被 SIGSTOP 停住、也没有被杀**；
- `PTRACE_CONT` 后同样正常跑完。

### 3.2 B1：真实 `strace -p`

```
PA strace_rc=124 log_lines=39 err=strace: Process 80 attached|strace: Process 80 detached|
PA strace_log_head: clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, {tv_sec=7, tv_nsec=905781473}, NULL) = 0|...
PA target_alive_after_strace: yes
PA target_rc=0 log=target-start|target-end|
```

`strace -p` 录到 **39 行 syscall**（含参数与返回值），正常 attach/detach，目标未被杀、
正常退出。`strace_rc=124` 是外层 `timeout 2` 的退出码，属预期。

### 3.3 B3：7 域矩阵

同一内核、`daily_use_checklist_regress.sh`、基座 `target/systemd-health/daily.ext2`：

```
DESKTOP ok   APP ok   APT ok   CLIPBOARD ok   AUDIO ok   USB ok   SUSPEND ok
DAILY_DONE=ok   panic=0   allocation failed=0
```

（`INPUT_METHOD=fail` 是这 7 域之外的既有缺口。）

### 3.4 权限边界

| 场景 | 结果 | 判定 |
|---|---|---|
| uid 1000 → **root** 进程 | `rc=-1 errno=1 (EPERM)` | ✅ 拒绝跨 uid |
| uid 1000 → 同 uid 的**兄弟**进程 | `rc=-1 errno=1 (EPERM)` | ✅ 与 Debian 默认一致：Yama `Relational(1)` 只允许祖先/子进程 |
| root → uid 1000 进程 | `rc=0` | ✅ `CAP_SYS_PTRACE` 放行 |
| uid 1000 **父进程 → 自己的子进程** | `rc=0`，`WIFSTOPPED=1 WSTOPSIG=19`，detach 后子进程 exit 0 | ✅ 允许（祖先关系） |

`/proc/sys/kernel/yama/ptrace_scope` 默认 1（`security/lsm/modules/yama.rs:128`），
与 Debian 相同；因此"同 uid 但非祖先"被拒**不是**本次改动的缺陷。

补充说明：早期用 `su asterinas -c "timeout 3 strace -p <root-pid>"` 的探针把整轮
checklist 卡死（gate 超时，无 panic），换用 `setpriv` 后同样场景立即返回 EPERM。
该卡死**未复现、未定位**，与本轮改动的关系不明，如实记录，不作为结论。

## 4. 边界：实现了什么、没实现什么

**已实现（可用）**：`ATTACH`、`DETACH`、`TRACEME`、`CONT`、`SYSCALL`、`SINGLESTEP`、
`GETREGS`/`SETREGS`、`PEEKTEXT`/`PEEKDATA`、`POKETEXT`/`POKEDATA`、`PEEKUSER`/`POKEUSER`、
`SETOPTIONS`（`TRACESYSGOOD`/`FORK`/`VFORK`/`CLONE`/`EXEC`/`VFORKDONE`/`EXIT`/`EXITKILL`）、
`GETEVENTMSG`、`GETSIGINFO`、`KILL`。

**未实现（仍返回 `EIO`）**：
- `PTRACE_SEIZE`(0x4206)、`PTRACE_INTERRUPT`(0x4207)、`PTRACE_LISTEN`(0x4208)；
- `PTRACE_GETREGSET`/`SETREGSET`(0x4204/0x4205)、`GET_SYSCALL_INFO`(0x420e)；
- `GETFPREGS`/`SETFPREGS`/`GETFPXREGS`/`SETFPXREGS`；
- `PTRACE_SYSEMU`/`SYSEMU_SINGLESTEP`；
- `PTRACE_O_TRACESECCOMP` 及 seccomp 事件。

**已知限制**（沿用既有实现，不是本轮引入）：
- tracer 必须是它所在进程的**主线程**（否则 `EOPNOTSUPP`），且 tracer/tracee 必须属于
  **不同进程**（否则 `EPERM`）；
- attach 只作用于**指定的那一个 tid**（不自动挂上其他线程；`strace -f -p` 的多线程枚举
  依赖后续逐个 attach）；
- `strace` 会带 `__WALL` 调 `wait4`，我们只打印 `unsupported wait options ... WALL` 告警
  并忽略该位；实测不影响 `strace -p` 的行为。

**没有声称"ptrace 已完整支持"**：以上未实现项在 `strace -p` 的常见路径之外，
需要时仍会失败。

## 5. 证据

- `evidence/2026-10-27-ptrace-attach/attach-verify-raw.txt`（B1+B2 原始行）
- `evidence/2026-10-27-ptrace-attach/permission-raw.txt`（权限边界）
- `evidence/2026-10-27-ptrace-attach/matrix.txt`（7 域矩阵）
- 探针：`target/ptrace-attach/`、`target/ptrace-perm/`
