# Asterinas PowerVR native KCCB health check（Megrez P2.3，2026-10-22）

## 结论

- Megrez 真机上，Asterinas 不仅启动了厂商 META 固件，还让固件**消费了一条
  kernel CCB 命令**：`FW_KCCB status=completed slot=0 rtn=0 write_off=1 read_off=1`；
- 这条命令是 `RGXFWIF_KCCB_CMD_HEALTH_CHECK`（`111 | 0x2ABC<<16`），
  经 KCCB 命令槽提交、写指针推进、`RGX_CR_MTS_SCHEDULE=0`（DM GP）kick；
- `read_off=1` 由固件写回，证明 MTS GP 任务被唤醒并消费了 slot 0；
  `rtn=0` 属预期：vendor 的 `RGXFWHealthCheckCmdInt` 走 fire-and-forget
  `RGXScheduleCommand`，**HEALTH_CHECK 没有 return-slot 契约**
  （vendor 只对 KICK/CLEANUP/MMUCACHE 等调用 `RGXWaitForKCCBSlotUpdate`）；
- 同时写入并回读了 vendor 默认的 `RGXFWIF_OSDATA.ui32FwOsConfigFlags=0x1f`
  （`RGXFWIF_INICFG_OS_CTXSWITCH_DM_ALL`，不是 `0x3ffff`）；
- 这是 **P2.3 门禁**。仍不是渲染：`compatibility=0`、`ddk=0.0` 在 native
  单 OS 构建下是正常的（vendor 只在 GUEST 模式等待 `sRGXCompChecks.bUpdated`）。

## 关键串口收据（P2e）

```text
ASTERINAS_POWERVR_OWNER status=ready bvnc=30.3.408.101 crg_restored=1 firmware=untouched render=unavailable
ASTERINAS_POWERVR_OWNER session=opened bvnc=30.3.408.101 gates=0b111 deasserted=0b11111
ASTERINAS_POWERVR_DMA_STAGE segment=0 bytes=52064 pages=13 daddr=0x81c50000 cpu_readback=ok gpu_visibility=unverified
ASTERINAS_POWERVR_DMA_STAGE segment=1 bytes=18432 pages=5  daddr=0x81c48000 cpu_readback=ok gpu_visibility=unverified
ASTERINAS_POWERVR_DMA_STAGE segment=2 bytes=73312 pages=18 daddr=0x81ca0000 cpu_readback=ok gpu_visibility=unverified
ASTERINAS_POWERVR_DMA_STAGE segment=3 bytes=9984  pages=3  daddr=0x81cd4000 cpu_readback=ok gpu_visibility=unverified
ASTERINAS_POWERVR_GPU_UTIL status=cpu_initialized monotonic_ns=43964928000 native_drivers=1 dms=8 gpu_visibility=unverified
ASTERINAS_POWERVR_FWIF_CLOCK core_hz=800000000 source=gpu_aclk_readback gpu_visibility=unverified
ASTERINAS_POWERVR_MMU status=all_tables_prepared root_daddr=0x81cc3000 \
  code_vaddr=0xe1c0000000 data_vaddr=0xe1c000e000 \
  coremem_code_vaddr=0xe1c0014000 coremem_data_vaddr=0xe1c0027000 \
  connection_vaddr=0xe1c1fd0000 osinit_vaddr=0xe1c1fe0000 sysinit_vaddr=0xe1c1ff0000 \
  connection_fwaddr=0xf1fd0000 osinit_fwaddr=0x71fe0000 sysinit_fwaddr=0x71ff0000 \
  fw_config_initialized=0 gpu_root_installed=0 gpu_visibility=unverified
ASTERINAS_POWERVR_FW_OSCONFIG fw_config_initialized=1 flags=0x1f
ASTERINAS_POWERVR_FW_CONN status=native started=1 faults=0 hwr_state=0x1 hwr_count=0 vz_fw_state=0 vz_boot_stage=0
ASTERINAS_POWERVR_META status=started started=1 compatibility=0 ddk=0.0 gpu_visibility=observed
ASTERINAS_POWERVR_FW_COMPAT started=1 compatibility=0 ddk=0.0
ASTERINAS_POWERVR_FW_KCCB status=completed slot=0 rtn=0 write_off=1 read_off=1
RELEASE_WRITE=4
STATUS_DECODED meta_release=1 started=1 faults=0 hwr_state=0x1 hwr_count=0 ddk=0.0
ASTERINAS_POWERVR_OWNER session=closed crg_restored=1 aclk=0x00000020 cfg=0x00000000 gray=0x00000000 reset=0x00000000
```

`hwr_state=0x1` 是 `HARDWARE_OK`；`started=1` 是 `RGXFWIF_SYSINIT.bFirmwareStarted`。

## 输入

| 项 | 值 |
|---|---|
| 内核 | `537e9630e39db1d0a15cbb40ac3b3d8e67d3ad9a5d2cf93c721c65c3381aadb0`，16,561,184 B，CRC32 `56ab4a4a`（`FEATURES=powervr_probe`） |
| initramfs | `daf09f4290037af1cd5730dbdf3636b6e6e9ed9ab94bf3b4d8f204cc488e4bd0`，1,245,184 B，CRC32 `8386b7de`（stage1-pvr + 固件 + staging 工具） |
| staging 工具 | `powervr-stage.py`，SHA-256 `29a1d1d0664e4121f89b962d8e3c49fcefa7262e73590eb66937deced3f90254`（未跟踪） |
| DTB | `megrez_dtb-465cb129333cc334.dtb`，CRC32 `40ed4c65` |
| 启动 | 手工 U-Boot：`asterinas.powervr=1 asterinas.powervr_dma_stage=1 asterinas.powervr_release=1`，minimal profile，`asterinas.reboot_after=900` |
| 板端分区 | U-Boot `mmc 1:2`（Linux `mmcblk0p2`，GPT 标签 `swap`）是已安装的 Debian 根；`mmc 1:3`（标签 `rootfs`）保留旧的 boot artifacts、`stage1-pvr.cpio` 与 DTB |

## 做了什么

1. **P2.1 纠偏（vendor 源码）**：Megrez 的 DDK 是 native 单 OS 构建
   （`RGX_NUM_DRIVERS_SUPPORTED=1`，无 `SUPPORT_AUTOVZ`/`SUPPORT_AUTOVZ_HW_REGS`）。
   `KM_SET_OS_CONNECTION`/`KM_FW_CONNECTION_IS` 在这种构建里展开为常量，
   `RGX_CR_SCRATCH14` 只在 `TRACK_FW_BOOT` 下被读取。因此
   `OS READY→FW READY/ACTIVE + INIT_DONE` 握手不存在；P2.1 板级
   `fw_state=0 boot_stage=0` 是预期值，不是固件挂起。native 启动门是
   `bFirmwareStarted=1` 且无 fault/HWR（P1 已达成）。
2. **P2.2（OSDATA flags）**：在 `release_selected_meta` 之前写
   `RGXFWIF_OSDATA.ui32FwOsConfigFlags = 0x1f`（vendor 默认
   `RGXFWIF_INICFG_OS_CTXSWITCH_DM_ALL`），读回并打印 marker。
3. **P2.3（KCCB HEALTH_CHECK）**：`dma.rs` 记录 kernel CCB control/命令/
   return-slot 的 CPU vaddr；`enqueue_health_check()` 清零 64 B 槽、
   写 `eCmdType=0x2ABC006F`、清 return slot、推进 `ui32WriteOffset`；
   `power.rs` 写 `RGX_CR_MTS_SCHEDULE=0`（DM GP）并读回，轮询
   `read_off`/return slot，`read_off != slot` 即判固件已消费，打印
   `FW_KCCB status=completed`。
4. **串口采集修正**：不再使用无限 `^C` 读循环。`/tmp/board_p2e.py` 与
   `/tmp/reboot_bounded.py` 的每个等待都有 deadline，命中 marker 或超时即
   退出并保留完整日志。

## 关于 p2d 没有 POWERVR marker 的判定

- `/tmp/board_upload_p2d.serial.log` 是**上传/网络启动会话**的日志，
  U-Boot 加载的是已安装内核
  `/home/debian/asterinas/boot/79e4761b973b4a51/kernel-a715ae3af6802ed9.Image`
  （a715ae3a），不是 p2d；该会话只负责把 p2d 下载到 `/root/kernel-pvr-p2d.Image`；
- 已安装内核在 `powervr_probe` feature 引入之前构建，不含 probe；
  且该会话 bootargs 没有 `asterinas.powervr=1`；
- `dri.rs` 的 `powervr_probe::probe_on_request()` 只在
  `cfg(feature="powervr_probe")` 且 cmdline 有
  `asterinas.powervr=1`/`asterinas.gpu_*_probe=1` 时运行；
- 即使上述条件满足，固件 staging/release 仍需用户态执行
  `/run/asterinas-tools/powervr-stage.py`（写 PVR1 段并触发 `PVRR`），
  上传会话从未执行它。
- 结论：p2d 图像本身没有启动过；P2e 才是在 U-Boot 手工加载、带 probe 标志
  并执行 staging 工具的运行。

## ktest

- `gpu_dma_stage_initializes_fw_os_config_flags`：`1 passed; 0 failed; 339 filtered out`；
- `gpu_dma_stage_enqueues_health_check_and_observes_return_slot`：`1 passed; 0 failed; 339 filtered out`；
- harness 备注：`cargo osdk test TESTNAME` 按测试路径**元素后缀精确匹配**，
  需在 `kernel/` 下并显式给 `--target-arch riscv64 --scheme riscv`。

## 固化

- 代码：`kernel/src/device/dri/powervr_probe/{dma,power,start,fwif}.rs`；
- 计划：[2026-10-21-megrez-powervr-p2-fwif-command.md](../../superpowers/plans/2026-10-21-megrez-powervr-p2-fwif-command.md)
  新增「2026-10-21 板级修正」与 P2.3 首次板级结果；
- 原始证据（未跟踪）：`target/megrez-powervr-p2e-20261022/`
  （P2e/上传串口日志、marker 提取、两个 ktest 串口日志、sha256sums）。

## 边界与下一步

- 本次证明「固件运行 + KCCB 命令被消费」，**不证明 return-slot 路径**；
  HEALTH_CHECK 无 return 契约。若后续需要 return-slot 证据，应改用
  KICK/CLEANUP/MMUCACHE 等会 `RGXWaitForKCCBSlotUpdate` 的命令；
- 超时时的两条替代路径（本次未触发）：
  A. 先完成版本握手/compatibility（native 下 `ddk=0.0` 是预期，不是阻塞项）；
  B. 检查 KCCB ring 基址/对齐/轮询语义（OSINIT `psKernelCCBCtl/CCB/RtnSlots`
  与 64 B 槽、wrap mask、`read_off` 语义）；
- 下一步 **P2.4**：把 return-slot 轮询换成 IRQ/fence（`RGXIRQ` 路径），
  验收 `ASTERINAS_POWERVR_IRQ count=N`，再进入 P3（用户态 DDK 接口）。
