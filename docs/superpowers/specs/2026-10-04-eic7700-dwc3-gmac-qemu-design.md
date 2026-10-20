# EIC7700 DWC3/GMAC QEMU 模型设计

## 目标

在不改变现有 `qemu-virt`、`sifive-u` 回归含义的前提下，增加一个可独立启动的
EIC7700 QEMU 设备组合，先完成 DWC3 探测、复位、中断和 DMA 胶水，再完成 USB
传输，最后完成 DWMAC 5.20 语义的 GMAC、PHY loopback 和时钟/复位 glue。

## 边界和版本

- QEMU 基线固定为 10.2.1；模型放在独立的 QEMU 源码工作区，不 vendoring 到
  Asterinas。
- Asterinas 仓库只保存设备树/启动参数、qtest 或 guest contract、运行器和证据。
- 现有五腿 QEMU recovery matrix 继续作为回归基线；它不能被 EIC7700 模型结果
  覆盖或降级。
- Linux EIC7700 binding/driver 作为硬件契约参考，但不复制 Linux 驱动实现。

## 方案选择

### DWC3

复用 QEMU 现有 `usb-dwc3` 的全局寄存器和嵌入式 `xhci-sysbus`，新增一个
EIC7700 wrapper。wrapper 负责两个实例的 MMIO 基址、设备树 compatible、时钟/复位
和 HSP 的最小可观察寄存器、DMA 地址窗口以及 PLIC IRQ 连接。xHCI 子设备负责已有
的控制器寄存器和后续 TRB/事件环传输。

### GMAC

单独实现 Synopsys DWMAC 5.20 兼容模型，不能以 Cadence GEM 代替。模型首先支持
寄存器复位和版本识别，再支持 TX/RX descriptor ownership、系统内存 DMA、正常/异常
IRQ、MDIO/PHY 状态和确定性 loopback。两个 EIC7700 实例分别映射到
`0x50400000` 和 `0x50410000`。

## 分阶段验收

### Phase 1：DWC3 探测、复位和中断

- USB0/USB1 映射到 `0x50480000`/`0x50490000`，每个窗口至少覆盖 `0x10000`。
- `eswin,eic7700-dwc3` DT 节点包含 `reg`、`interrupts`、`dr_mode = "host"`、DMA
  窗口和最小 clocks/resets/HSP 属性。
- qtest 读取核心 ID，验证 reset 后寄存器值、`GCTL.PRTCAPDIR`、事件寄存器清除和
  中断置位/清除；测试必须区分两个实例。
- Asterinas guest 记录设备树探测、DWC3 host 启动、xHCI 选择和恢复 nonce；没有
  传输结果时不得声称 USB 数据路径完成。

### Phase 2：USB 数据传输

- 通过 QEMU USB 测试设备或键盘/存储后端产生可重复控制和 bulk 事务。
- 验证 TRB/事件环使用系统 DMA，记录固定 payload 哈希、完成事件、IRQ 计数和错误。
- guest 失败时保留串口、QEMU 日志和运行器 JSON，禁止隐式重试。

### Phase 3：GMAC

- qtest 先验证版本、复位、descriptor ring、ownership、DMA 地址边界和 IRQ 状态机。
- guest 测试使用 PHY loopback 发送固定帧并校验接收帧哈希；另测异常 descriptor 和
  DMA 越界保护。
- 设备树/运行器明确标记 `eic7700-gmac`，不能把 SiFive GEM 结果计入 EIC7700
  GMAC 通过数。

## 证据和安全约束

每次运行保存 QEMU 版本、完整 argv、DTB 哈希、模型配置、guest 串口、模型日志和
结构化 JSON。设备模型缺失时 capability gate 必须失败。所有启动测试设置有限超时，
退出后确认 QEMU、socket 和临时串口均已清理；现有恢复矩阵的失败分类和证据保留规则
继续适用。

## 风险

- QEMU 现有 DWC3 的全局 AXI/FIFO 行为不完整；Phase 1 只实现 EIC7700 可观察 glue，
  不把未实现寄存器伪装成硬件通过。
- GMAC 没有现成的 DWMAC 5.20 模型，Phase 3 是新设备模型，需先锁定 Linux binding
  和 Asterinas `dwmac` 寄存器期望，再扩展功能。
- `/mnt/shared` 空间接近满载；QEMU 源码和构建产物放在有余量的本地目录，测试输出
  只保留摘要和必要日志。
