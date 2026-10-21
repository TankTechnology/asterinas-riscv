# Megrez 板级批量验证（P1–P5a，2026-10-23）

## 结论

用当前 HEAD（含 B1–B5、P4、P5a）重建的内核在 Megrez 上完成了板级批量。
首轮 B2 作业超时，随后定位并修复了 KCCB 命令类型后复跑通过：
**6 组 marker 全部达标**。

| 组 | marker | 结果 |
|---|---|---|
| B1 KCCB | `ASTERINAS_POWERVR_KCCB_PIPELINE status=completed count=4 ... wake=irq irq_count=1` | 通过 |
| B2 作业 | `ASTERINAS_POWERVR_JOB status=completed kind=noop ... rtn=0x1 write_off=5 read_off=5` | 通过（B2a 修复后） |
| B3 DRI/GEM | `DRI_NODE status=ready node=renderD128 gems=0`；`GEM status=ok handle=1 size=4096 offset=0x0 gpu_va=0x4000000000 gems=1` | 通过 |
| B4 fence | `ASTERINAS_POWERVR_FENCE status=ok signaled=1 waited=1 ctx=1` | 通过（B2a 修复后） |
| P4 PRIME | `ASTERINAS_POWERVR_PRIME status=ok exported=1 imported=1` | 通过 |
| P5a bridge | `ASTERINAS_POWERVR_BRIDGE status=ok init=1 cmds=1 unsupported=0` | 通过 |

## 镜像与启动

| 项 | 值 |
|---|---|
| 内核（首轮） | `/tmp/tftp/kernel-pvr-batch2.Image`，16,711,376 B，CRC32 `e291c9f4`，SHA-256 `06f906e98baccc031910addd62aa3a7d0f5bf635d0d06f1fbffbac56647b7d93` |
| 内核（B2a 修复） | `/home/debian/kernel-pvr-b2a.Image`，16,711,376 B，CRC32 `dce1d570`，SHA-256 `3739939a7a79f71ad8b8d513f32a457ba8617f1885fc8d724a0f30929f669903` |
| 构建 | `make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=powervr_probe`（`cargo fmt` 通过） |
| initramfs | `/home/debian/stage1-pvr-batch.cpio`，1,248,256 B，CRC32 `c8f825a7`，SHA-256 `1cf9df3cc2fc5f1232e1712cb600160ecdd2db9d8bf3cdb76fef2c3952f2e751` |
| DTB | mmc 1:3 `/home/debian/asterinas/boot/997c196e6410ad29/megrez_dtb-465cb129333cc334.dtb`，CRC32 `40ed4c65` |
| 加载 | U-Boot `ext4load mmc 1:3`（内核/cpio 都在分区 3；`mmc 1:2` 上的 `/home/debian` 路径不可读） |
| 内核 cmdline | `asterinas.powervr=1 asterinas.powervr_dma_stage=1 asterinas.powervr_release=1 asterinas.powervr_job_noop=1` 以及既有隔离参数 |
| 启动 | `booti 0x80200000 0x83000000:0x131000 0xf0000000` |

启动后先运行新增的 `powervr-p5a-probe.py`（GEM/PRIME/bridge），再运行
既有 `powervr-stage.py` 触发固件启动与 KCCB 流水。

## 逐组 marker

### B1 KCCB（通过）

```text
ASTERINAS_POWERVR_FW_CONN status=native started=1 faults=0 hwr_state=0x1 hwr_count=0
ASTERINAS_POWERVR_IRQ count=1 spurious=0 last_status=0x1 mts_status=0x0
ASTERINAS_POWERVR_FW_KCCB status=completed slot=0 rtn=0 write_off=4 read_off=4 wake=irq irq_count=1
ASTERINAS_POWERVR_KCCB_PIPELINE status=completed count=4 slots=[0, 1, 2, 3] rtn=[0, 1, 0, 1] executed=2 write_off=4 read_off=4 faults=0 wake=irq irq_count=1
```

### B2 作业（首轮超时 → B2a 修复后通过）

首轮（`e291c9f4`）：

```text
ASTERINAS_POWERVR_JOB status=timeout kind=noop ctx=0 vaddr=0xe1c0076000 size=4096 slot=4 rtn=0x0 write_off=5 read_off=4 wake=poll irq_count=1
RELEASE_ERR [Errno 110] Connection timed out
```

B2a 根因：`dma.rs` 把 `RGXFWIF_KCCB_CMD_SLCFLUSHINVAL` 写成了
`102 | (0x2abc << 16)`。vendor `rgx_fwif_km.h` 中 **102 是 `MMUCACHE`，
SLCFLUSHINVAL 是 104**（`0x2abc0068`）。固件收到的是带 SLC 负载的
MMUCACHE 命令，因此不消费该槽，`read_off` 停在 4，作业与 fence 都不会
完成。OS-online、kick 寄存器（0）和 `bDMContext=FALSE` 的 range 语义均
已按 vendor 源码核对，不是原因。

修复（`3739939a…`）后同一 stage 路径：

```text
ASTERINAS_POWERVR_JOB status=completed kind=noop ctx=0 vaddr=0xe1c0076000 size=4096 slot=4 rtn=0x1 write_off=5 read_off=5 wake=poll irq_count=1
ASTERINAS_POWERVR_FENCE status=ok signaled=1 waited=1 ctx=1
```

作业完成判定（`read_off` 前进 + return slot `CMD_EXECUTED`）与 B4
fence 的 signal/wait 同时成立；本条作业的唤醒是 `poll`，同一次启动的
KCCB 流水仍为 `wake=irq`（见 B1 组）。

### B3 DRI 节点 / GEM（通过）

```text
ASTERINAS_POWERVR_OWNER status=ready bvnc=30.3.408.101 crg_restored=1 firmware=untouched render=unavailable
ASTERINAS_POWERVR_DRI_NODE status=ready node=renderD128 gems=0
ASTERINAS_POWERVR_GEM status=ok handle=1 size=4096 offset=0x0 gpu_va=0x4000000000 gems=1
```

`renderD128` 已可被用户态打开，临时 GEM create ioctl 返回 handle、
pool 偏移和 GPU VA；`gems=1` 说明对象已进入 job MMU 上下文。

### P4 PRIME（通过）

```text
ASTERINAS_POWERVR_PRIME status=ok exported=1 imported=0
ASTERINAS_POWERVR_PRIME status=ok exported=1 imported=1
```

同一对象完成 `PRIME_HANDLE_TO_FD → PRIME_FD_TO_HANDLE` 往返，导入后
`gpu_va` 保持不变（probe 打印 `gpu_va=0x4000000000`）。

### P5a bridge（通过）

```text
ASTERINAS_POWERVR_BRIDGE status=ok init=1 cmds=0 unsupported=0
ASTERINAS_POWERVR_BRIDGE status=ok init=1 cmds=1 unsupported=0
```

`SRVKM_INIT(1)` 与 `SRVCORE_CONNECT` 均被接受，CONNECT 返回 packed BVNC
`0x001e000301980065`（30.3.408.101）。

## 串口日志

- 启动 + P5a 用户态 probe：`/tmp/board_batch2.serial.log`
  （归档 `target/board-batch-20261023/board_batch2.serial.log`）
- 固件 stage + KCCB/JOB：`/tmp/board_batch2_stage.serial.log`
  （归档 `target/board-batch-20261023/board_batch2_stage.serial.log`）
- B2a 修复后复跑（JOB + FENCE）：`/tmp/board_b2a.serial.log`
  （归档 `target/board-batch-20261023/board_b2a.serial.log`）

## 下一步

1. B2 作业的唤醒仍是 `poll`（100 ms 内未观察到该槽的 IRQ）；后续可
   核对完成 IRQ 是否覆盖 SLC flush，或把 `wake` 的判定改为“作业完成
   后再检查 IRQ”，不影响完成语义；
2. 用户态 probe 目前只导出/导入一次，后续可扩成多对象并核对 fdinfo
   中的 `gpu_va`/cache 计数；
3. 本轮未实现/未验证 gbm、真实 bridge 内存/渲染命令，仍按 B5 路线 a
   的 A1–A3 推进；
4. 历史文档 `2026-10-22-asterinas-powervr-job-noop.md` 中的
   `eCmdType = 0x2ABC0066` 已被本文件纠正为 `0x2ABC0068`。

## 边界

不修改既有 marker 语义；首轮 timeout 与 B2a 修复过程都按实际串口内容
记录，修复后的通过有单独镜像 SHA 和日志。
