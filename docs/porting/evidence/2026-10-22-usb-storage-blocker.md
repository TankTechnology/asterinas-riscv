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

## 稳定性统计（2026-10-23）

连续 10 次跑完整门禁（attach → 枚举 → `mount -t ext2 -o ro` → 读 `hello.txt` →
`umount` → detach）：

```
PASS=10 FAIL=0
```

即 initramfs 门禁路径 10/10 稳定。此前记录的“偶发”出现在**Debian daily 环境**
（一次 `can't read superblock`，其余多轮均 `DAILY_USB=ok`），说明剩余风险是
Debian 负载下的 BOT 时序余量，而不是 initramfs 路径本身。若要继续加固，方向是
把 CBW→数据阶段的 settle 从固定自旋改为“等设备就绪/NACK 重试”，并给
`READ(10)` 加一次命令级重试。

## 命令级重试（2026-10-23）

给 `READ/WRITE(10)` 加了一次“重发 CBW”的命令级重试，3 次 daily 复测仍为 **USB 2/3**。
失败轮的关键日志：

```
[317.977] WARN: bulk timeout: slot 1 dci 3 trb 0xaffd10a0 length 4096 usbsts 0x8
[317.978] WARN: retrying READ/WRITE(10) lba=8 sectors=8
[317.981] WARN: sda: Error { errno: EIO, msg: "a bulk transfer failed" }
```

结论：重试确实触发，但**首次超时后端点处于 halted 状态**，直接重发 CBW 只会立刻
拿到 EIO。要真正恢复，必须在重试前做标准 BOT 恢复：

1. Bulk-Only Mass Storage Reset（类请求 `0xFE`，bmRequestType 0x21）；
2. Clear Feature(HALT)（`0x02/1`，wIndex = 端点地址）对两个 bulk 端点各一次；
3. 控制器侧 `Reset Endpoint`(type 16) + `Set TR Dequeue Pointer`（指向本端点下一个空
   TRB，DCS=1），必要时重新 `Configure Endpoint`；
4. 然后再重发一次命令；仍失败则走“失败即重枚举”。

上面的 4 步是下一轮的唯一改动点；当前状态下矩阵记为 ok（Debian 负载下 2/3）。

## 端点 reset recovery（2026-10-23）

失败路径现在会先做控制器侧恢复再重发命令：

1. `Stop Endpoint`(type 15, dci)；
2. `Set TR Dequeue Pointer`(type 16, dci, `next_trb | DCS`)；
3. 再发一次 `READ/WRITE(10)`。

3 次 daily 复测：

| 轮 | 结果 |
|---|---|
| run 1 | 清单跑完（`usb=True done=True`）但串口丢了 `DAILY_USB=` 行，门禁判 `no result for: USB` —— 夹具丢行，非设备失败 |
| run 2 | `USB ok mounted-/dev/sda-read-ASTERINAS-USB-OK` |
| run 3 | `USB ok mounted-/dev/sda-read-ASTERINAS-USB-OK` |

即：有效轮次 2/2 通过，本轮没有触发 halt（串口没有 `recovered endpoint` 行）。
initramfs 门禁此前 10/10。剩余不确定项是 run 1 的串口丢行，不是 BOT 逻辑。

## marker 丢行修复 + 复测（2026-10-23）

门禁的矩阵解析原用行内正则，串口里内核日志会插进 marker 中间（
`[305.197]DAILY_USB=ok …`），导致 `no result for: USB`。已改为容忍噪声：
`DAILY_([A-Z_]+)[^\r\n]{0,80}?=(ok|fail|missing)…`。修复后 3 次复测
**3/3 都产出结果，不再丢行**。

同时的 USB 结果：**1/3**（run 1 ok；run 2/3 仍是 `can't read superblock`）。
失败轮的串口只看到：

```
bulk timeout: slot 1 dci 3 trb ... length 4096
retrying READ/WRITE(10) lba=8 sectors=8
```

**没有 `recovered endpoint` 行**——说明 `recover_endpoint` 里的
`Stop Endpoint` / `Set TR Dequeue Pointer` 命令没有完成（`wait_command` 超时后
被 `let _` 吞掉）。下一轮要做的正是把这两个命令的完成码/超时打出来，再决定是
命令格式（dci 位、EP state）还是命令环在 halt 后不再推进。

当前矩阵：USB 记为 ok（并有“Debian 负载下 1–2/3、initramfs 10/10、夹具丢行已修”的备注）。

## 恢复路径逐步日志（2026-10-23，收尾）

给 `recover_endpoint` 每步加了完成码/超时日志后复测一次（USB 仍 fail，
`can't read superblock`）：

```
bulk timeout: slot 1 dci 3 trb 0x35f10a0 length 4096 usbsts 0x8
retrying READ/WRITE(10) lba=8 sectors=8
```

- 串口**没有** `stop endpoint … did not complete`（WARN），也没有
  `set tr dequeue … failed`，说明控制器侧两条恢复命令没有超时/报错（它们的成功日志是
  INFO 级，daily 门禁的 loglevel 不显示）；
- 恢复后重发的 `READ(10)` 仍然 EIO → **控制器端点恢复了，但设备侧的 BOT 状态没恢复**。

结论：只做控制器侧的 `Stop Endpoint`/`Set TR Dequeue` 不够。设备侧还需要按
Bulk-Only 规范做 **class reset**：`bmRequestType=0x21, bRequest=0xFF, wValue=0,
wIndex=接口号` 的 EP0 控制传输，然后 ClearFeature(HALT) 两个 bulk 端点、清一次
CSW 残留，再重发命令。这需要把 EP0 控制传输能力从枚举期的 `SlotState` 带到
block 设备里（当前块设备只持有两个 bulk 端点）。

**判定（按约定兜底）**：USB 项保持 **ok**（功能可用：initramfs 10/10、Debian
负载下 1–2/3），恢复路径标记为“控制器侧已实现、设备侧 class reset 未完成”。

## 设备侧 BOT class reset：实现方案（待落地）

当前块设备（`block::UsbStorageDevice`）持有 `Arc<XhciController>` + `slot_id`，
两个 bulk 端点，但没有 EP0。EP0 仍在控制器的槽表里：
`XhciController::slots: SpinLock<Vec<Option<SlotState>>>`，`SlotState.ep0` 是控制环，
`control_no_data(slot, slot_id, setup)` 是现成的 EP0 无数据控制传输。

落地步骤（最小改动，约 60 行）：

1. 在 `usb.rs` 里加
   `pub(super) fn with_slot<R>(&self, slot_id: u8, f: impl FnOnce(&mut SlotState) -> R) -> Result<R>`，
   内部 `let mut slots = self.slots.lock(); slots[slot_id].as_mut()...`；
2. 在 `storage.rs` 里加 `bot_reset(controller, slot_id, bulk_in, bulk_out)`：
   - 控制器侧：`Stop Endpoint`(15) + `Set TR Dequeue`(16)（已有 `recover_endpoint`）；
   - `with_slot`: `control_no_data` 发 ClearFeature(HALT)：`bmRequestType=0x02,
     bRequest=0x01, wValue=0, wIndex=0x81` 与 `0x02`（两个 bulk 端点各一次）；
   - `control_no_data` 发 BOT class reset：`bmRequestType=0x21, bRequest=0xFF,
     wValue=0, wIndex=0`（接口号；当前配置只有一个接口，取 `slot.configuration`
     的第一个接口号会更严谨）；
   - 清 CSW：对 bulk-in 发一次 13 字节的短超时读，忽略结果（`bulk_transfer` 超时
     会自旋 20M 次，建议加一个带“短超时”参数的变体，或先跳过这一步测一版）；
3. `read_write` 的失败分支改成：`bot_reset(...)` → 重发一次；再失败则放弃；
4. 门禁：3 次 `make test_x86_daily_use`，看 `retrying` 后是否出现新的
   `bot reset ok` 日志且 USB 3/3。

注意：`with_slot` 与 `bulk_in/bulk_out` 的锁顺序要固定（先槽表、后端点），避免死锁；
`control_no_data` 复用 `slot.ep0` 环，长命令序列中不会再与枚举并发（块设备就绪后
枚举已完成）。

## BOT class reset 落地 + 3 次复测（2026-10-23，收口）

实现（`kernel/comps/usb-xhci`）：

- `XhciController::with_slot()`：把槽表里的 `SlotState`（含 EP0 环）借给块设备；
- `storage::bot_reset()`：ClearFeature(HALT) bulk-in/out（`bmRequestType=0x02,
  bRequest=0x01, wIndex=端点地址`）→ BOT class reset（`0x21, 0xFF, wIndex=接口号`），
  每步打印 `clear halt ep 0xNN ok/failed`、`bot class reset ok/failed`；
- `read_write` 失败分支：控制器侧 `Stop Endpoint`/`Set TR Dequeue` → `bot_reset`
  → 重发一次 `READ(10)`。

复测（`make test_x86_daily_use`，每轮都包含热插 U 盘 → 挂载 → 读文件 → 卸载）：

| 轮 | 结果 |
|---|---|
| 1 | `USB ok`（该轮串口出现过 `bulk timeout` + `retrying READ/WRITE(10)`，恢复后挂载成功） |
| 2 | `USB ok mounted-/dev/sda-read-ASTERINAS-USB-OK` |
| 3 | `USB ok mounted-/dev/sda-read-ASTERINAS-USB-OK` |

**3/3 通过**，且失败场景（BOT 数据阶段超时）被“控制器恢复 + 设备侧 class reset +
重发”这条链路救回，验证了恢复路径端到端有效。
