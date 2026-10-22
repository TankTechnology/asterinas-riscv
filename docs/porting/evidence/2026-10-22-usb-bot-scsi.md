# USB 增量 2 / ①.b：Bulk-Only Transport + SCSI 识别

**状态：完成，门禁 `make test_x86_usb_hotplug` PASS（2026-10-22）。**

这一段的门禁从"配置好了"推进到"能通过 bulk 端点跟设备说话"：读到了 U 盘的
厂商/型号与容量。

## 实现

`kernel/comps/usb-xhci/src/usb.rs`：

- `configure_endpoints`：为配置里的每个端点建传输环（一页 256 TRB，带 Link），
  在 Input Context 的 `8*(dci+1)` dword 处填 EP Context（`CErr=3`、EP type
  bulk in=6 / bulk out=2、max packet 1024、`TR dequeue pointer | DCS`），
  slot context 的 `context_entries` 提到最大 DCI，然后发 `Configure Endpoint`（type 12）。
- `bulk_transfer`：Normal TRB（type 1，`IOC`），按 slot 写 doorbell、值写 DCI，
  等 Transfer Event 并用 residual 求实际长度。

`kernel/comps/usb-xhci/src/storage.rs`（新）：

- BOT：CBW（31 字节，"USBC"、tag、传输长度、方向、LUN、CB 长度、16 字节 CB）
  → 数据阶段（仅 mass-storage 接口做）→ CSW（13 字节，"USBS"、tag、residue、status）。
- SCSI：`INQUIRY`（36 字节，厂商在 8、型号在 16）、`READ CAPACITY(10)`、
  `REQUEST SENSE`。
- **Unit Attention 处理**：端口复位后的第一条非 INQUIRY 命令会返回
  `sense key=6 / ASC=0x29`（POWER ON OR RESET）；读一次 sense 把它清掉后重试一次。

## 三个坑

1. **doorbell 的索引是 slot id、值才是端点号（DCI）**。第一版写成
   "索引 = DCI、值 = 0"，结果 bulk 传输永远等不到事件（EP0 那条
   `doorbell(slot, 1)` 恰好一直是对的，所以枚举没暴露这个问题）。
2. **Unit Attention**：复位后 `READ CAPACITY` 第一次必然失败，需要读 sense 再重试。
3. Configure Endpoint 的 Input Context 里，`add` 标志要含端点位（`1<<dci`），
   slot context 的 `context_entries` 要覆盖最大 DCI，否则端点不会真正启动。

## 证据

```
xhci: configured 2 endpoints (bulk in: true, bulk out: true)
xhci: sense key=6 asc=0x29 ascq=0x00
xhci: storage on slot 1: vendor="QEMU" product="QEMU HARDDISK" blocks=32768 block_size=512 capacity=16777216 bytes
USB_CLIENT added=True enumerated=True configured=True removed=True disconnected=True
PASS (hot-added device enumerated, configured, and its capacity read over bulk transfers)
```

`32768 × 512 = 16 MiB`，与门禁创建的 `truncate -s 16M usbstick.img` 一致；
`vendor="QEMU"` 是 INQUIRY 的标准数据。

## 边界（①.c）

- 还只是"读了一次容量"：没有块设备节点、没有 READ(10)、不能挂载。
- 拔出时仍未 `Disable Slot`；错误恢复只覆盖 Unit Attention 一种。
