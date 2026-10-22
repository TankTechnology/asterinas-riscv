# USB 增量 1：xHCI 枚举，设备出现在 `/sys/bus/usb/devices`

**状态：完成并通过门禁 `make test_x86_usb_hotplug`（2026-10-22）。**

方案与分解见 `2026-10-22-usb-enumeration-plan.md`；这份文档记录实现、证据和踩到的坑。
增量 0（控制器 bring-up + 端口热插拔上报）见 `2026-10-22-usb-xhci-hotplug.md`。

## 实现

`kernel/comps/usb-xhci/`（新依赖：`aster-device`、`aster-systree`、`aster-util`、
`inherit-methods-macro`）：

- `src/usb.rs`（新）
  - `Ring`：一页 256 个 TRB。生产者环（命令/传输）最后一槽是 Link TRB，
    创建时就写好、每次回绕时按新 cycle 重写；事件环按大小回绕、逐条消费并在
    每次消费后写 `ERDP`（低位清零即清 `EHB`）。
  - `Enable Slot`（type 9）→ 拿到 slot id；`Address Device`（type 11）→
    Input Context（`add = 0b11`，Slot Context speed/root-hub-port/context_entries，
    EP0 Context 的 `CErr=3`/`type=4`/max packet/`TR dequeue pointer | DCS`）。
  - `GET_DESCRIPTOR` 控制传输：Setup（type 2，`IDT=1`，`TRT=3`）、
    Data（type 3，`DIR=1`，`IOC`）、Status（type 4，`IOC`），doorbell target 1；
    用 data 阶段的 residual 算实际长度，完成码 1/13 都算成功。
  - `reset_port`：写 `PORTSC.PR`，等到 `PR` 清零且 `PED|PRC` 置位（不能只看 `PR`）。
- `src/sysfs.rs`（新）：`/sys/bus/usb` + `devices`，每个设备一个目录
  （`1-1`）带 `uevent`、`idVendor`、`idProduct`、`busnum`、`devnum`、`speed`、`port`。
  通过 `aster_device::register_legacy_bus_dir` 挂进设备模型。
- `src/lib.rs`：环形队列与 slot 表成为控制器状态；轮询线程（300 ms）先清事件环
  再检查端口；端口由「未连接→已连接」时执行 `reset_port` + `enumerate_port`；
  启动时把端口状态全部记为未连接，所以开机就插着的设备也会被枚举。

`tools/usb/usb_probe.c`：挂载 devtmpfs + sysfs，轮询
`/sys/bus/usb/devices/*/idVendor`，打印
`USBPROBE_DEVICE name=1-1 vendor=… product=…` 与 `USBPROBE_PASS`。

`tools/x86/usb_hotplug_check.sh`：门禁改为要求
`port 1 connected` → `USBPROBE_DEVICE/USBPROBE_PASS` → `device_del` →
`port 1 disconnected` 四件事都出现。

## 踩到的两个坑

1. **命令/事件 TRB 的 slot id 在 control dword 的 31:24 位**，不是 7:0。
   第一版把 `Address Device` 的 slot id 写在低字节，QEMU 的 `xhci_get_slot()`
   读到 slot 0，于是完成码 **5 = TRB Error**。改为 `(slot << 24)` 后通过；
   事件侧同理（Linux 的 `TRB_TO_SLOT_ID` 就是 `(flags >> 24) & 0xff`）。
2. **端口复位不能只看 `PR` 清零**。`PR` 清掉时复位可能还没落地；要等到
   `PED` 或 `PRC` 置位，否则后续命令落在未就绪的端口上。

另外记录：QEMU 把 `usb-storage` 挂在 `qemu-xhci` 的第 1 口时协商为
**SuperSpeed**（`PORTSC` 13:10 = 4），所以 EP0 max packet 取 512；
全速/低速仍按 8、高速按 64。

## 证据（`make test_x86_usb_hotplug`）

```
xhci: controller 1b36:000d version=0.0 caplength=0x40 slots=64 ports=8 scratchpads=0 hccparams1=0x87001
xhci: port 1 connected
xhci: port 1 reset: portsc=0x221203 speed=4
xhci: device 1-1: idVendor=46f4 idProduct=0001 class=0x00 speed=4 slot=1
xhci: port 1 enumerated
USBPROBE_DEVICE name=1-1 vendor=46f4 product=0001
USBPROBE_PASS
xhci: port 1 disconnected
USB_CLIENT added=True enumerated=True removed=True disconnected=True
PASS (xHCI controller up; hot-added device enumerated under /sys/bus/usb/devices)
```

`46f4:0001` 是 QEMU 虚拟 U 盘（`usb-storage`）的 id；`1-1` 是「总线 1、端口 1」，
与 Linux 的命名一致。产物：`target/usb-hotplug/x86/console.txt`。

## 边界（留给增量 2）

- 只读设备描述符：没有读配置描述符、没有接口/端点模型、没有 `SET_CONFIGURATION`。
- 没有 `Disable Slot`：拔出后 sysfs 目录仍在（本次门禁只要求“出现”）。
- `devnum` 直接复用 slot id、`busnum` 固定为 1（单控制器场景）。
- 端口靠 300 ms 轮询，不是中断；热插最多有 0.3 s 延迟。
- 没有 usb-storage（BOT/SCSI）与块设备，所以 U 盘还不能挂载。
