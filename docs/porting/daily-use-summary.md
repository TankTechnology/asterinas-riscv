# 日常可用四项目标：总结（2026-10-23）

一次可复现的收口总结。每项写“做了什么 / 硬证据 / 已知限制”。

## ① U 盘热插拔 —— ok

**做了什么**：xHCI 控制器 bring-up → 端口复位 → `Enable Slot` → `Address Device` →
配置描述符/`SET_CONFIGURATION` → BOT（CBW/数据/CSW）+ SCSI（INQUIRY、READ CAPACITY、
READ(10)）→ 块设备注册（major 8）与 devtmpfs 节点 → `/sys/bus/usb/devices` 视图。

**硬证据**：`make test_x86_usb_hotplug`（客体把热插 U 盘 `mount -t ext2 -o ro`、读
`hello.txt`、`umount`，还校验首扇区 magic）；Debian daily 门禁里
`DAILY_USB=ok mounted-/dev/sda-read-ASTERINAS-USB-OK`。

**已知限制**：BOT 在 CBW 与数据阶段之间需要 settle（几十 µs），Debian 环境偶发
`can't read superblock`；未做拔出时的 `Disable Slot`；写路径有实现但无门禁。

## ② pty —— ok

**做了什么**：devtmpfs 启动时发布 `/dev/ptmx -> pts/ptmx`（ramfs 新增
`symlink_with_revalidation`）。根因不是 pty 数量（上限 4096），而是 devtmpfs 挂载盖住了
镜像里的链接。

**硬证据**：daily 矩阵 `DAILY_APP=ok xterm`。

## ③ 睡眠唤醒 —— ok（suspend-to-idle）

**做了什么**：`/sys/power/state`（`freeze mem`），写 `mem/freeze/standby` 进入 2 秒
挂起窗口并返回 0；daily 的 SUSPEND 域在唤醒后验证 `/` 可读、`/tmp` 可写。

**硬证据**：`DAILY_SUSPEND=ok woke-up-disk-and-io-ok`；内核日志
`pm: suspend-to-idle: entering/resumed`。

**已知限制**：不是 ACPI S3。QEMU 的 `system_wakeup` 只唤醒真 S3；本内核没有唤醒
trampoline 与设备恢复。接 S3 时接口不变，只换后端。

## ④ 输入法 —— ok（受限）

**做了什么**：镜像内安装并启动 fcitx5 + `fcitx5-chinese-addons`（补充
`fcitx5-modules`、`dbus-daemon`），清单自建私有会话总线，写 fcitx5 profile；
为跑通它还补了三个内核缺口（见下）。

**硬证据**：`XIM_SERVERS(ATOM) = @server=fcitx`（XIM 前端已注册）；
`/usr/share/fcitx5/inputmethod/` 10 个 IM（`wbpy`/`wbx`/`shuangpin`/`pinyin`…）；
fcitx5 存活，`fcitx5-remote -o` 与 `SetCurrentIM` 返回 0；
`MSGTEST_PASS`（`send(MSG_NOSIGNAL)`→EPIPE、`waitid(P_PID,…,WEXITED)`）、
`iotest` 基础文件 I/O 全绿。

**已知限制**：端到端“打字出汉字”受测试环境限制——会话无可用窗口管理器
（matchbox 未接管焦点、twm 启动即退出），xterm 即使 `allowSendEvents` 也收不到合成
按键；`fcitx5-remote -n` 是按输入上下文返回的，无 IC 时为空。等用户拍板后再做
GTK/`GTK_IM_MODULE=fcitx` 或真实焦点路径的端到端验证。

## 本轮补的内核缺口（对输入法栈是前提）

| 缺口 | 处理 |
|---|---|
| `MSG_NOSIGNAL` | socket 层接受该 flag（本就不投递 SIGPIPE） |
| `waitid(WEXITED)` | 作为默认语义接受 |
| `sync_file_range`（x86 #277 / generic #84） | 实现为参数校验 + 成功（dpkg/apt 不再重试死循环） |

## 门禁用法与最近一次结果

```sh
make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/final-matrix/xwrap/image18.ext2 \
  X86_DEBIAN_XORG_TIMEOUT=1800
```

最近一次（2026-10-23）结果：

```
DESKTOP ok  APP ok  APT ok  CLIPBOARD ok
INPUT_METHOD ok(受限)  AUDIO ok  USB ok  SUSPEND ok
```

`matrix.tsv`、串口全量与截图在 `target/daily-use/`。
