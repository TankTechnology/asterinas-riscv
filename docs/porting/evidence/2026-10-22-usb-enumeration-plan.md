# USB 增量 1：枚举并在 `/sys/bus/usb` 可见（方案与进度）

用户决定：**先交付"设备出现在 `/sys/bus/usb/devices`（含 vendor/product id）"**，
`usb-storage` 与存储类留作增量 2。本文先写方案与当前进度，实现与证据随后补。

## 当前进度（增量 0 已完成，commit `6d9a52b59`）

`kernel/comps/usb-xhci`：

- PCI 匹配 `qemu-xhci`（`1b36:000d`）/ NEC xHCI（`1033:0194`），映射 BAR0。
- 复位（`USBCMD.HCRST` / `USBSTS.CNR`）、`CONFIG`、`DCBAAP`、空命令环
  `CRCR`、一个 event ring segment（`ERSTSZ/ERSTBA/ERDP`）、`USBCMD.RUN`。
- 内核线程每 500 ms 轮询 `PORTSC.CCS`，打印 `port N connected/disconnected`。
- 门禁 `make test_x86_usb_hotplug` 已 PASS（monitor `device_add/device_del`）。

命令环与事件环**已分配但未消费**；本增量补上消费与枚举。

## 增量 1 的实现计划

### 1. 事件环消费

- 事件 TRB 16 字节：`param: u64`、`status: u32`、`control: u32`。
- 生产者周期位（cycle）从 1 开始；读事件时比较 `control & 1`；不匹配则视为空。
- 消费后写 `ERDP`（地址 + 清 `EHB` bit3）；每次读一个 TRB。
- 关注的事件类型（`control` bits 15:10）：
  - **34 = Port Status Change**：`param` 的 31:24 是端口号。
  - **33 = Command Completion**：`param` 是命令 TRB 地址；`status` 31:24 是完成码
    （1 = Success）；`control` 7:0 是 slot id。
  - **32 = Transfer Event**：`control` 7:0 = slot id、15:8 = endpoint；
    `status` 31:24 是完成码（1 = Success，13 = Short Packet）。

### 2. 命令环发送

- 16 字节命令 TRB，写完后按 4 字节写 doorbell 0（命令环）。
- 环尾是 Link TRB（type 6）指回起点；本增量用单页 256 项，简单起见每次顺序写，
  满时回绕并翻转 cycle。
- 需要的命令：
  - **Enable Slot**（type 9）：完成事件给出 slot id。
  - **Address Device**（type 11）：`param` = Input Context 的 DMA 地址，
    `control` 7:0 = slot id；分配地址 + 设 EP0。

### 3. Input/Device Context

- Input Context 一页（33 dwords）：
  - Input Control Context：`add` 标志位 0（slot）与 1（EP0）= 0b11。
  - Slot Context：`route_string=0`、`speed`（来自 `PORTSC` 13:10）、
    `root_hub_port`（Slot dword1 bits 23:16）、`context_entries=1`、
    `device_address=0`（由 Address Device 分配）。
  - EP0 Context：`EP type=4`（Control）、`CErr=3`、`max packet size`
    （低速/全速 8、高速 64、超速 512）、`TR dequeue pointer` 指向 EP0 环
    （低 4 位 `DCS=1`）、`average TRB length=8`。
- 每 slot 一页 Device Context + 一页 EP0 Transfer Ring（DMA coherent）。

### 4. 控制传输（GET_DESCRIPTOR）

- Setup Stage（type 2，`IDT=1`，`TRT`：IN=3、OUT=2、无数据=0），`status=8`。
- Data Stage（type 3，`DIR=1` 表示 IN），`status=请求长度`。
- Status Stage（type 4，`DIR=0`），带 `IOC`。
- 写 doorbell：index = slot id，target = 1（EP0）。
- 等 Transfer Event（slot 匹配、完成码 1 或 13）。
- 先读 8 字节设备描述符取 `bMaxPacketSize0`，再读 18 字节；
  `idVendor` 在偏移 8、`idProduct` 在偏移 10（小端 u16）。

### 5. `/sys/bus/usb` 可见性

- 用 `aster_device::register_legacy_bus_dir` 在 `/sys/bus` 下建 `usb`，
  其下建 `devices`。
- 每个设备一个只读目录 `usb1`（根 hub，可选）与 `<busnum>-<port>`（热插设备），
  属性：`uevent`（`PRODUCT=vvvv/pppp/dddd` 等）、`idVendor`、`idProduct`
  （4 位小写十六进制 + 换行）、`busnum`、`devnum`、`speed`。
- 设备拔出时删除目录（增量 1 只要求出现；删除随 Disable Slot 一并做）。

### 6. 门禁

`make test_x86_usb_hotplug` 扩展为：

1. 启动 `qemu-xhci`；
2. monitor `device_add usb-storage,drive=usbstick`；
3. 客体内的 `usb_probe`（挂载 devtmpfs + sysfs）轮询
   `/sys/bus/usb/devices/*/idVendor`，出现后打印
   `USBPROBE_DEVICE name=... vendor=... product=...` 与 `USBPROBE_PASS`；
4. 门禁要求串口/console 里同时有 `port 1 connected` 和 `USBPROBE_PASS`，
   并把 `idVendor/idProduct` 写进证据文档；
5. monitor `device_del` 后要求 `port 1 disconnected`（可选，保留）。

不做：配置描述符解析、接口/端点模型、usb-storage、块设备（增量 2）。

## 风险与备选

- **上下文格式易错**：Input Context 的 `add` 标志、EP0 的 `TR dequeue pointer`
  低 4 位必须是 `DCS=1`；错误会导致 Command Completion 的完成码非 1。
  备选：先用 Enable Slot 单独验证（只看到 slot id），再加 Address Device。
- **事件环 cycle 处理**：`ERDP` 的 `EHB` 不置位会重复读同一批事件。
  备选：每次消费后重写 `ERDP = 下一条 | EHB` 并检查 `IMAN` 的 `IP` 位。
- **速度判断**：`PORTSC` 13:10 的编码（1=Full、2=Low、3=High、4=Super）；
  若 QEMU 的 `usb-storage` 是高速设备，`max packet=64`。
- 若枚举在一轮内无法稳定拿到描述符：按规则写清卡点与两条替代路径
  （① 只交付"slot 已分配 + 端口可见"；② 改用控制传输的 `SET_ADDRESS` 先手
  动寻址再读描述符），继续增量 2 之外的其它工作。
