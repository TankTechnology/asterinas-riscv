# Asterinas PowerVR 固件启动（Megrez P1，2026-10-21）

## 结论

- Asterinas 在 Megrez 真机上完成了厂商 PowerVR 固件的**用户态分段 + 内核
  META 释放**：固件 `started=1`、`faults=0`，随后会话干净关闭、CRG 恢复；
- 这是 **P1 / D2 门禁**：证明 Asterinas 侧可以把 AXM-8-256 的 META 固件
  跑起来，不依赖 Linux 或厂商 KMD；
- 不是渲染结果：`compatibility=0`、`ddk=0.0`，固件尚未完成版本握手；
  命令队列/提交（P2）与用户态 GLES（P5）仍待实现。

## 关键串口收据

```text
ASTERINAS_POWERVR_OWNER status=ready bvnc=30.3.408.101 crg_restored=1 firmware=untouched render=unavailable
ASTERINAS_POWERVR_OWNER session=opened bvnc=30.3.408.101 gates=0b111 deasserted=0b11111
ASTERINAS_POWERVR_DMA_STAGE segment=0 bytes=52064 pages=13 daddr=0xfe6f0000 cpu_readback=ok ...
ASTERINAS_POWERVR_DMA_STAGE segment=1 bytes=18432 pages=5  daddr=0xfe6e8000 cpu_readback=ok ...
ASTERINAS_POWERVR_DMA_STAGE segment=2 bytes=73312 pages=18 daddr=0xfe740000 cpu_readback=ok ...
ASTERINAS_POWERVR_DMA_STAGE segment=3 bytes=9984  pages=3  daddr=0xfe774000 cpu_readback=ok ...
ASTERINAS_POWERVR_GPU_UTIL status=cpu_initialized monotonic_ns=54283121000 native_drivers=1 dms=8 ...
ASTERINAS_POWERVR_FWIF_CLOCK core_hz=800000000 source=gpu_aclk_readback ...
ASTERINAS_POWERVR_MMU status=all_tables_prepared root_daddr=0xfe763000 \
  code_vaddr=0xe1c0000000 data_vaddr=0xe1c000e000 \
  coremem_code_vaddr=0xe1c0014000 coremem_data_vaddr=0xe1c0027000 ...
ASTERINAS_POWERVR_META status=started started=1 compatibility=0 ddk=0.0 gpu_visibility=observed
STATUS_DECODED meta_release=1 started=1 faults=0 hwr_state=0x1 hwr_count=0 ddk=0.0
ASTERINAS_POWERVR_OWNER session=closed crg_restored=1 aclk=0x00000020 cfg=0x00000000 gray=0x00000000 reset=0x00000000
```

`hwr_state=0x1` 是 `HARDWARE_OK`；`started=1` 是固件启动标志。

## 输入

| 项 | 值 |
|---|---|
| 内核 | `d1d59ce5cb34f2cd7fc0505f971e6f404ebc909018d3cd71bbcb7ef9687b8010`，16,551,936 B，CRC32 `98027dc3`（`FEATURES=powervr_probe`） |
| initramfs | `daf09f4290037af1cd5730dbdf3636b6e6e9ed9ab94bf3b4d8f204cc488e4bd0`，1,245,184 B（stage1-policy-r2 + 固件 + staging 工具） |
| 固件 | `rgx.fw.30.3.408.101`，SHA-256 `25e9e7ff4645292ceb991617dba028e350a5c17088a105230dbaaaf995f4413b`（来自 RockOS 包，未入库） |
| staging 工具 | `powervr-stage.py`，SHA-256 `29a1d1d0664e4121f89b962d8e3c49fcefa7262e73590eb66937deced3f90254`（未跟踪） |
| DTB | `465cb129333cc334…`（r4 generation） |
| 根镜像 | `440443f25c80baba…`（已安装的 browser-web-board-r1） |
| 启动 | 手工 U-Boot：`asterinas.powervr=1 asterinas.powervr_dma_stage=1 asterinas.powervr_release=1`，minimal profile，`asterinas.reboot_after=900` |

## 做了什么

1. 厂商 `rgx.fw` 是 META LDR 命令流（`RGX_META_LDR_*`），不是 ELF。
   用户态工具按厂商 `ProcessLDRCommandStream` 语义解析 41 个 LOADMEM、
   9 个 ZEROMEM 块，重建四个段：
   `[code 52,064 / data 18,432 / coremem-code 73,312 / coremem-data 9,984]`。
2. 把内核 `meta_boot_config.bin`（296 B，固定堆布局的 META 段 MMU/
   两线程缓存配置）写入 code 段偏移 512。
3. 通过 `/dev/powervr-control` 发送四个 `PVR1` 帧（12 B 头 + 段负载），
   再写 `PVRR` 触发内核的 `release_firmware()`：CRG 上电、catalogue
   安装、`prepare_selected_meta`、`release_selected_meta`、1 s 内轮询
   固件 `started` 标志。
4. 内核返回 `ASTERINAS_POWERVR_META status=started started=1`，随后
   文件关闭触发 Drop：恢复 CRG、清理 catalogue，GPU 回到关断状态。

## 固化

- 内核 feature 修正：`powervr_probe` 从无法编译的
  `eic7700_native_scanout` 中拆出（`kernel/Cargo.toml`、
  `kernel/src/device/dri.rs`），使 probe 可以独立构建；
- 原始证据（未跟踪）：`target/megrez-powervr-p1-20261021/`
  （串口日志、staging 工具、sha256sums、p1-markers）。

## 边界与下一步

- 本次只证明**固件启动**；`compatibility/ddk` 仍为 0，说明固件初始化
  握手尚未完成，不能据此声称渲染或命令提交可用；
- 会话在状态读取后立即关闭，GPU 已恢复关断；P2 需要让会话保持打开，
  实现 FWIF 命令队列、内核/固件堆与至少一个作业的完成 fence；
- 移植计划见
  [2026-10-21-megrez-powervr-native-acceleration.md](../../superpowers/plans/2026-10-21-megrez-powervr-native-acceleration.md)。
