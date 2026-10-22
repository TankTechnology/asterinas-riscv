# USB ①.c：块设备 + 挂载（已解决）

**状态：完成。** 热插 U 盘 → `/dev/sda` → `mount -t ext2 -o ro` → 读文件 → `umount`，
门禁 `make test_x86_usb_hotplug` 连续 8 次干净通过（无 bulk timeout）。

## 根因

Bulk-Only Transport 的 **CBW 完成事件之后，数据阶段的 TRB 排得太早**：设备还没从
CBW 状态切到数据阶段，QEMU 的 usb-storage 把这个 packet 挂成 async 后再也没有完成它。
证据链：

- QEMU trace：`usb_msd_data_in 512/512 (scsi 0)`、`usb_msd_cmd_complete status 0`，
  但**没有** `usb_xhci_xfer_success`——xhci 侧从未完成这个 xfer；
- 客体：事件环在下标处一直是 0，data stage 的 Transfer Event 永远不来；
- 继续发下一个 CBW 时设备报 `usb_xhci_xfer_error ret -3`，随后
  `ep_state slotid 1, epid 4, running -> halted`，所有后续传输静默超时；
- 每次在 CBW 完成与数据 TRB 之间多打印一行日志（几十 µs）就几乎不复现——典型的
  设备状态机时序竞态。

## 修复

`storage::command` 在 CBW 完成之后、数据阶段之前加了一个短暂的 settle 自旋
（`20_000 × spin_loop`，几十微秒），让设备进入数据阶段；两端点锁仍覆盖整个命令。

同批修正的三处（都有独立理由）：

1. **ERDP 的 EHB 是写 1 清除**：原来写 `addr & !0xF` 永远不会清 EHB，
   控制器的 “event handler busy” 一直挂着。现在写 `addr | EHB`。
2. **删掉 doorbell 重踢看门狗**：在 async packet 未完成时重踢会让 xhci 取新 TRB、
   把旧 packet 变成孤儿，反而更容易卡死。
3. **数据与 CSW 不能链成一个 TD**：xHCI 的链式 TRB 会合成一个 USB packet，
   设备侧看到的是 `data_in 49/36`（36 数据 + 13 CSW），BOT 协议不成立。

## 门禁

`make test_x86_usb_hotplug`（客体是 initramfs 里的 `usb_probe`）：

```
USBPROBE_DEVICE name=1-1 vendor=46f4 product=0001
USBPROBE_CONFIG name=1-1 config=1 interfaces=1
USBPROBE_BLOCK dev=/dev/sda read=512 magic=4153544552555342
USBPROBE_MOUNT_READ n=17 text=ASTERINAS-USB-OK
USBPROBE_MOUNT rc=0
PASS (hot-added stick enumerated, mounted read-only, file read, unmounted)
```

U 盘镜像由门禁生成：`mke2fs -t ext2 -b 4096`（内核只支持 4 KiB 块）
+ `hello.txt`；挂载读文件后卸载，全程无 `bulk timeout`。

## 边界

- 写路径（SCSI WRITE(10)）已实现但没有门禁用例；拔盘仍未 `Disable Slot`。
- settle 是对设备状态机时序的显式让步（几十 µs/命令），不是协议必需项；
  若以后换真实硬件，应改为等设备 NAK 重试而不是固定延迟。
