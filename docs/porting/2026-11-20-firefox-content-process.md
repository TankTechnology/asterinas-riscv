# Megrez：Firefox 起不来内容进程（2026-11-20 排查记录）

> **2026-11-20 夜，根因已找到并修复**：内核在 `SCM_RIGHTS` 里传 UNIX socket 时要求
> `CAP_SYS_ADMIN`。Gecko 正是这样把一个新子进程的 IPC 通道交给它的，普通用户（uid 1000）
> 每一次子进程启动都被 `EPERM` 拒绝 → `Failed to launch socket subprocess`，内容进程从未出现。
> 见下面“根因”一节；`unix/ctrl_msg.rs` 已改，提交 `873964cb1`。
>
> 另有一个**独立的**第二因：板子没有 RTC，开机时钟停在镜像构建日期，所有 HTTPS 站点都报
> “证书未生效”。它不阻止内容进程启动，但会让**页面**加载失败，看起来像“浏览器还是坏的”。
> 已由 `megrez_clock_bootstrap.sh` + `asterinas-clock-persist.timer` 解决（提交 `f7ee37e7c`）。

**结论先行**：板上 Firefox 的**窗口/界面能渲染**，但**页面永远画不出来**。原因是 Gecko 的
**内容子进程（tab/socket/utility）启动后握手失败**，不是配置、不是沙箱开关、不是 profile 路径问题。
这是一条**回归**：2026-11-17 同一台板子的日志里有 `ASTERINAS_BROWSER_NAVIGATE_OK`（`06ea8220e`），
页面当时是能渲染的。

## 根因（2026-11-20 夜，已修复）

`unix/ctrl_msg.rs` 的 `from_control` 在 SCM_RIGHTS 消息里出现 UNIX socket 时调用
`lsm_hooks::on_capable(…, CapSet::SYS_ADMIN)`，理由是 socket 之间成环会泄漏内核内存
（Linux 用 GC 处理，见该处 FIXME）。后果是**普通用户根本不能传 socket 描述符**：

| 身份 | 同一段 Python 探测（两个 socketpair，把一个 fd 用 SCM_RIGHTS 发出去） |
|---|---|
| root | `SCM_OK bytes=1 fds=1` |
| asterinas (uid 1000) | `SENDMSG_FAIL 1 Operation not permitted` |

Gecko 在 `GeckoChildProcessHost` 里正是把 IPC 通道（一个 socket）这样交给子进程的，于是：

```
WARNING: Failed to launch socket subprocess @FSC::SFNS::Send (Error:0)
WARNING: Failed to launch tab subprocess
```

修复：去掉能力检查，保留告警（提交 `873964cb1`）。环仍然会泄漏，直到 `scm_graph`（已实现
边建模与成环拒绝，但尚未接入发送路径）接进来——拒绝调用会直接破坏程序，代价更大。

**验证**：`ps` 里出现 `Web Content`；窗口标题从 `Warning: Security Risk` 变成真实网页标题；
随后在时钟也修好后拿到 `ASTERINAS_BROWSER_NAVIGATE_OK url=https://www.baidu.com/`。

## 第二因：时钟（同样已修复）

板子没有 RTC。开机时钟来自 `/etc/asterinas-build-time`（镜像构建时间），于是每次开机都在
“几个月前”，证书全部 not-yet-valid。`openssl s_client` 看到的是**百度的真证书**（issuer
GlobalSign），所以这**不是**门户 MITM——早先本文把 TLS 告警归因为门户替换，是错的。

修法是 fake-hwclock 语义：`asterinas-clock-persist.timer` 每 15 分钟把 `date +%s` 写进
`/var/lib/asterinas/last-time`，开机由 bootstrap 优先采用它。板上实测（无任何人工干预）：

```
megrez-clock-bootstrap[50]: ASTERINAS_CLOCK_SEED_APPLIED current_before=1776109154
    seed=1795183748 source=last-time
browser-web-firefox[249]: ASTERINAS_BROWSER_NAVIGATE_OK url=https://www.baidu.com/
```

两个 unit 必须挂 `sysinit.target.wants/`：这块板子的 systemd 从不到达 `basic.target` /
`timers.target`。

## 现象（可直接复现）

```
[Parent N, IPC I/O Parent] WARNING: Failed to launch socket subprocess @FSC::SFNS::Send (Error:0)
[Parent N, IPC I/O Parent] WARNING: Failed to launch tab subprocess    @FSC::SFNS::Failed (Error:0)
[Parent N, IPC I/O Parent] WARNING: Failed to launch utility subprocess
JS: ExtensionParent.sys.mjs:1987: can't access property "addMessageListener", messageManager is null
ASTERINAS_BROWSER_NAVIGATE_FAIL url=... error=Browsing context has been discarded
```

`GeckoChildProcessHost.cpp` 里该行是 `LaunchError` 的回调：`@FSC::SFNS::Send` 是**失败的函数名**，
`(Error:0)` 说明没带 errno。

子进程**是存在的**：

```
<pid> firefox-esr -contentproc -ipcHandle 0 -signalPipe 1 -initialChannelId {...} -parentPid <ppid> ... forkserver
```

形态与仓库里"启动成功"的快照一致，说明 **fork + exec 成功**，断在之后的**IPC 握手**。

## 已经排除的（都在板子上实测过，不是推理）

| 假设 | 测试 | 结果 |
|---|---|---|
| SCM_RIGHTS 传 fd 坏了 | `sendmsg` 带 `SCM_RIGHTS` 传一个 pipe fd，收端用传来的 fd 写、对端读 | ✅ 正常 |
| AF_UNIX 通道坏了 | SEQPACKET / DGRAM / STREAM 三种 socketpair 各发各收 | ✅ 正常 |
| posix_spawn + dup2 坏了 | `posix_spawn(file_actions=DUP2(sock,0))`，子进程往 fd 0 写、父进程读 | ✅ 正常 |
| 多线程 fork 坏了 | 24 线程进程 fork，子进程回写 pipe | ✅ 正常 |
| 大地址空间 fork 坏了 | **224 MB + 24 线程**再 fork，子进程回写 | ✅ 正常 |
| IPC 基础原语缺失 | eventfd / memfd_create / timerfd / pipe2 | ✅ 都可用 |
| epoll 触发方式 | 水平触发、边缘触发、多 fd | ✅ 正常 |
| IPI 丢包导致握手超时 | `/proc/sys/kernel/asterinas_tlb_flush_stats`，重启浏览器前后对比 `resent` | ❌ 不增长，排除 |
| profile 被写坏 | 目录 28 项 `stat` 失败、`cache2` 是 root 的普通文件 | ✅ **已修**（见下），修好后 Marionette 能监听 |
| 沙箱 | `MOZ_DISABLE_CONTENT_SANDBOX=1` | ❌ 无效，且被 evidence 门禁禁止，已撤回（真正的原因是 SCM_RIGHTS 权限，见上） |
| 单进程模式 | `MOZ_FORCE_DISABLE_E10S=1` + 干净 profile + 本地 file:// 页面 | ❌ 仍要 tab 子进程，仍失败 |
| 渲染链 | 单进程下窗口 `1280x934` 真实出现、XUL 对话框能画字 | ✅ 渲染没问题 |

## 已修好的一半：profile 损坏

profile 目录（`/home/asterinas/.mozilla/asterinas-browser-web`）当时**28 个目录项 `stat` 失败**，
`MarionetteActivePort` 里装的竟是 `profiles.ini` 的内容，`cache2` 是 **root 拥有的普通文件**（本应是目录）。
这是 p2 上 ext2 目录块损坏的症状（与 `inode 131089` 同源）。

**处置**：`systemctl stop asterinas-browser-web` → 确认 firefox 退出 → 把 profile 目录 `mv` 成
`asterinas-browser-web.broken-<epoch>`（不删除）→ `start`。

**判据**：新 profile 里 `MarionetteActivePort` 内容为 `2828`、`127.0.0.1:2828` 能连上（服务端回
`Connection attempt denied because an active session has been found` 即已监听）。三条中前两条达成，
`NAVIGATE_OK` 仍不出现。**这一步是有效修复，但不是根因修复。**

## 新线索：启动时的三连错误循环

开启 `asterinas.syscall_diag` 后读 `/proc/<pid>/task/<tid>/asterinas_syscall`，抓到一个循环：

```
unlinkat   → EINVAL (-22)
symlinkat  → EEXIST (-17)
readlinkat → EINVAL (-22)
```

这正是 Firefox 的 **profile 锁协议**。现场检查：`.parentlock` 是**普通文件**（0 字节、nlink=2），
不是 Firefox 期望的符号链接（`ip:<pid>-<token>`），所以 `readlink` 必然 EINVAL。

**但直接复现失败**：在同一个目录、以同一个用户（asterinas）跑
`create → symlink → readlink → unlink`，结果完全符合 Linux 语义（EEXIST / EINVAL / unlink OK）。
所以那串 EINVAL 的**具体路径仍未定位**。

## 下一步（按成本排序）

1. **程序化操作 X**：板上若有 `xdotool`/`xte`/`xinput`，可绕过物理鼠标验证输入链路，
   也能先让界面"可用"；配合 `xwd` 截图走串口回传，就有一套不依赖显示器的远程操作闭环。
2. **内核侧取证（已备好，未上板）**：`kernel/core/src/syscall/mod.rs` 加了
   `asterinas.syscall_err_log` 开关——进程名含 `firefox` 的**每一次失败系统调用**都打印
   `SYSCALL_ERR pid=… nr=… errno=… comm=…`（上限 4000 行）。它会直接指出内核在拒绝哪个系统调用。
   镜像已构建（`target/osdk/asterinas/asterinas-osdk-bin.Image`，md5 `cdca7f5c…`），
   **只差送上板子**。
3. **profile 锁方向**：先试"预置正确的锁 / 关掉 profile 锁"这类不动内核的办法。

## 上板通道的教训（2026-11-20）

- **串口逐行推 9 MB 不可靠**：26,058 行 base64 推到最后一刻才发现有行被截断（一行变成 `eco`，
  把后面整段吞进未闭合引号）。**必须分块 + 每块 md5**。
- **U-Boot `loadx`（XMODEM）两次卡死**：第一次卡在等 `press ENTER`，第二次把板子留在 1.5 Mbaud
  接收态；115200/1.5M 都不再回应，**ESC / Ctrl-C 均无法恢复**。按文档这是已知形态，唯一出路是断电。
  **不要再在无人能断电的窗口里用它。**
- **"零字节回应" 与 "有回显但命令不执行" 是两类问题**：后者可用 Ctrl-C 救回（实测 `help` 能执行）；
  前者是链路/电源层面，只能靠物理检查。
