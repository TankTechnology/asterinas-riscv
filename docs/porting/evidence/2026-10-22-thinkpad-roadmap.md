# ThinkPad 线：剩余四项的总计划（2026-10-22）

用户定的顺序与门禁（每项完成即更新 `daily-use-matrix.md` 对应行 + 证据文档 + commit，
全部推送 `feat/drm-virtio-gpu`；单项卡超过 2 天写卡点与两条替代路径后跳到下一项）。

## ① USB 增量 2：usb-storage → `/dev/sdX` 可挂载

分三段交付，每段都有可运行门禁：

- **①.a 配置描述符 + 接口模型 + `SET_CONFIGURATION`**
  - 读配置描述符（先 9 字节取 `wTotalLength`，再读全量），解析 interface（type 4）
    与 endpoint（type 5）。
  - `SET_CONFIGURATION`（无 data 阶段的控制传输）。
  - sysfs 增加 `bConfigurationValue`、`bNumInterfaces`、`endpoints` 摘要；
    串口打印 `config N: interfaces=…`、`interface …class/subclass/protocol`、
    `endpoint … bulk in/out maxpacket`。
  - 门禁：`make test_x86_usb_hotplug` 增加这三类标记（QEMU 盘是
    class 08/06/50、bulk 双向）。
- **①.b BOT + SCSI 只读**：Configure Endpoint（type 12）为 bulk 端点建 EP Context
  与传输环；CBW/CSW；`INQUIRY`、`TEST UNIT READY`、`READ CAPACITY(10)`。
  门禁：串口打印厂商/型号与容量（扇区数），与镜像大小一致。
- **①.c 块设备 + 挂载**：把 SCSI 读接到块层，注册 `/dev/sda`（major 8）；
  daily 清单 `dd if=/dev/sda` 读首扇区、`mount -t ext2`、`cat` 一个文件、`umount`。
  门禁：`daily-use-matrix.md` 的「U 盘热插拔」从“部分通过”变 **ok**。

## ② pty 池上限

矩阵「应用」fail 的报错是 `xterm: get_pty: not enough ptys`。先测：devpts 的
`max`/`nr`、内核 `MAX_PTY_NUM`（或等价上限）、以及 devpts 挂载参数。
- 门禁：daily 清单的应用行起得来 xterm 并有 X 窗口；矩阵该行变 ok。

## ③ 睡眠唤醒

补 `/sys/power/state`（`mem`）与 ACPI S3：写 state → 保存设备状态 → 发 `_PTS`/
`SLP_TYP` → 唤醒后恢复。QEMU 侧 `system_wakeup` 已由 daily 门禁接好。
- 门禁：矩阵「睡眠唤醒」变 ok，唤醒后网络 (`curl` 或 ping 宿主 slirp) 与磁盘
  (`fsck`/读文件) 仍可用。

## ④ 输入法

rootfs 装 `fcitx5` + `fcitx5-chinese-addons`，XIM/`GTK_IM_MODULE` 配置，
用 `xterm` + `xdotool`/`xte` 输入拼音并截图验证候选词。
- 门禁：矩阵「输入法」从 missing 变 ok（中文上屏）。

## 当前状态（2026-10-22 晚）

- **① USB**：
  - 增量 1（枚举到 `/sys/bus/usb`）：`2026-10-22-usb-enumeration.md`，PASS。
  - ①.a 配置描述符/接口/`SET_CONFIGURATION`：`2026-10-22-usb-configuration.md`。
  - ①.b BOT + SCSI INQUIRY/READ CAPACITY：`2026-10-22-usb-bot-scsi.md`。
  - ①.c 块设备 `/dev/sda` + 挂载：**完成**（CBW→数据阶段 settle、ERDP/EHB 修正），
    门禁 8/8 通过，矩阵 USB 行 = ok；见 `2026-10-22-usb-storage-blocker.md`。
- **② pty**：完成，矩阵「应用」= ok（`2026-10-22-pty-ptmx.md`）。
- **③ 睡眠唤醒**：未实现，评估与两条路径见 `2026-10-22-suspend-assessment.md`。
- **④ 输入法**：fcitx5 安装/XIM 端到端测试已写进 daily 清单，门禁在跑；
  结果与证据见 `2026-10-22-input-method.md`。
- 矩阵：`docs/porting/daily-use-matrix.md`。
