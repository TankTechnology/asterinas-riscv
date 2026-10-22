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

## 两条替代路径

1. **内核补齐两个 syscall 选项**：`send/recv` 支持 `MSG_NOSIGNAL`（或至少不告警
   并按无该标志处理）、`waitid` 支持 `WEXITED`。然后复跑，若仍崩，在
   `do_exit`/信号投递处打印 faulting signal + 用户态 PC，定位 libpinyin 的崩溃点。
2. **绕开 libpinyin**：
   - 试 `fcitx5-table` 路线（`table.conf` 已加载）：装 `fcitx5-table-extra`，
     用拼音/五笔码表引擎，不碰 libpinyin；
   - 或按 rootfs profile 预置一个在本内核上验证过的 fcitx5（本地 deb + `dpkg -i`
     打进镜像），避免运行时 apt 的不确定性。

## 下一步（拿到引擎之后才有意义的验证）

`fcitx5-remote -n` 返回 `pinyin` 后，清单里的 XIM 端到端用例（xterm + xdotool
打 `nihao` + 空格选词，检查 `/tmp/xim-out` 里出现“你好”）就会给出 ok/fail 的
确定结论。
