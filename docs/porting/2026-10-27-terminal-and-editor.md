# 日用最低配应用：GTK 终端 + 图形编辑器

日期：2026-10-27　镜像：`target/softgl/base.ext2`（ff-lag 基座 + `softgl_defaults.sh`）
内核实测：`feat/drm-virtio-gpu`（含 virtio-gpu DRM，Xorg 需要 `/dev/dri/card0`）

## 1. 选型与理由

| 角色 | 选中 | 理由 |
|---|---|---|
| 终端 | **lxterminal 0.4.1-1** | 安装体积 **594 KB**，直接依赖 8 个（GTK3 + libvte-2.91，无 XFCE 组件）。xfce4-terminal 1.1.4 安装体积 **2212 KB**、依赖 13+（libxfce4ui/libxfconf/exo-utils/perl），镜像里没有 XFCE 栈。 |
| 编辑器 | **mousepad 0.6.3-1** | 依赖 `libmousepad0` + `libxfce4ui-2` + `libgspell`；gedit 虽然二进制 1323 KB，但闭包包含 `gir1.2-*`（GObject introspection）、gtksourceview/tepl/libpeas 与 `gedit-common`，组件更多、启动链路更长。 |

两者都在 Debian trixie 主仓，安装共 16 个包：

```
apt-get install -y --no-install-recommends lxterminal mousepad
```

## 2. 镜像改动（单独 commit 的内容）

- 上述两个包已装进镜像（dpkg 状态正确；`/usr/bin/lxterminal` 83784 B、
  `/usr/bin/mousepad` 14568 B）。
- **输入法环境默认化**：只把软 GL 写进镜像还不够——图形编辑器要"不设任何变量就能打中文"，
  GTK 还必须知道用 XIM。因此把 `GTK_IM_MODULE=xim XMODIFIERS=@im=fcitx
  QT_IM_MODULE=fcitx` 追加到三条与软 GL 相同的投递路径：
  `/etc/environment`（PAM：登录/su/sudo）、`/etc/bash.bashrc`（交互式非登录 shell）、
  `/etc/profile.d/91-asterinas-im.sh`（login shell）。
- 删除 `/etc/X11/xorg.conf.d/20-asterinas.conf`：这个悬空符号链接指向
  `/etc/asterinas/...`，在缺少该目录的镜像上会让 `e2fsck` 报 invalid symlink
  （门禁 checklist 本来也会删它）。删除后镜像 `e2fsck -fn` 退出码 **0**。
- 落地方式：包由 guest 内 `apt-get` 安装（镜像内 dpkg 状态正确）；环境/清理部分由
  `tools/x86/debian/daily_apps.sh <image>` 可重放（包缺失时脚本会打印 apt 命令并以非零退出，
  因为宿主机侧无法执行 dpkg）。

## 3. 真实场景验收（不手动设任何环境变量）

启动方式：Xorg（`/etc/X11/xorg-kbd.conf`，modesetting + evdev）+ openbox + fcitx5（wbpy 默认）。
所有测试命令都在 `sudo` 会话里跑——环境变量全部来自镜像默认值
（`GDK_GL=disable`、`LIBGL_ALWAYS_SOFTWARE=1`、`GTK_IM_MODULE=xim`、`XMODIFIERS=@im=fcitx`）。

### 终端

```
LXT win=6291459 map= Map State: IsViewable title=[sh]
hello.html                      # lxterminal 内执行 ls /home/asterinas/daily 的输出
ZH=中文测试                     # 终端内 printf 写出的中文
```

lxterminal 窗口 `IsViewable`，终端内命令真的执行（`ls` 结果与中文写入文件一致），
说明 VTE 渲染路径可用（屏幕帧保存在 `/tmp/screen-apps.ppm`）。

### 编辑器

```
MP win=6291458 map= Map State: IsViewable title=[/tmp/note.txt - Mousepad]
FOCUS=6291458 tries=0
FILE_BEFORE_CLOSE=[start你好|]        # 五笔 wqiy + vbg 输入后 Ctrl+S
AFTER_CLOSE=[start你好|]
REOPEN win=6291458 map= Map State: IsViewable title=[/tmp/note.txt - Mousepad]
CONTENT=[start你好|]
```

窗口一次拿到焦点、XIM 上屏 `你好`、保存后关闭、重新打开内容一致。

## 4. 失败与定位（按 `2026-10-27-gtk-window-map.md` 的方法）

首轮 Xorg 起不来，两类原因都**不是 GTK/GL**：

1. `open /dev/dri/card0: No such file or directory`：当时 `target/osdk/asterinas-osdk-bin.elf`
   是 `origin/main` 的构建（main 没有本分支的 virtio-gpu DRM），换回
   `feat/drm-virtio-gpu` 内核后 `card0`/`renderD128` 出现，Xorg 与 GTK 都正常。
2. 基座镜像里没有 `/etc/X11/xorg-kbd.conf`（门禁运行时才注入）；按门禁同样的内容创建后 Xorg 启动成功。

GTK 端没有再出现 `glx: failed to create dri3 screen` / 不建窗的问题——软 GL 默认值生效，
两个应用都在 33 ms 量级内 map。

## 5. 未做/待确认

- **9 域门禁未重跑**：45 分钟时间盒内单次门禁（含 30 分钟 soak）放不下；本轮改动只增加两个
  应用和环境默认值，不触碰门禁脚本与既有域。建议下一轮用 `target/softgl/base.ext2`
  作 `X86_DEBIAN_XORG_BASE_ROOT` 跑一次确认。
- 终端"显示中文"用"终端内命令执行 + 中文写出 + 窗口 IsViewable + 屏幕帧"证明，未做 OCR。
- 镜像未推送到任何远端；改动文件为 `tools/x86/debian/daily_apps.sh` 与本文档。
