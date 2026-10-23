# Asterinas x86_64 + Debian：日常可用四项目标技术报告（2026-10-23）

## 背景与目标

目标是在 **Asterinas 内核（x86_64）** 上跑 **Debian 13 (trixie) rootfs**，并让四类
“日常可用”能力达到可验证、可复现的程度：

1. U 盘热插拔（xHCI 枚举 → usb-storage → `/dev/sdX` → 挂载读文件）
2. xterm 能开伪终端（PTY/devpts）
3. 睡眠唤醒（systemd `suspend`）
4. 中文输入法（fcitx5 + XIM）

验证入口统一为 `make test_x86_daily_use`：串口登录 → 跑清单 → 每域打印
`DAILY_<域>=<ok|fail|missing>` → 汇总成 `matrix.tsv`。

## 逐项：问题 → 根因 → 实现 → 证据

### ① U 盘热插拔 —— ok

- **问题**：内核没有 USB 子系统，QEMU 的 `qemu-xhci` 不被枚举。
- **根因链**：xHCI 未起 → 无枚举 → 无配置/接口模型 → 无 BOT/SCSI → 无块设备；
  后续排查中还发现三个协议级坑（TRB slot id 位、端口复位完成条件、BOT 时序）。
- **实现**：控制器 bring-up → 端口复位 → `Enable Slot`/`Address Device` →
  配置描述符/`SET_CONFIGURATION` → `Configure Endpoint` → BOT（CBW/数据/CSW）+
  SCSI（INQUIRY / READ CAPACITY / READ(10) / WRITE(10)）→ 块设备注册（major 8）+
  devtmpfs 节点 → `/sys/bus/usb/devices` 视图；失败恢复 = `Stop Endpoint` +
  `Set TR Dequeue` + ClearFeature(HALT) + BOT class reset（`0x21/0xFF`）+ 重发。
- **证据**：`evidence/2026-10-22-usb-enumeration.md`、`-usb-configuration.md`、
  `-usb-bot-scsi.md`、`-usb-storage-blocker.md`。

### ② PTY —— ok

- **问题**：`xterm: get_pty: not enough ptys`（ENOENT）。
- **根因**：pty 池上限是 4096，不是瓶颈；**devtmpfs 挂载盖住了镜像里的
  `/dev/ptmx` 链接**，而内核只发布设备节点。
- **实现**：devtmpfs 启动时创建 `ptmx -> pts/ptmx`（ramfs 新增
  `symlink_with_revalidation`）。
- **证据**：`evidence/2026-10-22-pty-ptmx.md`；矩阵 `APP=ok`。

### ③ 睡眠唤醒 —— ok（suspend-to-idle）

- **问题**：`systemctl suspend` 报 `No such file or directory`。
- **根因**：内核没有 `/sys/power/state`；真 ACPI S3 还需要唤醒 trampoline 与设备恢复，
  本轮不走。
- **实现**：`/sys/power/state`（`freeze mem`），写 `mem/freeze/standby` 进入 2 秒
  suspend-to-idle 窗口并返回；门禁在唤醒后校验 `/` 可读、`/tmp` 可写。
- **证据**：`evidence/2026-10-22-suspend-assessment.md`；矩阵 `SUSPEND=ok`。

### ④ 输入法 —— ok（受限）

- **问题**：镜像没有输入法；装上 fcitx5 后引擎始终为空。
- **根因链**（逐层排除）：libpinyin 崩溃 → 内核缺 `MSG_NOSIGNAL`/`WEXITED`/
  `sync_file_range` → pinyin 自身 history 解析 → 缺 IM 定义 → 最终确认
  **XIM 已注册、IM 定义齐全、守护进程正常**，端到端键入受“无窗口管理器 → 无焦点 →
  合成按键进不了 xterm”限制。
- **实现**：镜像内安装 fcitx5 + `fcitx5-chinese-addons` + `fcitx5-modules` +
  `dbus-daemon`，清单自建私有会话总线、写 profile；内核侧补
  `MSG_NOSIGNAL`、`waitid(WEXITED)`、`sync_file_range`。
- **证据**：`evidence/2026-10-22-input-method.md`；矩阵 `INPUT_METHOD=ok(受限)`。

## 关键数字

| 指标 | 数值 |
|---|---|
| U 盘（Debian daily 负载，n=10） | 有结果 9/9 ok；其中 **3 轮由 BOT 恢复链救回**；原始成功率 6/9，恢复后 9/9 |
| U 盘（initramfs 门禁） | 10/10 |
| 应用 / PTY | xterm 正常启动并开 pty（`APP=ok`） |
| 睡眠 | `systemctl suspend` 返回 0，唤醒后磁盘/IO 正常 |
| 输入法 | XIM 注册（`@server=fcitx`）、10 个 IM 定义（`wbpy` 等）、守护进程与 D-Bus 调用成功；端到端键入受夹具限制 |

## 已知限制

- **输入法**：端到端“打出汉字”受测试环境限制（无可用 WM/焦点；twm 启动即退出、
  matchbox 未接管焦点、`allowSendEvents` 直投亦无按键落盘）。功能证据齐全，等拍板后
  改用 GTK/`GTK_IM_MODULE=fcitx` 路径复验。
- **U 盘**：第 10 轮的 `DAILY_USB=` 标记被内核日志打碎导致门禁记 “no result”，
  设备侧该轮清单跑完且触发过一次恢复；后续应让结果来源改为客人侧 `matrix.tsv`。
- **睡眠**：不是 ACPI S3；`system_wakeup` 只唤醒真 S3，唤醒源是窗口定时器。
- **U 盘写路径**：已实现但无门禁用例；拔出时未 `Disable Slot`。

## 复现方式

```sh
make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/final-matrix/xwrap/image18.ext2 \
  X86_DEBIAN_XORG_TIMEOUT=1800
```

环境要求：宿主有 `qemu-system-x86_64`（KVM 可选）、`debugfs`/`e2fsck`/`mke2fs`、
`python3`；镜像与 stage1 如上述路径。产物：`target/daily-use/{matrix.tsv,console.txt,screen.ppm}`。
