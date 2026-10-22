# USB 热插拔：xHCI 控制器第一增量

任务 B 的第一块（bounded）。目标是把"插上 U 盘能被发现"这件事从零做到可验收；
完整的 USB 存储栈（枚举 + usb-storage + 块设备）是下一个增量。

## 实现（`kernel/comps/usb-xhci`）

- PCI 驱动匹配 QEMU 的 `qemu-xhci`（`1b36:000d`）与 NEC xHCI（`1033:0194`），
  映射 BAR0（MMIO）。
- 控制器初始化：读 capability 寄存器（`CAPLENGTH`、`HCSPARAMS1` 的 slot/port
  数、`HCCPARAMS1`、`RTSOFF`），`USBCMD.HCRST` 复位并等 `USBSTS.CNR` 清零，
  写 `CONFIG`（使能全部 slot）、`DCBAAP`、空命令环 `CRCR`、一个 event ring
  segment（`ERSTSZ/ERSTBA/ERDP`），最后 `USBCMD.RUN` 启动并等 `USBSTS.HCH`
  清零。
- 端口监视：内核线程每 500 ms 读每个 `PORTSC`，比较 `CCS`（当前连接状态），
  变化时打印 `port N connected` / `port N disconnected`，并清 `PORTSC` 的
  change 位。
- 命令环与事件环已经分配并编程，但本增量不使用它们（不取事件、不发命令）。

## 验收

```sh
make test_x86_usb_hotplug
```

门禁：QEMU 带 `-device qemu-xhci` 启动 → 等控制器日志 → monitor 发
`device_add usb-storage,drive=usbstick,id=stick` → 等 `port 1 connected` →
发 `device_del stick` → 等 `port 1 disconnected`。transcript：

```
xhci: controller 1b36:000d version=0.0 caplength=0x40 slots=64 ports=8 hccparams1=0x87001
xhci: port 1: empty
...
xhci: port 1 connected
xhci: port 1 disconnected
PASS (xHCI controller up; device add/remove reported on its port)
```

（`version=0.0` 来自 QEMU 该模型的 `HCIVERSION` 读数，不影响复位/端口路径；
真正的设备枚举不看这个字段。）

## 还没做的（下一个增量）

1. **设备枚举**：Port Status Change Event → `Enable Slot` → `Address Device`
   （Device Context + input context）→ `GET_DESCRIPTOR`（device/config/string）→
   `SET_CONFIGURATION`。需要事件环消费（当前只编程不读）。
2. **USB core + sysfs**：`/sys/bus/usb/devices`、`usb_device` 设备模型、
   interface/endpoint 抽象；目前内核完全没有 USB 子系统。
3. **usb-storage（BOT + SCSI 子集）**：bulk-only transport、`INQUIRY`/
   `READ CAPACITY`/`READ(10)`，接到现有块层，生成 `/dev/sdX` 并可挂载。
4. 中断（当前端口靠轮询；QEMU 的 MSI-X 已经具备，可改用中断 + 唤醒）。

## 边界

- 只识别根hub端口变化；不枚举、不分配地址、不读描述符。
- 无 `/sys/bus/usb`、无块设备、无可挂载文件系统。
- 轮询间隔 500 ms：热插拔检测有最多半秒延迟。
