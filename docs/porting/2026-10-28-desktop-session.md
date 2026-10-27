# x86 Debian 桌面会话：冷启动即用（P1）

日期：2026-10-28
范围：x86_64 Asterinas + Debian trixie 镜像上的桌面会话（壁纸/面板/桌面图标、
会话自启、输入法自启）。不含 DRM 性能线。

## 1. 结论

镜像现在可以「冷启动 → 直接看到桌面」：systemd 在 **2.2 s** 启动
`asterinas-desktop.service`，随后 Xorg、openbox、pcmanfm、lxpanel 与 fcitx5
各自就位，无需串口登录、无需手敲 `startx`。桌面上有 Asterinas 壁纸、底部面板
（应用菜单、三个启动器、任务栏、时钟）、左侧 Files/Terminal/Web Browser 图标。
面板菜单能启动应用；默认输入法为 wbpy（五笔拼音），新窗口直接可打中文，
候选窗与正文都正常渲染（WenQuanYi Micro Hei）。

复现：镜像内以 root 运行 `tools/x86/debian/daily_session.sh`。

## 2. 会话组成

| 组件 | 选择 | 说明 |
|---|---|---|
| X server | Xorg + modesetting | 以 root 启动：`/dev/dri/card0`、`/dev/input/event0` 都是 0600 root-only，镜像又没装 `Xorg.wrap`；会话本身用 `setpriv` 降到 uid 1000 |
| WM | openbox | 系统默认 `focusNew=yes`，新窗口直接获得焦点 |
| 桌面 | `pcmanfm --desktop --profile=Asterinas` | 壁纸 `/usr/share/asterinas/desktop-anime-wallpaper.png`（crop），桌面图标来自 `~/Desktop` |
| 面板 | lxpanel（`Asterinas` 配置） | 应用菜单（lxmenu-data）、browser/files/terminal 启动器、任务栏、时钟 |
| 输入法 | fcitx5 + wbpy 表引擎 | GTK3/GTK2 前端；`GTK_IM_MODULE=fcitx`，XIM 仍保留给 Xlib 客户端 |
| 字体 | fonts-wqy-microhei | 否则候选窗与中文正文是豆腐块 |

启动链：

```
graphical.target
└─ asterinas-desktop.service  (enabled, WantedBy=graphical.target)
   └─ /usr/local/sbin/asterinas-desktop      (root)
      ├─ Xorg :0 -config /etc/X11/xorg-kbd.conf
      └─ setpriv --reuid=1000 … /usr/local/bin/asterinas-session
         ├─ dbus-daemon --session            → /tmp/asterinas-session-dbus
         ├─ fcitx5 -d --replace
         ├─ openbox
         ├─ pcmanfm --desktop --profile=Asterinas
         └─ lxpanel
```

## 3. 验收证据

### 3.1 冷启动 → 桌面

`target/session/gate2/console.txt`（同一镜像、同一内核的冷启动）：

```
[  OK  ] Started asterinas-desktop.service - Asterinas Debian desktop session.
[  OK  ] Reached target graphical.target - Graphical Interface.
```

内核时间 2.2 s 完成；随后 1–2 s 内首帧上屏。冷启动后不登录、不输入任何命令，
8 s 时 QEMU `screendump` 得到 `evidence/2026-10-28-desktop-session/cold-boot-desktop.png`
（壁纸、面板、三个桌面图标、时钟 16:37）。

### 3.2 菜单启动应用

冷启动后只用鼠标：面板菜单 → Accessories → Mousepad。`pgrep -a mousepad`
返回 `137 mousepad`，截图见 `menu-launch-mousepad.png`（截图里是 Mousepad
自己的「上次未正常退出，是否恢复」对话框——它来自更早测试里被 SIGKILL 的实例，
干净镜像首次启动不会出现；这次运行同时也证明了菜单项确实拉起了进程）。

### 3.3 能打中文

冷启动后打开 GTK 客户端（yad，`GTK_IM_MODULE=fcitx`），新输入上下文直接是
**wbpy**；键入 `nihao` 后候选窗显示「1. 你好 2. 倪浩 3. 你好啊 …」
（`ime-wbpy-candidates.png`），空格选 1、回车提交，客户端读到 UTF-8 `你好`。
同一窗口的提示文字「输入测试」也正常渲染，说明 CJK 字体已生效。

### 3.4 物理键盘（而不是 XTEST）

会话接管 Xorg 时，QEMU `sendkey a/b/c/ret`（virtio-keyboard → evdev event0 →
Xorg → xterm）能把 `abc` 送进 `xterm -e 'read x; printf %s "$x" > /tmp/kbd-out'`。
注意这依赖 3.5 的 xterm XIM 关闭；未关闭时 xterm 一个字符都收不到。

### 3.5 P4：xterm 的 Xlib XIM 吞键（已修）

根因：`XMODIFIERS=@im=fcitx` 让 xterm 通过 XIM 连接 fcitx5，fcitx5 的 XIM
客户端没有把普通按键交还给 xterm，于是**连 ASCII 都打不进去**（不是只有中文）。
对照实验（同一会话、同一窗口）：

| xterm 环境 | sendkey abc + 回车 | `/tmp/kbd-out` |
|---|---|---|
| `XMODIFIERS=@im=fcitx` | 无反应 | 空 |
| `env -u XMODIFIERS` | 收到 | `abc` |
| `XMODIFIERS=@im=fcitx` + `XTerm*inputMethod: none` | 收到 | `abc` |

修复：`/etc/X11/app-defaults/XTerm` 增加 `XTerm*inputMethod: none`。代价是
xterm 不再支持中文输入——它本来也打不进去；中文输入走 GTK 客户端
（lxterminal、mousepad），它们用 fcitx 的 GTK 前端，已验证可输入「你好」。
这个资源同时让门禁的 KEYBOARD 域（xterm + `sendkey`）恢复通过。

## 4. 输入法默认引擎：两个坑

1. **表引擎按需加载导致 wbpy 被丢弃**。`table` addon 默认 `OnDemand=True`；
   fcitx5 启动时先解析 profile、后加载 addon，于是 wbpy 被视为「不存在」而从
   group 中删除，profile 被重写成只有 keyboard-us，新窗口永远停在英文。
   修复：`/usr/share/fcitx5/addon/table.conf` 的 `OnDemand=False`（系统级，
   root 跑 fcitx 的场景同样生效）。修复后 profile 保留 wbpy + keyboard-us，
   新上下文取第一项 wbpy；Ctrl+Space 可在中英之间切换。
2. **GTK 前端**。仅装 fcitx5 本体时只有 XIM 可用；装上
   `fcitx5-frontend-gtk3/gtk2` 后 GTK 客户端走 dbus 前端，候选窗与提交更稳定。
   XIM 仍保留（`XMODIFIERS`），供 Xlib 客户端使用。

## 5. XDG_RUNTIME_DIR 必须分用户

镜像原来把 `XDG_RUNTIME_DIR=/tmp/xdg` 写进 `/etc/environment`。桌面会话需要
`/tmp/xdg` 属于 uid 1000（GLib 要建 dconf/GIO 目录），但这样 root 通过 sudo
启动的 `dbus-daemon` 会拒绝这个目录（`owned by uid 1000, not our uid 0`），
门禁里 root 侧 fcitx5 因此拿不到 bus，输入法域直接失败。

现在：`/etc/environment` 不再设置；`/etc/bash.bashrc` 与
`/etc/profile.d/90-asterinas-softgl.sh` 按 uid 选择 `/tmp/xdg-root`（root）或
`/tmp/xdg`（普通用户）；`/etc/tmpfiles.d/asterinas-session.conf` 创建
`/tmp/xdg`、`/tmp/xdg-asterinas`、`/tmp/xdg-root` 并各自设对属主。

## 6. 与 9 域门禁的关系

会话接管显示与输入后，门禁里两个域的解释发生变化：

- **KEYBOARD**：门禁用 xterm + `sendkey` 验证真实键盘。3.5 修复前 xterm 的
  XIM 吞键，域名失败；修复后同一路径通过。
- **INPUT_METHOD**：门禁以 root 起自己的 fcitx5，并用 `fcitx5-remote -n` 读
  引擎。会话自己也有 fcitx（uid 1000），门禁的 `pkill -x fcitx5` 会先杀掉
  用户实例，再由 root 实例接管 XIM；配合 4 的 eager addon 与 5 的
  XDG 修复，root 实例能保留 wbpy。若 root 实例因 XIM 抢占时序仍未就绪，
  该域会报 `engine--no-text`，但产品侧的 GTK 中文输入已单独验证（3.3）。

两处都不涉及内核，属于「会话与测试脚手架如何共存」的问题，已在镜像配置层
处理；门禁的完整矩阵见 §7。

## 7. 门禁矩阵（同一镜像，9 域 + 稳定性）

见本轮运行结束后追加的 `matrix.tsv` 摘要。

## 8. 未决与限制

- **Debian 内核不在 main 上**。main 的 `kernel/src` 没有可挂载的 devtmpfs，也
  不解析 `--root-init=systemd`，Stage1 会停在 `DEBIAN_ROOTFS_FAIL
  reason=devtmpfs-mount`。因此本轮桌面验收用的是 feat/drm 内核
  （`target/session/kernel-proven.elf`）。桌面会话本身是镜像层，与内核无关；
  要把它和 Debian 一起落到 main，需要先把 devtmpfs 挂载与 root-init 接续
  移植到 main（另行立项）。
- **窗口管理器在大量窗口创建/销毁后可能卡死**：openbox 停止处理 MapRequest
  （新窗口一直 IsUnMapped、`getwindowfocus` 变成 PointerRoot），`kill -9`
  重启 openbox 后恢复。冷启动后的正常使用没有复现；长会话里的
  kill/restart 循环可以复现。疑似内核侧事件/唤醒问题，未定位。
- 壁纸/图标/面板配置来自镜像里已有的 Asterinas profile（`~/Desktop`、
  `~/.config/{pcmanfm,lxpanel}/Asterinas`），本次只负责把它们接上会话。
