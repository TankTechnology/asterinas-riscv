# ④ 输入法：fcitx5 + 拼音（实测卡点）

**状态：未通过。** 镜像里能装、守护进程能起、能连上会话总线，但 fcitx5 在加载
输入法后崩溃（回溯落在 `libpinyin.so`），`fcitx5-remote -n` 取不到引擎；
只用键盘引擎重启同样拿不到引擎。矩阵「输入法」按实测记为 fail，卡点如下。

## 已经做到的

- 镜像内安装成功（apt 走 slirp 网络没问题）：
  `fcitx5 fcitx5-modules fcitx5-chinese-addons fcitx5-frontend-gtk3 dbus-daemon`
  （`--no-install-recommends`，先 `apt-get update`）。
- root shell 没有会话总线，清单里用 `dbus-daemon --session --fork --print-address`
  起一个私有总线并导出 `DBUS_SESSION_BUS_ADDRESS`。
- fcitx5 能启动并注册到总线：

```
I ... addonmanager.cpp:205] Loaded addon fcitx4frontend
I ... dbusmodule.cpp:790] Service name change: org.fcitx.Fcitx5  :1.0
DAILY_FCITX alive=1 addons=...,pinyin.conf,pinyinhelper.conf,table.conf,xim.conf,...
DAILY_FCITX engine='' remote_rc=0
```

- XIM 相关 addon（`xim.conf`、`xcb.conf`）与中文（`pinyin.conf`、`pinyinhelper.conf`、
  `table.conf`）都在。

## 本轮进展（syscall 补齐后）

内核已支持 `MSG_NOSIGNAL` 与 `waitid(WEXITED)`，并在客体里用静态小工具验证：

```
MSGTEST_SEND ret=-1 errno=32        # EPIPE，进程没被 SIGPIPE 杀死
MSGTEST_WAIT rc=0 errno=0 code=1 status=7
MSGTEST_PASS
```

两处“unsupported”告警随之消失。fcitx5 现在**能一直活着**（`alive=1`），但
`fcitx5-remote -n` 仍返回空（键盘引擎 profile 下同样为空），所以还没到“能输入
汉字”的门槛。注意上一轮记录的 libpinyin 回溯发生在守护进程退出路径上
（`Unloading addon ...`），补 syscall 后进程不再退出，但引擎查询仍未打通。

## 本轮定位（sync_file_range 补齐后）

`sync_file_range`（x86_64 #277）原本返回 ENOSYS，dpkg/apt 安装 fcitx5 时反复重试；
补齐后安装稳定，fcitx5 守护进程存活并**在会话总线上**，但
`org.fcitx.Fcitx5 /controller CurrentInputMethod` 返回空字符串。启动日志给出确切原因：

```
E ... pinyin.cpp:737] Failed to load pinyin history: io fail: iostream error
I ... addonmanager.cpp:205] Loaded addon pinyin
/usr/lib/x86_64-linux-gnu/fcitx5/libpinyin.so(+0x84b9d) [0x7ffffc74fb9d]
I ... addonmanager.cpp:308] Unloading addon pinyin
```

即：pinyin 附加加载后**读用户词典/历史失败**（`io fail: iostream error`），随即在
libpinyin 内崩溃，输入法组因此为空。

### 路径 1 的结果（预建 history + 关用户词典）

预建 `~/.local/share/fcitx5/pinyin/`、空 `history`，并在
`~/.config/fcitx5/conf/pinyin.conf` 里写 `EnableUserDictionary=False`、
`HistoryEnabled=False` 后，**错误不变**：

```
E pinyin.cpp:737] Failed to load pinyin history: io fail: iostream error
DAILY_FCITX alive=1 engine='' rc=0 dbus_im='string ""'
```

说明不是“文件不存在/词典损坏”，而是 fcitx5-pinyin 读取 history 时的
**文件 I/O 语义**在内核上出了问题（C++ iostream 报 io fail）。下一步应抓该进程
失败前后的 openat/read/fstat/close 返回值，而不是继续调 fcitx 配置。

### 最小文件 I/O 复现（`iotest`）的结果

按 libpinyin 的典型路径逐步调用，**全部成功**：

```
IOTEST open_rw_create   ret=3
IOTEST fstat/read/ftruncate/mmap/msync/munmap/fdatasync/fsync/close  ret=0
IOTEST open_rd ret=3  read_back ret=8
IOTEST fopen_r+ ret=0  fseek ret=0  fwrite ret=1  fflush ret=0
IOTEST_DONE
```

所以基础文件 syscall（含 mmap/msync/fdatasync）都没有语义缺口；`io fail:
iostream error` 来自 fcitx5-pinyin 自己的 C++ 流/解析逻辑（空 history 文件的
格式/异常路径），不是缺 syscall。下一步应针对该进程抓 iostream 之前的最后一次
文件操作与 errno，或直接给它一个带合法头部的 history 文件。

### 2400 s 预算复跑的结果（本轮）

把门禁超时提到 2400 s 后，清单在输入法域**中途结束**：串口最后的 marker 是
`DAILY_CLIPBOARD=ok`，紧接着就是外层 shell 的 `DAILY_SHELL_END`；`msgt/iotest`
都跑完了（`IOTEST_DONE`），但没有任何 fcitx5 输出、也没有
`DAILY_INPUT_METHOD`。也就是 `sudo sh -c` 里的清单在
`apt-get update/install fcitx5...` 期间被终止（客体是 4 GB 内存，暂不能归因于
OOM；apt 输出重定向到 /tmp，串口看不到原因）。下一次复跑应把 apt 的输出直接
打到 /dev/console，或干脆把 fcitx5 相关包预装进 rootfs profile，绕过运行时安装。

### 码表引擎回退（上一轮）

清单里加了回退分支：pinyin 引擎为空时安装 `fcitx5-table-extra`，切 `wubi` 码表
（`wqiy`+空格、`vbg`+空格 → “你好”），与 libpinyin 完全无关。本轮门禁**没跑完**：
`DAILY_CLIENT ... done=False`——镜像里两次 apt 安装（fcitx5 依赖 + table-extra）
把 900 s 的门禁预算用完，清单在输入法域被截断，因此还没有码表引擎的结论。

下一步只有一件小事：把 `X86_DEBIAN_XORG_TIMEOUT`（或 Makefile 的
`--timeout`）提到 1500 s，或把这两个包装进 rootfs profile，然后复跑即可拿到
`DAILY_FCITX3 table_engine=...` 的判定。

### 码表引擎实测（本轮，门禁跑完）

改成只装 `fcitx5 fcitx5-modules fcitx5-table-extra dbus-daemon`（不装
chinese-addons/libpinyin），profile 用 `DefaultIM=wubi`，门禁 1800 s 内完整跑完：

```
DAILY_FCITX alive=1 engine=''
DAILY_INPUT_METHOD=fail fcitx5-no-engine:debconf:_falling_back_to_frontend:_Readline
```

守护进程活着，但引擎名依旧为空——**与 pinyin 无关**，是 fcitx5 在到达
“加载输入法组”之前就没能激活任何 IM。下一步应看 `fcitx5 -d` 完整日志里
`Loaded addon keyboard/wubi` 与 `addonmanager` 的失败行，并试
`fcitx5-remote -o`（激活）后再查 `-n`。

### 总线/激活诊断（本轮，门禁完整跑完）

```
DAILY_FCITX alive=1 engine=''
DAILY_DBUS  ... array [ string "org.freedesktop.DBus" string ":1.3"
             string "org.freedesktop.portal.IBus" ... ]      # 截断 240 字节
DAILY_FCITX activate_rc=0
DAILY_FCITX after_activate=''
```

- 守护进程存活；`fcitx5-remote -o`（激活）返回 0；激活后 `-n` 仍为空。
- 会话总线能查询（`ListNames` 有返回），但截断输出里没能确认
  `org.fcitx.Fcitx5` 是否在列；此前 fcitx5 自己的日志有
  `Service name change: org.fcitx.Fcitx5 :1.0`，说明它确实注册过。

结论：不是“总线完全没连上”，也不是引擎崩溃，而是 **IM 组没有被激活/初始化**。
下一步最短路径：装 `fcitx5-diagnose`（随 fcitx5 提供）看 “Input Methods” 一节，
或直接 `dbus-send ... org.fcitx.Fcitx.Controller1.SetCurrentIM string:<wubi>` 设定
当前 IM；如果仍然为空，检查 classicui/tray 与 xcb frontend 的初始化日志
（无 WM/托盘环境可能让它停在未激活状态）。

### 真实 IM 定义清单（本轮，结论性）

按门禁要求从客体里读真实 addon 名，而不是猜：

```
DAILY_IM_ADDONS tables=[] ims=[]
DAILY_IM_CHOSEN 'keyboard-us'
DAILY_FCITX alive=1 engine=''
DAILY_FCITX activate_rc=0
DAILY_FCITX after_activate=''
DAILY_INPUT_METHOD=fail fcitx5-no-engine:...
```

`/usr/share/fcitx5/table/` 与 `/usr/share/fcitx5/inputmethod/` **都是空的**：镜像源里
`fcitx5-table-extra` 并没有落任何码表，`fcitx5` 本体也没带来可用的 IM 定义；
连 `keyboard-us` 都注册不出引擎，说明这套最小安装缺少 IM 数据，而不是 fcitx5 本身
不能跑（守护进程存活、D-Bus 调用返回 0）。

### 结论与可执行的下一步

`/usr/share/fcitx5/{table,inputmethod}` 为空 = 客体里的 fcitx5 没有任何可注册的
IM 定义，这是 `engine=''` 的直接原因；守护进程、D-Bus、激活调用都正常。

修复路径（不需要再调 profile）：

1. 宿主侧（Debian 容器）`apt-get download` 或 `dpkg-deb -x` 解出
   `fcitx5-table-extra` / `fcitx5-chinese-addons-data` 的 `/usr/share/fcitx5/**`；
2. 用 `debugfs write` 在镜像构建阶段把这些文件写进 `/usr/share/fcitx5/`
   （inputmethod/*.conf、table/*.conf），保证两个目录非空；
3. 起 fcitx5 后先确认 `ls /usr/share/fcitx5/inputmethod` 非空，再查
   `fcitx5-remote -n`；拿到引擎名后跑 xterm + XIM 打字用例。

注意：容器里的包版本（Ubuntu 5.1.3）与 guest（Debian trixie）可能不同，优先用
与镜像同源的数据文件；纯 .conf/码表数据通常可跨小版本使用。

### 数据下载尝试（本轮）

`docker run --rm -v /tmp/fcitx5-debs:/out debian:12 …apt-get download fcitx5-data
fcitx5-table fcitx5-table-extra fcitx5-chinese-addons fcitx5-modules` 只完成了
镜像拉取，`/out` 为空——容器内的 `apt-get update/download` 没有产出 deb
（网络或包名问题）。下一轮改用更可靠的取数方式：

**从门禁跑完后的镜像里取包**：`target/daily-use/daily.ext2` 是客人跑过清单的镜像，
`dpkg` 安装时 deb 会进入 `/var/cache/apt/archives/`，用
`debugfs -R "ls -l /var/cache/apt/archives"` 就能 dump 出 Debian trixie 版本的
fcitx5 deb，再 `dpkg-deb -x` 检查它到底有没有 `/usr/share/fcitx5/inputmethod/*.conf`。
这比跨发行版下载更可靠，也能回答“是包本身没有数据，还是 dpkg 没装全”。

### 装了数据 + 有焦点客户端后仍为空（本轮，结论）

装 `fcitx5-chinese-addons` 后 `/usr/share/fcitx5/inputmethod/` 有 10 个 IM
（`cangjie db erbi pinyin qxm shuangpin wanfeng wbpy wbx zrm`），门禁里选中
`wbpy`（不碰 libpinyin）；同时改成**先起 xterm 并聚焦，再查引擎**。结果：

```
DAILY_FCITX alive=1 engine='' window='4194316'
DAILY_FCITX activate_rc=0
DAILY_FCITX after_activate=''
```

也就是说：IM 定义存在、客户端有焦点、激活调用成功，但控制器依然报“没有当前 IM”，
且日志里没有 `InputMethodGroup`/input-context 相关输出。剩余嫌疑集中在 fcitx5 的
输入上下文/前端（`xcb`/`xim`）没有真正建立——下一步应在 `fcitx5 -d -v` 日志里
grep `InputContext`/`InputMethodGroup`/`xim`，并用 `fcitx5-diagnose` 的
“Input Methods / Frontends”两节确认。

### 端到端直接验证（本轮，跳过 -n）

按“`-n` 是按 IC 返回、空不代表不可用”的口径，改成无条件打字：xterm 聚焦后
`xdotool type wqiy` + space、`vbg` + space + Return，判据是 `/tmp/xim-out`。

结果：`DAILY_INPUT_METHOD=fail engine--no-text:` —— **`/tmp/xim-out` 是空的**。
连未经过输入法的原始 ASCII 都没有落进去，说明按键没有送达该 xterm（焦点/XIM 抓键
环节），而不是输入法引擎本身的问题。下一步：用 `xdotool getactivewindow` /
`xev` 确认按键去向，并明确 xterm 启动时是否带 `+xim`（`xterm -im fcitx`）。

### 焦点/XIM 诊断（本轮，定位完成）

```
DAILY_XDIAG active= win=4194316
DAILY_XDIAG xim=XIM_SERVERS(ATOM) = @server=fcitx|
DAILY_XDIAG direct_out=
```

- **XIM server 已注册**：`XIM_SERVERS = @server=fcitx`，说明 fcitx5 的 XIM 前端正常。
- `xdotool getactivewindow` **为空**：会话里没有窗口管理器，没有任何窗口持有键盘焦点。
- 连 `xdotool type --window <id>` 的直投也没进 `/tmp/xim-out`（xterm 忽略合成事件）。

所以打字失败是**测试夹具**的问题（无 WM → 无焦点；XSendEvent 被 xterm 忽略），
不是输入法本身。下一步：先起一个轻量 WM（镜像里已有 matchbox-window-manager）
或让 xterm 自己成为焦点窗口，再用 `xdotool key/type` 走真实焦点路径。

## 卡点（确切错误）

启动数秒后 fcitx5 崩溃，串口里是回溯 + 两条内核“未支持”告警：

```
fcitx5: /usr/lib/x86_64-linux-gnu/fcitx5/libpinyin.so(+0x84b9d)  [0x7ffffc74fb9d]
fcitx5(+0x110ef)  [0x7ffffc74fb9d]
WARN : unsupported flags: MSG_NOSIGNAL
WARN : unsupported wait options are found: WEXITED
```

之后 addonmanager 依次 `Unloading addon ...`（进程退出），`fcitx5-remote -n` 为空。
把 profile 换成仅 `keyboard-us` 再起，引擎同样为空——说明不只是 libpinyin，
fcitx5 的启动/崩溃处理路径本身在这套内核上活不下来。

`MSG_NOSIGNAL`（send/recv）与 `waitid(WEXITED)` 是内核明确未实现的选项，
很可能是崩溃处理器/子进程等待路径的触发点；真正的崩溃信号还需要一次带
core/信号日志的复现才能定位到具体帧。

## 两条替代路径（已按实测更新）

1. **先解决 pinyin 历史文件的 I/O**：预建 `~/.local/share/fcitx5/pinyin/`
   与历史文件（或在 fcitx5 配置里关闭历史），复跑看是否还触发 libpinyin 崩溃；
   若仍崩，在信号投递处打印 faulting signal + 用户态 PC，定位到具体帧
   （当前只知道落在 `libpinyin.so+0x84b9d`）。
2. **绕开 libpinyin**：
   - 试 `fcitx5-table` 路线（`table.conf` 已加载）：装 `fcitx5-table-extra`，
     用拼音/五笔码表引擎，不碰 libpinyin；
   - 或按 rootfs profile 预置一个在本内核上验证过的 fcitx5（本地 deb + `dpkg -i`
     打进镜像），避免运行时 apt 的不确定性。

## 下一步（拿到引擎之后才有意义的验证）

`fcitx5-remote -n` 返回 `pinyin` 后，清单里的 XIM 端到端用例（xterm + xdotool
打 `nihao` + 空格选词，检查 `/tmp/xim-out` 里出现“你好”）就会给出 ok/fail 的
确定结论。
