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

1. **把引擎查询本身打通**（已完成 syscall 部分）：用准确的返回码与调试输出复跑
   （当前诊断里的 `remote_rc` 取的是 `echo` 的返回值，需改成先存
   `engine=$(fcitx5-remote -n); rc=$?`），并尝试 `fcitx5-remote -o/-t/-r`；
   如果 `-n` 仍为空，用 `dbus-send --session --dest=org.fcitx.Fcitx5` 直接查询
   InputMethod 属性，区分“总线没连上”与“守护进程没有输入法组”。
2. **绕开 libpinyin**：
   - 试 `fcitx5-table` 路线（`table.conf` 已加载）：装 `fcitx5-table-extra`，
     用拼音/五笔码表引擎，不碰 libpinyin；
   - 或按 rootfs profile 预置一个在本内核上验证过的 fcitx5（本地 deb + `dpkg -i`
     打进镜像），避免运行时 apt 的不确定性。

## 下一步（拿到引擎之后才有意义的验证）

`fcitx5-remote -n` 返回 `pinyin` 后，清单里的 XIM 端到端用例（xterm + xdotool
打 `nihao` + 空格选词，检查 `/tmp/xim-out` 里出现“你好”）就会给出 ok/fail 的
确定结论。
