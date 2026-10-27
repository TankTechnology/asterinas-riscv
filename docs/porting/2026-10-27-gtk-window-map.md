# GTK 窗口为什么不 map：与 xterm 的 map 失败是否同源

日期：2026-10-27　镜像：`target/daily-kbd2`（gate 基础镜像 + 门禁安装的 Xorg/evdev/yad/fcitx5）

## 1. 结论先行

两者**不同源**：

| 客户端 | 旧现象 | 根因 | 修复后 |
|---|---|---|---|
| xterm（Xt/Xlib） | 窗口 `IsUnMapped` | pty slave 打开后没有自动取得控制终端 → 子 shell 失败 → xterm 走错误路径 | `058ae1515` 后 **28 ms** map（`IsViewable`） |
| yad（GTK3） | 窗口 `IsUnMapped`，甚至完全不建窗 | **GDK 初始化 GL 时选中 Mesa 的 `virtio_gpu` 驱动并失败**，GTK 在 realize 之前就停住 | 加软件 GL 环境后 **33 ms** map，且能拿焦点、XIM 上屏 `你好` |

MapNotify/焦点/WM 都不是原因：Xlib 与"GTK + 软件 GL"都在 ~30 ms 内 map 成功。

## 2. 最小对照

同一个 Xorg（`xorg-kbd.conf`，modesetting）、同一个 openbox（`_NET_SUPPORTING_WM_CHECK`
已设置）、同一台 guest：

```
DIAG A win=6291468 now=IsViewable
DIAG A viewable_after_ms=28          # xterm，Xlib/Xt
DIAG B pid=118 win= now=missing      # yad，普通环境
DIAG B syscall=202                   # 唯一线程在 futex
DIAG B tree=                         # xwininfo -root -tree 里连窗口都没有
DIAG C win=4194306 now=IsViewable    # yad + 软件 GL 环境
DIAG C viewable_after_ms=33
```

普通环境 yad 的 stderr：

```
glx: failed to create dri3 screen
failed to load driver: virtio_gpu
```

单线程、futex 等待、root tree 里无窗口、30 s 内不出现 —— 说明它不是"map 请求发了但
没人处理"，而是 GDK 在 GL/EGL 探测阶段就失败并停在主循环里，根本没走到
`gdk_window_show()`。

## 3. 为什么只有 GTK 中招

Xorg 自己是用 `MESA_LOADER_DRIVER_OVERRIDE=kms_swrast` +
`LIBGL_ALWAYS_SOFTWARE=1` 起的（门禁一直这样），所以 glamor 用软件路径；但
**客户端进程不继承 Xorg 的环境**。xterm 走纯 Xlib/Xt，不碰 GL，所以不受影响；
GTK3 默认尝试 GL，Mesa 在本镜像里选 `virtio_gpu`（DRM 栈还不支持），失败。

## 4. 最小修复

GTK 客户端在启动时带上软件渲染环境即可：

```
LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe GDK_GL=disable
GDK_BACKEND=x11 NO_AT_BRIDGE=1 XDG_RUNTIME_DIR=<0700 dir>
```

门禁 checklist 的 yad 路径已改为：APT 域安装 `yad`；启动 yad 前导出上述变量；
先等 `xwininfo` 报 `IsViewable`，再 `xdotool windowfocus` 并**校验**
`getwindowfocus == 窗口 id`（无 WM 时 `windowactivate` 不可用），然后才发
`DAILY_KEY_WAIT`/输入五笔。

验收（同一 VM）：

```
ACC win=6291458 map= Map State: IsViewable focus=6291458 tries=0
ACCOUT=[你好]
```

## 5. 对所有 GTK 应用意味着什么

在这套镜像/内核上，**任何 GTK3 应用都需要同样的软件渲染环境**，否则会在 GL
探测阶段失败、连窗口都不出现（不只是 yad；`eog`/`pcmanfm` 等早前能起窗是因为
它们跑在 DRM 桌面剖面里，X 会话环境里带了 Mesa 覆盖）。要彻底去掉这个限制，
需要让 Mesa 的 `virtio_gpu` 驱动在本内核的 DRM 栈上可用（属于 DRM/GL 栈工作，
不在本轮内核待办里）；在此之前，把上述变量写进镜像的会话默认值
（`/etc/environment` 或 Xsession.d）即可让所有 GTK 应用恢复正常。

## 6. 纪律说明

未改 ostd、未新增 unsafe、未 push。第 7 项（fork 页表整页复制）按红线维持
"设计已写、不实现"。
