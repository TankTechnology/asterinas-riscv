# Megrez 板级批量验证（P1–P5a，2026-10-23）

## 结论

用当前 HEAD（含 B1–B5、P4、P5a）重建的内核在 Megrez 上完成了一次板级
批量。**4 组 marker 达标，1 组超时，1 组因作业未完成而缺失**：

| 组 | marker | 结果 |
|---|---|---|
| B1 KCCB | `ASTERINAS_POWERVR_KCCB_PIPELINE status=completed count=4 ... wake=irq irq_count=1` | 通过 |
| B2 作业 | `ASTERINAS_POWERVR_JOB status=timeout kind=noop ... slot=4 rtn=0x0 write_off=5 read_off=4 wake=poll` | **超时（未完成）** |
| B3 DRI/GEM | `DRI_NODE status=ready node=renderD128 gems=0`；`GEM status=ok handle=1 size=4096 offset=0x0 gpu_va=0x4000000000 gems=1` | 通过 |
| B4 fence | `ASTERINAS_POWERVR_FENCE` | **缺失**（作业未完成，fence 未被 signal） |
| P4 PRIME | `ASTERINAS_POWERVR_PRIME status=ok exported=1 imported=1` | 通过 |
| P5a bridge | `ASTERINAS_POWERVR_BRIDGE status=ok init=1 cmds=1 unsupported=0` | 通过 |

## 镜像与启动

| 项 | 值 |
|---|---|
| 内核 | `/tmp/tftp/kernel-pvr-batch2.Image`，16,711,376 B，CRC32 `e291c9f4`，SHA-256 `06f906e98baccc031910addd62aa3a7d0f5bf635d0d06f1fbffbac56647b7d93` |
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

### B2 作业（超时，如实记录）

```text
ASTERINAS_POWERVR_JOB status=timeout kind=noop ctx=0 vaddr=0xe1c0076000 size=4096 slot=4 rtn=0x0 write_off=5 read_off=4 wake=poll irq_count=1
RELEASE_ERR [Errno 110] Connection timed out
```

- SLC flush 槽 4 已写入（`write_off=5`），但 100 ms 内固件没有推进
  `read_off`（仍为 4），return slot 仍为 `NO_RESPONSE`；
- 同一次启动的 KCCB 4 条 HEALTH_CHECK/STATEFLAGS 流水已完成，IRQ 也
  触发（`irq_count=1`），所以不是 IRQ/启动问题，而是这条 context-free
  SLCFLUSHINVAL 作业没有被固件接受或在 100 ms 内未执行；
- 因此 `ASTERINAS_POWERVR_FENCE` 没有打印（fence 只在作业完成后 signal）。

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

## 下一步

1. **B2 作业超时**：先用更长但仍有界的等待（例如 1 s）复跑，确认是
   100 ms 太短还是固件拒绝 context-free SLCFLUSHINVAL；若 `read_off`
   始终不推进，按 B2a 计划改为带上下文的 flush/MMU cache 路径；
2. 作业完成后再收 B4 `ASTERINAS_POWERVR_FENCE`；
3. 用户态 probe 目前只导出/导入一次，后续可扩成多对象并核对 fdinfo
   中的 `gpu_va`/cache 计数；
4. 本轮未实现/未验证 gbm、真实 bridge 内存/渲染命令，仍按 B5 路线 a
   的 A1–A3 推进。

## 边界

不修改既有 marker 语义；未完成的组按实际串口内容记录，未将 timeout
写成通过。
