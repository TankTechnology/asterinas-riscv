# Megrez USB HID：鼠标静默的两个根因

日期：2026-10-28
基线：`origin/main`（`7564b2976`）
硬件：Megrez（EIC7700）板载 xHCI + Instant USB Optical Mouse `30fa:0302`（MPS=5，report 协议下 5 字节报告）

## 1. 现象

键盘正常，鼠标在桌面里完全不动：光标不移动、按键无响应，内核日志里也**没有**
传输失败。改动前的最后一次努力把鼠标从 boot 协议切到 report 协议（为了滚轮），
但每次传输固定申请 8 字节，于是在真机上变成「一个报告都收不到」。

## 2. 根因

### 2.1 设备必须收到 `SET_IDLE` 才会发报

这只鼠标在收到 HID 类请求 `SET_IDLE`（bRequest=0x0A、wValue=(duration/4ms)<<8）
之前**完全静默**：中断传输一个都不完成，连控制 `GET_REPORT` 都超时；请求一发出去
就恢复正常上报。之前的代码只发 `SET_PROTOCOL`，从未发过 `SET_IDLE`。

### 2.2 中断传输的长度必须恰好一个包

xHCI 的中断传输只在「TD 被填满」或「收到短包」时完成，而设备每个轮询周期最多
发一个包：

| TD 长度 | 5 字节整包的结果 |
| --- | --- |
| 8 | 第二包溢出 → `BabbleDetectedError`（残余 3 = 8 − 5），crab-usb 返回 `Err`，数据在到达 ostd 队列之前就被丢弃 |
| 64 | 既不填满也不是短包 → TD 永不完成，报告静默消失，连错误都没有 |
| 5（= 端点 MPS） | 整包填满 → 完成 |

## 3. 改动

| 位置 | 改动 |
| --- | --- |
| `ostd/src/bus/usb.rs` | 新增 `set_hid_idle`，在 `SET_PROTOCOL` 之后对每个 HID 接口发 `SET_IDLE`（500 ms）；设备拒绝只告警，不弃用设备 |
| `ostd/src/bus/usb.rs` | 新增 `HidEndpoint`，每次提交的传输长度 = `interrupt_transfer_len(MPS, buffer_len)`；纯函数 + ktest `hid_transfers_ask_for_exactly_one_packet` |
| `ostd/src/bus/usb.rs` | 统一走 boot 协议（3 字节报告 < MPS → 短包即完成），不再尝试 report 协议；滚轮能力暂时放弃 |
| `ostd/src/bus/usb.rs` | 打开设备时记录端点 MPS 与 interval，便于现场判断「传输该发多长」 |
| `kernel/comps/usb/src/arch/riscv/mod.rs` | 事件环中断之外的 4 ms 定时轮询兜底（`PollWakeup`），并且**只在中断确实不投递时才轮询**：探针等到第一个中断就把定时器降级为 1 s 看门狗，之后每轮唤醒从 250 次/s 降到约 1 次/s |

`kernel/comps/usb/src/mouse.rs` 的 `HidBootMouse::decode` 本来就会在报告短于描述符
布局时回落到 `BOOT_MOUSE_LAYOUT`，所以强制 boot 协议后滚轮字段被忽略而不会误动。

## 4. 验证

真机：Megrez 桌面（Xorg + matchbox）光标可移动、可点击；此前版本完全无反应。

QEMU（`tools/riscv/xhci/input_gate.py`，Sv39 + smp4 + `qemu-xhci,msi=off,msix=off`
+ 精确 8 条键盘 / 7 条指针 evdev 事件断言）：

```bash
tools/riscv/xhci/build_input_gate.sh target/riscv-xhci-input/initramfs.cpio
cargo osdk build --target-arch riscv64 --scheme riscv --features=riscv_sv39_mode
ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
ASTERINAS_INITRAMFS="$PWD/target/riscv-xhci-input/initramfs.cpio" \
QEMU_UBOOT_PROFILE=generic-sv39-ltp-smp4 \
QEMU_UBOOT_OUT_DIR="$PWD/target/qemu-uboot/xhci-input" \
QEMU_UBOOT_BUILD_DIR="$PWD/target/qemu-uboot/cache/u-boot-build" \
  tools/riscv/prepare_qemu_uboot_booti.sh prepare
timeout 240s python3 tools/riscv/xhci/input_gate.py \
  --uboot target/qemu-uboot/cache/u-boot-build/u-boot \
  --boot-disk target/qemu-uboot/xhci-input/boot.ext4 \
  --manifest target/qemu-uboot/xhci-input/artifacts.json \
  --serial-log target/riscv-xhci-input/serial.log \
  --result target/riscv-xhci-input/result.json --smp 4
```

结果：`passed: true`，`cleanup: complete`，键盘 8 条、指针 7 条事件逐条匹配；
串口中没有 `Babble`、没有传输失败重试、没有 `refused SET_IDLE`。

## 5. 边界

QEMU 的 `usb-kbd`/`usb-mouse` 不发 `SET_IDLE` 也照常上报，因此**这两条根因在 QEMU 上
复现不出来**：门禁只能证明改动没有破坏输入链路，`SET_IDLE` 的必要性只有真机能证明。
鼠标滚轮仍然不可用（需要 report 协议 + 正确的单包传输长度，属于后续工作）。
