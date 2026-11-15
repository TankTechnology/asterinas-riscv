# pty 控制终端：O_NOCTTY 被忽略，ssh -tt 的 shell 没有作业控制（2026-11-15）

切到 OpenSSH 之后 `ssh -tt` 终于有 `/dev/pts/0` 了，但交互式 shell 仍然不对：
`bash -i` 打印 `cannot set terminal process group (-1): Inappropriate ioctl for device` 和
`no job control in this shell`，而且**在 pty 里按 Ctrl-C 会把整条 SSH 连接打断**
（`Connection reset by peer`）。这篇记录定位过程与修复。

## 1. 症状与危害

```
$ ssh -tt board 'python3 -c "import os; print(os.tcgetpgrp(0))"'
OSError: [Errno 25] Inappropriate ioctl for device
```

- 会话确实拿到了 pty（fd 0/1/2 都是 `/dev/pts/N`，数据双向正常）；
- 但这个 pty **不是该会话的控制终端**：`tcgetpgrp()` 返回 `ENOTTY`。
- 于是 bash 关掉作业控制；更糟的是行规程把 `^C`（ISIG）送给**终端所属会话**
  的前台进程组——也就是 sshd 自己，sshd 被 SIGINT 打死，客户端看到连接被重置
  （内核日志：`mm_reap: preauth child terminated by signal 2`）。

## 2. 根因

内核里有**两处**“打开 pty 从设备时自动把终端设为控制终端”的逻辑：

| 位置 | 是否看 `O_NOCTTY` |
| --- | --- |
| `PtySlaveInode::open` → `Terminal::acquire_control_on_open` | 看（正确，对应 Linux 的 `tty_open_proc_set_tty`） |
| `PtySlaveFile::new` → `let _ = terminal.set_control(&current!())` | **不看**（bug） |

sshd 的流程正好撞上第二处：

1. 每个连接的 `sshd-session` 进程启动时调用 `setsid()`（内核日志
   `setsid pid=A ppid=<listener>`），于是它是 session leader 且没有控制终端；
2. 它用 glibc `openpty()` 分配 pty——**从设备是用 `O_NOCTTY` 打开的**
   （日志 `slave-open pid=A flags=O_NOCTTY no_ctty=true`）；
   `PtySlaveInode::open` 正确跳过了自动接管，但 `PtySlaveFile::new` 又把终端
   设给了它 → `set_control ok pid=A sid=A`；
3. 真正要跑 shell 的子进程随后 `setsid()` + `TIOCSCTTY(0)`
   （`pty_make_controlling_tty`），因为 pty 已属于另一个会话，回答
   `EPERM "the terminal is already a controlling terminal of another session"`
   （sshd 对这一句只 `error()` 不退出，所以连接照常建立）。

Linux 不会这样：`tty_open_proc_set_tty()` 在 `O_NOCTTY` 下直接返回，pty 留给
真正调用 `TIOCSCTTY` 的会话。

## 3. 怎么调试的

改动一次内核、看一次证据，逐层排除（每轮都是 build → preflight → deploy → reboot，
板上跑一遍 `ssh -tt` 再读 `journalctl -k | grep ttydiag`）：

1. 先证伪“pty 坏了”：`script -qc 'test -t 0' /dev/null` 正常（`/dev/pts/0`），
   `pty.openpty()` 正常，`setsid + TIOCSCTTY` 单独跑也正常 → 数据通路没问题，
   问题在**谁拥有**这个终端。
2. 在 `set_control` 的三条失败路径上打点（非 leader / 会话已有终端 / `set_session`
   失败），加上成功路径 → 看到 sshd 会话进程成功的 `set_control ok`，而 shell
   进程是 `set_session-failed: EPERM`。
3. 在 `sys_setsid` 打点 pid/ppid → 才知道“setsid 的人”是 sshd-session 本身，
   shell 是它的孙子进程。
4. 在 pty 从设备 `open` 打点 `status_flags` → 决定性证据：
   **sshd 那次 open 带着 `O_NOCTTY`，却仍然被接管**，而 `set_control` 的调用点
   里有一处根本不看这个标志（`PtySlaveFile::new`）。
5. `is_control_and` 再补一条日志确认 `tcgetpgrp` 的 `ENOTTY` 是
   `session_terminal=none`（shell 的新会话确实没有终端），排除“对象不同”的假设。

顺带证伪过的假设：Tty 对象按 open 各建一份（不是，`Arc<PtySlave>` 每对 pty 一个）、
`O_NOCTTY` 在更底层被丢掉（不是，inode 那层看得到）、bash 自己调 `setsid`
（不是，bash 源码里没有）。

## 4. 修复

删掉 `PtySlaveFile::new` 里的接管，只保留 `PtySlaveInode::open` 那一处（它拿着
`status_flags`，是唯一能判断 `O_NOCTTY` 的地方）：

```rust
// kernel/src/device/pty/file.rs —— PtySlaveFile::new()
-        let terminal: Arc<dyn Terminal> = slave.clone() as Arc<dyn Terminal>;
-        let _ = terminal.set_control(&current!());
```

`xterm` 依赖的“打开从设备即成为控制终端”行为不受影响（走的是 inode 那条路）。

## 5. 验收（板上，修复后）

| 项 | 结果 |
| --- | --- |
| `tcgetpgrp(0)` | `sid=612 pgid=612 fg=612`（不再是 `ENOTTY`） |
| `bash -i` | 不再打印 `cannot set terminal process group` / `no job control` |
| 后台作业 | `sleep 45 &` → `[1] 668`，`jobs` → `[1]+ Running` |
| 前台 `cat` + `^C` | 只中断 `cat`，shell 存活，**连接不断**（修复前会 `Connection reset`） |
| 窗口尺寸 | 本地 120×40 → 板上 `stty size` 读到 `40 120` |
| 全屏程序 | `vi` 正常进出 alternate screen，退出码 0 |
| `script -qc` | 仍拿到 `/dev/pts/0`（`SCRIPT_TTY_OK`） |
| xterm（桌面上） | 子进程 fd0 = `/dev/pts/0`，`stty size` = `24 80` |
| scp / sftp / rsync | 校验一致 |

## 6. 相关

- 服务端切换见 `2026-11-15-ssh-openssh-cutover.md`（本篇是它之后的第二个坑）。
- pty 的 SCM_RIGHTS 与 poll 修复见 `2026-11-04-remote-access-and-deepseek.md`。
- 调试用的临时日志（`ttydiag:`）在定稿前已全部移除。
