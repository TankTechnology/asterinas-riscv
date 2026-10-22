# USB ①.c：块设备已注册，但 bulk 传输会在若干次后停摆（卡点）

**状态：部分完成。** `/dev/sda` 已注册、能读；但持续读写若干净跑完需要解决下面这个
传输层卡点，所以「U 盘热插拔」在矩阵里仍是“部分通过”。

## 已经能用的部分

- 块设备注册：`aster_block::register` + `aster_core::device::register_hotplug_device`
  （新增的 hotplug 入口，把设备挂进 devtmpfs），major 8、minor 0 → `/dev/sda`。
- `BlockDevice::enqueue` 把每个 bio 的每个 segment 变成 READ(10)/WRITE(10)，
  直接 DMA 到 segment 的地址（`BioSegment::dma_slice().daddr()`），按 64 KiB 分块。
- 门禁 `make test_x86_usb_hotplug` 里，客体的 `read("/dev/sda")` 能读回门禁写在
  镜像开头的 `ASTERUSB`（`USBPROBE_BLOCK ... magic=4153544552555342`）。
- ext2 镜像需要 4 KiB 块（内核报 `unsupported block size` 才是 1 KiB 的问题）。
- 每日清单已改成“等待 `/dev/sda` → `mount -t ext2 -o ro` → 读 `hello.txt` → `umount`”。

## 卡点：bulk 传输在若干次之后不再产生 Transfer Event

症状（同一次运行内）：

```
xhci: bulk timeout: slot 1 dci 4 trb 0x... length 31     # 之后都是 CBW 超时
```

诊断证据：

- QEMU trace（`usb_msd_*`）：设备侧**已经把数据发出去了**
  （`usb_msd_data_in 512/512 (scsi 0)`），`usb_msd_cmd_complete status 0`，
  但随后的 CSW（`usb_msd_send_status`）没有再出现——因为客体的 CSW 读从未排队。
- 客体侧：失败的那次传输是 **data stage 的 Transfer Event 没来**；事件环在
  该下标处一直是 0（`event ring at (N, 1) next [(0,0),...]`），而之前所有事件
  都按序消费完（消费者下标与生产数一致）。
- 再往后 QEMU 报 `usb_xhci_xfer_error ... ret -3` 并
  `ep_state slotid 1, epid 4, running -> halted`，之后所有传输都无事件。
- 与 SuperSpeed 无关：`-device qemu-xhci,p2=8,p3=0`（只留 USB2 口）同样复现；
  已经按 SS companion 描述符设置 `bMaxBurst=15`，也无效。
- 触发点不确定：有时第 3 次读、有时第 9 次；加日志/加 trace 会推迟（典型竞态）。

## 两条替代路径

1. **端点恢复（推荐先做）**：把超时当作可恢复错误。
   - 发 BOT reset（类请求 `0xFE`，bmRequestType 0x21）+ Clear Feature(HALT)
     （0x02/1，wIndex = 端点地址）各一次；
   - 控制器侧 `Stop Endpoint`(type 15) → `Set TR Dequeue Pointer`(type 16，
     指向本端点的下一个空 TRB，DCS=1)，再重放这次传输；
   - 若恢复成功，卡点就变成“偶发重试”，不影响挂载。
2. **失败即重枚举**：超时时置一个标志，让端口线程对端口做一次复位 + 完整枚举，
   块设备把 slot/端点环换成新的。实现简单、语义粗暴，但挂载期间的 I/O 会失败一次，
   文件系统可能重试成功或报 EIO。

在选定路径前先做一件事：开 `usb_xhci_xfer_start,usb_xhci_xfer_*` 全量 trace，
确认失败 TRB 是否真的 `xfer_start` 了、以及 TS 上 xhci 是否收到 packet 完成回调。
这一步能区分“QEMU xhci 丢完成”与“我们的事件环同步问题”。

## 证据文件

- 本文与 `2026-10-22-usb-configuration.md`、`2026-10-22-usb-bot-scsi.md`。
- 门禁产物：`target/usb-hotplug/x86/console.txt`（含 `USBPROBE_BLOCK`）。
