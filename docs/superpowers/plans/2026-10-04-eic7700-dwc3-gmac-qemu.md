# EIC7700 DWC3 与 GMAC QEMU 模型实施计划

> **执行约束：** 先完成并验证 Phase 1，再进入 Phase 2；GMAC 作为独立 Phase 3，任何阶段都不改变现有 QEMU recovery matrix 的通过判定。

## 0. 建立独立 QEMU 基线

**目标：** 在有余量的本地目录获取并固定 QEMU 10.2.1，确认构建和 qtest 能运行。

**步骤：**

1. 创建 `/home/ubuntu/.codex/qemu-eic7700-10.2.1` 工作区，检查 tag、子模块和 SHA256。
2. 用现有开发容器工具链配置 `riscv64-softmmu`、USB、网络和 qtest；避免修改 Asterinas 的 Cargo/target 产物。
3. 记录 `configure` 参数、编译器、QEMU 版本和可用设备清单。
4. 运行一个不带 EIC 模型的基线 qtest/`qemu-system-riscv64 -M virt`，确认后续失败能归因于模型。

**验证：** 版本输出、可执行文件哈希、基线 qtest 结果和磁盘占用记录在 `target/eic7700-qemu/phase0/`。

## 1. Phase 1 DWC3 wrapper

**目标：** 两个 EIC7700 DWC3 实例可被设备树和 guest 探测，reset、GCTL、事件寄存器和 IRQ 可测试。

**步骤：**

1. 在 QEMU `hw/usb/` 增加 EIC7700 DWC3 wrapper，实例化现有 `usb-dwc3`/`xhci-sysbus` 子设备。
2. 添加 USB0/USB1 的 MMIO、IRQ、DMA 属性和复位回调；默认 host mode，未支持的 global AXI/FIFO 位读写保持明确的保留值。
3. 增加最小 EIC7700 clock/reset/HSP register bank，保存 enable/reset 状态，并在 reset 时恢复硬件默认值。
4. 添加 machine 或 `-device` 入口和 DTB 生成/修补逻辑，节点 compatible 使用 `eswin,eic7700-dwc3`。
5. 写 qtest：reset/ID/GCTL、event clear、IRQ assert/deassert、两个实例隔离、DMA 地址窗口。
6. 在 Asterinas 运行器中增加 `eic7700-dwc3` capability leg，验证 guest 探测、host 启动和 nonce recovery。

**验证：** qtest 全部通过；guest serial 出现两阶段 marker；缺少模型时 capability gate 失败；现有五腿矩阵结果不变。

## 2. Phase 2 USB 传输

**目标：** 通过 Phase 1 wrapper 的 xHCI 子设备完成可重复 USB 控制/bulk 传输。

**步骤：**

1. 接入确定性 USB test device，同时保留键盘/存储后端作为兼容检查。
2. 校验 DCBAA、event ring、TRB 的 DMA 地址经过 EIC wrapper 的窗口转换。
3. 添加 control、bulk IN/OUT、短包和设备复位测试，记录 payload SHA-256、完成事件和 IRQ 计数。
4. 添加 Asterinas guest 测试和超时/失败分类，禁止把 PCI xHCI 结果标为 EIC7700 DWC3。

**验证：** qtest + guest 传输结果 JSON；重复运行结果一致，退出后无 QEMU 残留。

## 3. Phase 3 EIC7700 GMAC

**目标：** 实现最小可用 DWMAC 5.20 语义和两个 EIC7700 GMAC 实例。

**步骤：**

1. 建立 DWMAC 5.20 register model，包含 MAC/DMA reset、version、状态和中断寄存器。
2. 实现 TX/RX descriptor ring：ownership、长度、地址、链表/环回、DMA 读写和边界检查。
3. 实现正常/异常 IRQ、状态清除、MDIO 寄存器和确定性 PHY loopback。
4. 添加 EIC7700 clock/reset/HSP glue，并映射 `0x50400000`/`0x50410000` 及 DT interrupts。
5. 写 qtest descriptor/IRQ/MDIO/loopback 测试，再接入 Asterinas `dwmac` guest 收发包验证。
6. 将 capability inventory 标记为 `eic7700-gmac`，与 SiFive GEM 分开统计。

**验证：** 固定帧的 TX/RX 哈希、IRQ 次数和错误路径 JSON；现有 VirtIO/GEM 回归继续通过。

## 4. 收尾和审计

1. 对每个阶段运行 `git diff --check`、QEMU 相关 qtest、Asterinas Python contract tests 和现有 recovery matrix。
2. 保存模型 commit、QEMU SHA、DTB/argv 哈希、测试摘要，清理失败运行的临时进程和 socket。
3. 做 requirement-by-requirement audit：未覆盖的 EIC7700 时钟、PHY 或真实板行为明确标注为未验证。
