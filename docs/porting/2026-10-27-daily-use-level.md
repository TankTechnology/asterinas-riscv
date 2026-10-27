# 日用化第一轮：软件 GL 默认化 + 应用级抽查

日期：2026-10-27　镜像：`target/ff-lag/daily.ext2` + `tools/x86/debian/softgl_defaults.sh`　
内核：`94ad82da6` 之后（含本轮全部修复）

## A. 软件 GL 环境默认化（已完成）

写进镜像（`softgl_defaults.sh`，debugfs 注入后 `e2fsck -fn` 通过），三条投递路径：

| 文件 | 覆盖的会话 | 理由 |
|---|---|---|
| `/etc/environment` | PAM 登录、su、**sudo**（新增 `session optional pam_env.so`） | 无显示管理器，Xsession.d 不会被读取；门禁/清单在 `sudo sh -c` 下运行 |
| `/etc/profile.d/90-asterinas-softgl.sh` | login shell | 兜底 |
| `/etc/bash.bashrc` | **交互式非登录 shell** | 本镜像 console getty 起的是非登录 bash，实测不读 profile.d |
| `/etc/tmpfiles.d/asterinas-softgl.conf` | `d /tmp/xdg 0700 root root -` | pam_systemd 不给 getty 会话 XDG_RUNTIME_DIR，且 /run/user/0 不存在 |

变量：`LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe GDK_GL=disable
GDK_BACKEND=x11 NO_AT_BRIDGE=1 XDG_RUNTIME_DIR=/tmp/xdg`。

### 零手动配置验收（新会话、不 source 任何变量）

```
# sudo 会话里实际携带（pam_env 从 /etc/environment 读取）
VARCHECK=GALLIUM_DRIVER=softpipe,GDK_GL=disable,LIBGL_ALWAYS_SOFTWARE=1,
ZERO map= Map State: IsViewable focus=6291458 tries=0
ZEROOUT=[你好]
```

`yad` 没有手动设任何 GL 变量，只设了输入法需要的 `XMODIFIERS/@im=fcitx` 与
`GTK_IM_MODULE=xim`；窗口 33 ms 内 map、一次拿到焦点、五笔上屏 `你好`。

### 对照组（把变量 unset 后）

```
WIN=            (xdotool search --name 返回空)
ZERO map= focus=2097439 tries=25
ZEROOUT=[]
```

变量去掉后 yad 连窗口都不建 —— 证明这一步是必要的，不是"本来就能跑"。

## B. 应用级抽查

| 应用 | 结果 | 证据 |
|---|---|---|
| Firefox（firefox-esr 已有） | **冷启动 5.4 s 到可交互窗口**；本地页渲染 | `FF_VIEWABLE=6291498 ms=5402`；标题 `asterinas-daily — Mozilla Firefox` |
| Firefox 菜单 | 能打开 | `F10` 后 root window tree 313 → 317，Escape 后恢复 |
| 文件管理器 pcmanfm | 能开窗、显示目录 | `PCMANFM win=8388610 Map State: IsViewable title=[daily]` |
| xterm | 能开窗、执行 `ls`、显示中文 | `XTERM win=10485772 IsViewable`；窗口内 `ls /home/asterinas/daily; printf 中文测试` |
| GTK 终端 | **镜像里没有**（gnome-terminal/xfce4-terminal/lxterminal 均未安装） | `command -v` 全空 |
| 编辑器 | **镜像里没有**（gedit/nano/vi/vim/mousepad/leafpad 均未安装） | `command -v` 全空 |

## C. 稳定性

本轮在 90 分钟时间盒内只跑了约 12 分钟（`date` 计的 720 s）的负载：
Firefox 常驻 + xterm 反复开关 + `dd 32 MiB conv=fsync` 读写循环。

- **panic：0；`allocation failed`：0；`heap:` 告警：0**（整段 console 日志）
- 进程数稳定在 32–33；结束后 firefox-esr 与 pcmanfm 仍存活
- **未完成 60 分钟要求**：时间盒不够，如实记录。下轮直接跑
  `end=$(date +%s)+3600` 的同款负载即可，判据不变（panic/alloc/heap 计数）。

一个顺带发现：本轮负载下 guest 的单调时钟只走了 **367 s**（`/proc/uptime`）
而此时墙钟已约 12 分钟，即 guest 时钟 ≈0.5× 墙钟。这会影响所有基于
`CLOCK_MONOTONIC` 的计时（门禁的 `sleep 300` 实际要等更久、应用超时语义偏移），
是日用体验的一个真实缺陷，下一轮值得单独定位（TSC/timer 频率）。

## 还差什么（按用户可感知度排序）

1. **60 分钟稳定性跑满**（本轮只有 12 分钟）。
2. **guest 单调时钟 ≈0.5× 墙钟**——影响所有超时/动画/计时。
3. **DRM/GL 栈**：让 Mesa 的 `virtio_gpu` 驱动可用，GTK 就不再需要软件渲染环境；
   在此之前镜像默认值已让用户无感。
4. **镜像补应用**：GTK 终端与图形编辑器都不在镜像里，"日用"还缺这两类。
5. 菜单/文件打开的更细交互（双击打开文件、菜单项点击）本轮只验证到"菜单能打开"。
