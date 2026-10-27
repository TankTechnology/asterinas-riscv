# INPUT_METHOD 域失败定位：门禁把 fcitx5 启动错了，且测试客户端（xterm/pts）根本不可用

日期：2026-10-27
内核：`64b028b0c`（本轮未改内核）
任务书：`/tmp/tp/INPUT_METHOD.md`
门禁数据：`target/matrix-sockopt/matrix.tsv`（`INPUT_METHOD fail engine--no-text`）

## 0. 结论（先分类）

| 分类 | 是否命中 | 证据 |
|---|---|---|
| **A 框架未运行** | **是，但原因是门禁脚本** | 门禁执行 `fcitx5 -d --replace -v`。实测 **`-v` 是版本开关**：`fcitx5 -r -v` 打印 `5.1.12` 后 **rc=0 退出**；探针 1 里该命令启动后 `pgrep -c -x fcitx5` = **0**。去掉 `-v`（`fcitx5 -d --replace`）后守护进程 **alive=1**，并把 `XIM_SERVERS` 设为 **`@server=fcitx`** |
| **B 框架运行但 XIM 客户端侧不通** | **是** | 门禁的输入客户端是 xterm，而 xterm 的窗口**始终 `IsUnMapped`**（`xwininfo` 实测），stderr 反复 `open ttydev: Inappropriate ioctl for device` / `Input/output error`；未 map 的窗口无法获得焦点，`xdotool windowfocus` 返回 1、`windowactivate --sync` 超时（124），**任何按键（XTEST 或 XSendEvent）都到不了 xterm** ⇒ `/tmp/xim-out` 永远为空 |
| **C 协议通但引擎无候选** | **无法判定** | 没有客户端上下文时 `fcitx5-remote -n` 与 D-Bus `CurrentInputMethod` 都是空；`AvailableInputMethods` 已列出 `wbpy`/`wbx`/`pinyin` 等引擎，`XIM_SERVERS=@server=fcitx` 已注册 |

**即：这个域失败的直接原因是测试环境的两处缺陷，不是输入法框架本身。**
另外发现 **APP 域的 xterm 检查是假阳性**：它只 `pgrep -f "xterm -display"`（
`tools/x86/debian/daily_use_checklist.sh:56-61`），窗口没 map 也算 ok。

## 1. 证据链（5 个定点探针，同一内核、Xorg 按门禁方式启动）

| 探针 | 做什么 | 结果 |
|---|---|---|
| 1 | 复刻门禁 IM 段（`fcitx5 -d --replace -v`） | `fcitx alive=0`，日志只有 `5.1.12`；xterm `windowfocus_rc=1`、`XTEST/XSendEvent` 均为空 |
| 2 | `fcitx5 -r -v` 前台 vs `fcitx5 -d`；xev 收键 | 前台 `-v` → **rc=0 只打印版本**；`-d` → **alive=1**；**xev 在正确焦点下收到 2 个 KeyPress**（XTEST 与 XSendEvent 都能送达） |
| 3 | xterm 为何不 map；fcitx 引擎状态 | xterm `Map State: IsUnMapped`，stderr `open ttydev: ... Input/output error`；`fcitx5-remote -n` 为空；D-Bus `CurrentInputMethod` 为空，`AvailableInputMethods` 有 `wbpy` |
| 4 | pty/devpts 是否可用 | `devpts on /dev/pts` 已挂载；python `openpty` 成功（`/dev/pts/0`，读写 `hi` 正常）⇒ pty 本身可用，xterm 的失败是它自己的 tty 建立路径 |
| 5 | TIOCSCTTY 路径 | pty 可建、可读写；**子进程的 setsid/TIOCSCTTY 输出未捕获**（探针里子进程用 `os._exit` 未 flush），此项**未定论** |

关键原始行：

```
IM3 fg_rc=0 fg_alive=0 fg_log=5.1.12|
IM3 d_start rc=0 alive=1 ...
IM3 xim=XIM_SERVERS(ATOM) = @server=fcitx
IM3 xev_keypresses=2 ... KeyPress event ... window 0x600001
IM4 win=6291468 map=  Map State: IsUnMapped
IM4 xterm_err=open ttydev: Inappropriate ioctl for device|open ttydev: Input/output error|...
IM5 devpts_mount=[devpts on /dev/pts type devpts (rw,relatime)|]
IM5 openpty ok master=3 slave=4 name=/dev/pts/0
```

## 2. 尝试的修复

1. **fcitx5 启动参数**：把 `-v` 去掉（`fcitx5 -d --replace`）。守护进程能起来并注册 XIM
   server —— 这是门禁脚本的确定缺陷。
2. **改用不依赖 pty 的 XIM 客户端**（探针内嵌 C，Xlib `XOpenIM`/`XCreateIC`/
   `XFilterEvent`/`Xutf8LookupString`，自建窗口、无 pty）：**编译通过**，
   但运行阶段见 §3。
3. **没有改内核**：没有新的 `ENOSYS/EPERM` 证据指向内核 syscall 缺口；
   `deny(unsafe_code)` 与 ostd 抽象均未触碰。

## 3. 最终结果（未修复）

自建 XIM 客户端（不依赖 pty）在 `fcitx5 -d --replace` 正常运行、`XIM_SERVERS=@server=fcitx`
已注册的前提下运行，逐步输出（无缓冲）：

```
IM7 fcitx alive=1 engine=[]
IM7 after_o engine=[] xim=[XIM_SERVERS(ATOM) = @server=fcitx]
IM7 FT before_openim
IM7 FT after_openim im=0x55555561da00      <- XOpenIM 成功
IM7 FT styles=5 chosen=0x408               <- XIMPreeditNothing|XIMStatusNothing
IM7 FT before_createic
IM7 done                                   <- 没有 after_createic：XCreateIC 阻塞
```

即：**XIM 服务器能应答 `XOpenIM`（连接/风格协商通过），但 `XCreateIC` 收不到回复**，
客户端在 15 s 超时被杀。没有 IC 就没有输入上下文，任何客户端都打不出字——
这与门禁里 xterm 的失败是同一层（客户端侧 XIM 建立不了上下文），但比 xterm 的
tty 问题更靠近协议层。

因此本轮**没有达成用户可见验收**，`INPUT_METHOD` 仍应记为 **fail**。
已落地的唯一修复是启动参数（§2.1），它让框架真正运行，但不足以让域通过。

## 4. 未修复的部分与下一步

- **用户可见验收未达成**：本轮没有做到「在 Debian 里实际打出一个中文字符」，
  因此 **INPUT_METHOD 仍应记为 fail**，不宣称 ok。
- **XCreateIC 阻塞**是本轮能定位到的最深一步；还没有证明它属于内核还是 fcitx5/Xlib。
  下一步最少要做的两件事（都未做）：
  1. 对客户端 `strace -f -tt` 看阻塞点（是 `poll`/`read` 在 X 连接上，还是 futex）；
  2. 对 fcitx5 的 XIM 线程做同样的事，判断是服务端不回复还是客户端不收。
  如果两边都在等对方，问题就在 XIM 用的 select/poll 语义（内核）而不是配置。
- 需要按顺序做：
  1. **门禁 IM 段**：`fcitx5 -d --replace`（去掉 `-v`），并改用**能 map 的客户端**
     （自建 Xlib XIM 客户端，或先修好 xterm 的 tty 路径）；
  2. **单独定位 xterm 的 `open ttydev` EIO**：devpts/pty 基本功能正常，问题在
     xterm 建立控制终端的路径（`setsid`/`TIOCSCTTY`/slave 打开），这会影响所有
     终端类应用，属于独立缺口；
  3. **修 APP 域的假阳性**：xterm 检查应从 `pgrep` 改为「窗口已 map 且能收到键入」。

## 5. 证据

- 探针脚本：`/tmp/appmatrix/im_probe{,2,3,4,5,6}.sh`（未入库）
- 主机 console：`target/im-probe*/console.txt`（本轮结束后已清理大镜像，console 保留）
- 门禁原始结果：`target/matrix-sockopt/matrix.tsv` 的 `INPUT_METHOD fail engine--no-text`
- 未 push。
