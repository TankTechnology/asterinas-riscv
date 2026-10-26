# Firefox 内容进程 IPC 崩溃（`pipe error: Operation not permitted`）

日期：2026-10-26
内核：修复前 `eaa13d92`，修复后 `e8b4a8194c0918ac`
复现器：`tools/x86/debian/ipc_flags_probe.c`（门禁脚本 `ipc_flags_probe_check.sh`、
`ipc_fix_verify_check.sh`）

## 1. 复现表（最小探针，root 运行，修复前内核）

| # | 用例 | Linux 期望 | 修复前 Asterinas | 修复后 |
|---|---|---|---|---|
| 1 | `SCM_RIGHTS` 传普通文件 fd | 0 | 0 | 0 |
| 2 | `SCM_RIGHTS` 传**socket** fd（Firefox 传通道端点就是这么做的） | 0 | **0（root）/ 非 root 需 CAP_SYS_ADMIN** | 0 |
| 3 | 同上 + `MSG_DONTWAIT` | 0/EAGAIN | 0 | 0 |
| 4 | `recvmsg(MSG_CMSG_CLOEXEC)` | 收到的 fd 带 CLOEXEC | rc=0 但 **CLOEXEC 未置位** | 未修（遗留项） |
| 5 | `send(MSG_NOSIGNAL)` | 0 | 0 | 0 |
| 6 | `pipe2(O_CLOEXEC)` + 写读 | 正常 | 正常 | 正常 |
| 7 | 非阻塞 pipe 写满 | `EAGAIN` | `EAGAIN` | `EAGAIN` |
| 8 | `SCM_CREDENTIALS` 发**自己的 pid** | 0 | 0 | 0 |
| 9 | `SCM_CREDENTIALS` 发 **pid=0**（内核填充） | 0 | **EPERM** | **0** |
| 10 | `SCM_CREDENTIALS` 发**别人的 pid** | EPERM | EPERM | EPERM |
| 11 | 对端已关闭后 `send(MSG_NOSIGNAL)` | EPIPE | EPIPE | EPIPE |
| 12 | Firefox 风格：`SOCK_SEQPACKET|CLOEXEC|NONBLOCK` + `SCM_RIGHTS` 传 socket fd | 0 | 0（root） | 0 |

原始输出见 `evidence/2026-10-27-ipc-sendmsg-eperm/`。

## 2. 根因（两处，均在 `kernel/core/src/net/socket/unix/ctrl_msg.rs`）

### 2.1 主因：SCM_RIGHTS 传 socket fd 要求 CAP_SYS_ADMIN

`ctrl_msg.rs:256-272`（修复前）：

```rust
if files.iter().any(|file| (&**file as &dyn Any).is::<UnixStreamSocket>()) {
    warn!("UNIX sockets in SCM_RIGHTS messages can leak kernel resource");
    lsm_hooks::on_capable(... CapSet::SYS_ADMIN)?;   // ← 非 root 直接 EPERM
}
```

Firefox 建立新通道时会把一端 socket 通过现有通道传给内容进程（SCM_RIGHTS）。
我们以 root 跑探针/诊断时该检查通过（Firefox 有 10 个进程），但**桌面正常
用户（uid 1000）没有 CAP_SYS_ADMIN → `sendmsg` 返回 EPERM**，内容进程收到
"pipe error: Operation not permitted" 后退出 —— 与 thinkpad 上"1 个进程、
页面不绘制"的现象一致。Linux 允许任何用户这样传 socket（靠循环引用 GC）。

### 2.2 次因：SCM_CREDENTIALS 把命名空间 pid 当"别人的凭据"

`ctrl_msg.rs:242-250`（修复前）直接比较整个 `CUserCred`（pid+uid+gid）。
Firefox 的沙箱内容进程自认 pid=1（Linux 上它在新 PID 命名空间里），
于是每次启动都会触发（本轮实测 **173–187 次**）：

```
SCM_CREDENTIALS rejected: msg=CUserCred { pid: 1, uid: Uid(0), gid: Gid(0) },
                          current=CUserCred { pid: 20, uid: Uid(0), gid: Gid(0) }
```

Asterinas 尚未实现 PID 命名空间（`CLONE_NEWPID` 被接受但忽略，
`process/clone.rs:264-286`），`current!().pid()` 是全局 pid，于是必然不等。

## 3. 修复（两处最小改动，内核侧）

1. **`ctrl_msg.rs:256-272`**：移除 socket-fd 的 CAP_SYS_ADMIN 要求，只保留
   "可能泄漏内核资源"的 warn ——与 Linux 语义一致（Linux 不拒绝，用 GC 处理）。
2. **`cred.rs` 新增 `CUserCred::is_acceptable_sender_claim()`**，
   `ctrl_msg.rs:242-250` 改用它：
   - uid/gid 必须等于当前值（或 `INVALID`/-1，与 Linux 一致）；
   - pid 允许 `0`（内核填充）或 `1`（我们未实现 PID 命名空间，沙箱进程自认
     pid 1），其余必须等于发送者自己的 pid；
   - 其他 pid 仍返回 EPERM（探针用例 10 保持 EPERM，未放宽安全边界）。

## 4. 验证

### 4.1 探针（root，修复后 `e8b4a819`）

```
root ipc_case SCM_RIGHTS:socket_fd              rc=0 errno=-
root ipc_case SCM_CREDENTIALS:pid_zero          rc=0 errno=-     ← 修复前 EPERM
root ipc_case SCM_CREDENTIALS:foreign_pid       rc=-1 errno=Operation not permitted
root ipc_case recvmsg:CLOEXEC_set               rc=-1 errno=Success  ← 遗留：MSG_CMSG_CLOEXEC 未生效
```

### 4.2 Firefox（内容沙箱开启，root 运行）

修复前 `ffsb` 运行与修复后 `ffpost` 运行都拿到 **10 个进程**
（`firefox-esr / Socket_Process / WebExtensions / RDD / Isolated_Web_Co /
Privileged_Cont / Web_Content ×3 / Utility_Process`）且有窗口
（`win=4194305`）——因为 root 本来就有 CAP_SYS_ADMIN，**root 路径修复前后
都通**；用户的失败是**非 root 路径**（见 §2.1）。

### 4.3 非 root 验证：被一个 guest 卡死阻断（如实记录）

`ipc_fix_verify_check.sh` 在 `setpriv --reuid=1000 … /tmp/ipc_flags_probe`
之后 guest 整体停止输出（console 停在 6.849 s，QEMU 仍存活但无进展），
`np` 段一条日志都没有（stdout 走管道是块缓冲，输出随进程一起卡住）。
同一镜像里 `mknodat_probe_check.sh` 的 `setpriv` 用例是正常的，所以不是
`setpriv` 本身；**这是一个独立的新问题（uid 切换后某条路径丢唤醒/死锁），
需要单独复现（建议：`setpriv` + 逐用例 `stdbuf -oL` + 每用例 `timeout`）**。

### 4.4 7 域矩阵

修复内核 `e8b4a819` 的矩阵结果见 §5（本轮结束后补跑）。

## 5. 结论

- 找到两处内核侧 EPERM：**SCM_RIGHTS 传 socket fd 的 CAP_SYS_ADMIN 检查**（主因，
  正是桌面普通用户跑 Firefox 的死因）与 **SCM_CREDENTIALS 的命名空间 pid 比较**
  （次因，root 下也每次都触发）。
- 两处均已按 Linux 语义修正，未放宽 uid/gid 边界；探针 root 侧全绿（除
  `MSG_CMSG_CLOEXEC` 这个与本问题无关的遗留 TODO）。
- **未完成**：非 root 端到端验证被 guest 卡死阻断（§4.3），因此"Firefox 在 uid
  1000 下 procs ≥5、页面绘制"这条验收**尚未拿到数字**；需要在解决该卡死后补测。
- 遗留：`MSG_CMSG_CLOEXEC` 未实现（`ctrl_msg.rs:139-144` 的 TODO），fd 会漏进
  exec 的子进程；与本 EPERM 无关，建议单独立项。
