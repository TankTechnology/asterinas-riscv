# USB 增量 2 / ①.a：配置描述符、接口模型与 `SET_CONFIGURATION`

**状态：完成，门禁 `make test_x86_usb_hotplug` PASS（2026-10-22）。**

这是 `2026-10-22-thinkpad-roadmap.md` 里 ① 的第一段。增量 1 只读到设备描述符，
这一段把设备配置起来，并解析出 ①.b 需要的 bulk 端点。

## 实现（`kernel/comps/usb-xhci/src/usb.rs`）

- 控制传输拆成两个通用原语：
  - `control_in`：Setup（`TRT=3`）→ Data（`DIR=1`，`IOC`）→ Status，按 data 阶段
    residual 计算实际长度；
  - `control_no_data`：Setup（`TRT=0`）→ Status（`DIR=1`，`IOC`）。
- `read_configuration`：先 `GET_DESCRIPTOR(Configuration, 0, 9)` 取 `wTotalLength`
  （偏移 2），再读全量；`parse_configuration` 按描述符链走
  （type 2 配置 / type 4 接口 / type 5 端点），跳过不认识的类型。
- `set_configuration`：`SET_CONFIGURATION`（bmRequestType 0x00、bRequest 9、
  `wValue = bConfigurationValue`），并打印 `config N: interfaces=… selected`。
- `SlotState` 保存解析后的 `Configuration`，供下一步配置端点使用。
- sysfs 增加 `bConfigurationValue`、`bNumInterfaces`、`endpoints`
  （每行 `81:bulk in maxpacket=1024`）。

## 门禁

`tools/usb/usb_probe.c` 额外打印 `USBPROBE_CONFIG name=… config=… interfaces=…`；
`tools/x86/usb_hotplug_check.sh` 现在要求四个标记齐全才允许拔出并判 PASS：

- `config 1: interfaces=1 selected`
- `interface 0: class=0x08`
- `endpoint 81:bulk in`
- `endpoint 02:bulk out`

## 证据

```
xhci: port 1 reset: portsc=0x221203 speed=4
xhci: interface 0: class=0x08 subclass=0x06 protocol=0x50 endpoints=2
xhci: endpoint 81:bulk in maxpacket=1024
xhci: endpoint 02:bulk out maxpacket=1024
xhci: config 1: interfaces=1 selected
xhci: device 1-1: idVendor=46f4 idProduct=0001 class=0x00 speed=4 slot=1
USBPROBE_CONFIG name=1-1 config=1 interfaces=1
USBPROBE_DEVICE name=1-1 vendor=46f4 product=0001
USB_CLIENT added=True enumerated=True configured=True removed=True disconnected=True
PASS (hot-added device enumerated and configured; bulk endpoints published)
```

`class=08 subclass=06 protocol=50` 正是 USB Mass Storage / SCSI transparent /
Bulk-Only Transport，两个 bulk 端点各 1024 字节（SuperSpeed）。

## 边界（下一步 ①.b）

- 端点上下文还没建（没有 `Configure Endpoint`），所以还不能发 bulk 传输。
- 没有读字符串描述符（厂商/型号名）。
- 拔出时仍未 `Disable Slot`。
