# Asterinas PowerVR IRQ 唤醒（Megrez P2.4，2026-10-22）

## 结论

- Megrez 真机上，Asterinas 注册了 GPU 的专用 PLIC 中断（DT `interrupts = <15>`，
  `interrupt-parent` phandle `0x10`），并在一次 `HEALTH_CHECK` 提交中收到
  固件事件：`ASTERINAS_POWERVR_IRQ count=1 spurious=0 last_status=0x1`；
- KCCB 完成等待由中断唤醒：
  `FW_KCCB status=completed slot=0 rtn=0 write_off=1 read_off=1 wake=irq irq_count=1`；
- P2.1–P2.3 的既有 marker 全部保持不变（OSCONFIG/CONN/META/COMPAT 原样输出）；
- **P2.4 门禁通过**。

## Discovery：RockOS 参考栈的 RGXIRQ 路径

1. **DT 契约**：vendor `eswin-win2030-die0-soc.dtsi` 的 `gpu@51400000`
   节点为 `interrupt-parent = <&plic0>; interrupts = <15>;`，
   PLIC 节点 `#interrupt-cells = <1>`。板级实测解码结果：
   `source=15 parent=0x10`。
2. **Linux 注册链**：`SysInstallDeviceLISR()` →
   `tc_set_interrupt_handler()` / `tc_enable_interrupt()` →
   `TCInterruptHandler()` → `RGX_LISRHandler()`。
3. **中断确认**：`RGX_LISRHandler` 调用 `RGXAckIrqDedicated()`：
   - 读 `RGX_CR_IRQ_OS0_EVENT_STATUS`（offset `0x0BD0`，source bits `[1:0]`）；
   - 若 `status & 0x3 != 0`，把 `status & 0x3` 写回
     `RGX_CR_IRQ_OS0_EVENT_CLEAR`（offset `0x0BE0`）；
   - `RGX_CR_MTS_GPU_INT_STATUS`（offset `0x0BB0`）只在调试 dump 中使用。
4. **不存在主机侧 `RGX_CR_MTS_INT_EN` 门控**：本 native 构建里
   `RGX_CR_MTS_DM0..7_INTERRUPT_ENABLE` 由固件初始化维护（vendor 只读
   `DM4_INTERRUPT_ENABLE` 判断 AutoVz 固件是否在跑），不是主机使能 IRQ 的
   寄存器。主机只需注册 PLIC 线并按 OS0 event status/clear 确认。
5. **结论**：GPU 的“MTS 中断”在本平台体现为专用 host event 线 + OS0
   event status/clear；`RGX_CR_MTS_GPU_INT_STATUS` 是诊断量，不是 ack 寄存器。

## 关键串口收据（P2f）

```text
ASTERINAS_POWERVR_OWNER status=ready bvnc=30.3.408.101 crg_restored=1 firmware=untouched render=unavailable
ASTERINAS_POWERVR_OWNER session=opened bvnc=30.3.408.101 gates=0b111 deasserted=0b11111
ASTERINAS_POWERVR_DMA_STAGE segment=0 bytes=52064 pages=13 daddr=0xdf310000 cpu_readback=ok gpu_visibility=unverified
ASTERINAS_POWERVR_DMA_STAGE segment=1 bytes=18432 pages=5  daddr=0xdf308000 cpu_readback=ok gpu_visibility=unverified
ASTERINAS_POWERVR_DMA_STAGE segment=2 bytes=73312 pages=18 daddr=0x81ac0000 cpu_readback=ok gpu_visibility=unverified
ASTERINAS_POWERVR_DMA_STAGE segment=3 bytes=9984  pages=3  daddr=0x81a44000 cpu_readback=ok gpu_visibility=unverified
ASTERINAS_POWERVR_GPU_UTIL status=cpu_initialized monotonic_ns=45710088000 native_drivers=1 dms=8 gpu_visibility=unverified
ASTERINAS_POWERVR_FWIF_CLOCK core_hz=800000000 source=gpu_aclk_readback gpu_visibility=unverified
ASTERINAS_POWERVR_MMU status=all_tables_prepared root_daddr=0x1ff803000 \
  code_vaddr=0xe1c0000000 data_vaddr=0xe1c000e000 \
  coremem_code_vaddr=0xe1c0014000 coremem_data_vaddr=0xe1c0027000 \
  connection_vaddr=0xe1c1fd0000 osinit_vaddr=0xe1c1fe0000 sysinit_vaddr=0xe1c1ff0000 \
  connection_fwaddr=0xf1fd0000 osinit_fwaddr=0x71fe0000 sysinit_fwaddr=0x71ff0000 \
  fw_config_initialized=0 gpu_root_installed=0 gpu_visibility=unverified
ASTERINAS_POWERVR_FW_OSCONFIG fw_config_initialized=1 flags=0x1f
ASTERINAS_POWERVR_FW_CONN status=native started=1 faults=0 hwr_state=0x1 hwr_count=0 vz_fw_state=0 vz_boot_stage=0
ASTERINAS_POWERVR_META status=started started=1 compatibility=0 ddk=0.0 gpu_visibility=observed
ASTERINAS_POWERVR_FW_COMPAT started=1 compatibility=0 ddk=0.0
ASTERINAS_POWERVR_IRQ status=installed source=15 parent=0x10 count=0
ASTERINAS_POWERVR_IRQ count=1 spurious=0 last_status=0x1 mts_status=0x0
ASTERINAS_POWERVR_FW_KCCB status=completed slot=0 rtn=0 write_off=1 read_off=1 wake=irq irq_count=1
RELEASE_WRITE=4
STATUS_DECODED meta_release=1 started=1 faults=0 hwr_state=0x1 hwr_count=0 ddk=0.0
ASTERINAS_POWERVR_OWNER session=closed crg_restored=1 aclk=0x00000020 cfg=0x00000000 gray=0x00000000 reset=0x00000000
```

`last_status=0x1` 是 OS0 event source bit 0；`mts_status=0x0` 说明本事件不经过
`RGX_CR_MTS_GPU_INT_STATUS` 的调试位。

## 输入

| 项 | 值 |
|---|---|
| 内核 | `493d297a3149fa35ff7001d74ed5ba8b72ab1203eff836c10e5d0bd2e2f70bcd`，16,568,272 B，CRC32 `fb722f97`（`FEATURES=powervr_probe`） |
| initramfs | `daf09f4290037af1cd5730dbdf3636b6e6e9ed9ab94bf3b4d8f204cc488e4bd0`，1,245,184 B，CRC32 `8386b7de` |
| staging 工具 | `powervr-stage.py`，SHA-256 `29a1d1d0664e4121f89b962d8e3c49fcefa7262e73590eb66937deced3f90254`（未跟踪） |
| DTB | `megrez_dtb-465cb129333cc334.dtb`，CRC32 `40ed4c65` |
| 启动 | 手工 U-Boot：`asterinas.powervr=1 asterinas.powervr_dma_stage=1 asterinas.powervr_release=1`，minimal profile，`asterinas.reboot_after=900` |

## 实现

- 新增 `kernel/src/device/dri/powervr_probe/irq.rs`：
  - 从 DT 的 `img,gpu` 节点解析 `interrupt-parent` 与单 cell `interrupts`
    （`source=15`），用 `IrqLine::alloc()` +
    `IRQ_CHIP.map_fdt_pin_to()` 注册 ostd handler；
  - handler 在 top half 读取 `RGX_CR_IRQ_OS0_EVENT_STATUS`，把 pending 的
    `[1:0]` 位写回 `RGX_CR_IRQ_OS0_EVENT_CLEAR`，再计数；由于 ostd 在
    callback 之后才完成 PLIC claim，先清设备事件可避免 level 触发重入；
  - 安装时先清除启动阶段可能遗留的 pending 事件并清零计数；
  - 统计 `count/spurious/last_status/mts_status`；
- `power.rs` 在提交 `HEALTH_CHECK` 前安装 IRQ；等待循环优先看 IRQ 计数，
  完成判据仍是 KCCB `read_off` 前进，marker 增加 `wake=irq|poll` 与
  `irq_count`；
- 安装失败不阻塞 P2.1–P2.3：打印 `ASTERINAS_POWERVR_IRQ status=skipped
  reason=…` 并回退到轮询。

## dma-fence 判定

**不可行（本轮）**：Asterinas 没有 dma-fence 子系统；现有
`kernel/src/device/dri/fence.rs` 只是一个“创建即已完成”的 anon-inode
descriptor（`new_signalled()`），没有 fence 上下文、时间线或 IRQ 信号通路。
替代方案（已实现）：IRQ 事件计数 + 直接读 KCCB `read_off` 作为完成真相；
真正的 dma-fence/syncobj 属于 P3/P5 的用户态 DRM 接口工作，不在内核 probe
范围内强行引入。

## ktest

- `gpu_irq_ack_requires_a_pending_event_source`：
  `1 passed; 0 failed; 340 filtered out`；
- P2.3 的 OSDATA/KCCB ktest 未改动，仍为 `1 passed; 0 failed`。

## 固化

- 代码：`kernel/src/device/dri/powervr_probe/irq.rs`（新增）、
  `powervr_probe.rs`（模块声明）、`power.rs`（安装与唤醒等待）；
- 计划：[2026-10-21-megrez-powervr-p2-fwif-command.md](../../superpowers/plans/2026-10-21-megrez-powervr-p2-fwif-command.md)
  新增 P2.4 通过记录与 P3 决策简报；
- 原始证据（未跟踪）：`target/megrez-powervr-p2f-20261022/`
  （P2f/上传串口日志、marker 提取、IRQ ktest 日志、bounded 脚本、sha256sums）。

## 边界与下一步

- 本轮的 IRQ 只做**事件确认 + 计数**；没有处理 FWCCB 消息、没有把
  KCCB return-slot 的等待改成真正的 fence；
- KCCB 完成仍以 `read_off` 为准，IRQ 作为唤醒信号；若后续固件事件与
  命令完成解耦，需要把 `wake=poll` 回退路径保留；
- P2.1–P2.4 全部通过后，下一步是 P3 路线拍板（见计划文档的 1 页决策简报：
  方案 A 厂商 C 核心 + Rust 层，4–7 人月；方案 B 纯 Rust，9–16 人月）。
