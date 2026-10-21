# Asterinas PowerVR KCCB 命令流水（Megrez P3/B1，2026-10-22）

## 结论

- 内核侧 B1 已实现：kernel CCB 环形队列的**多 slot 入队、写指针推进、
  每槽 return slot 清零/回读、一次 MTS kick 排空、IRQ 唤醒完成等待**；
- 命令流为 `HEALTH_CHECK` ×2 + `STATEFLAGS_CTRL` ×2。后者是 vendor 用
  `RGXWaitForKCCBSlotUpdate()` 等待返回槽的诊断命令；零负载、零状态变化，
  用来验证 return-slot 路径；
- 两个 ktest 在 QEMU 通过：
  `gpu_kccb_pipeline_enqueues_multiple_slots_and_reads_returns`、
  `gpu_kccb_ring_wraps_and_rejects_a_full_ring`；
- **板级验收尚未完成（阻塞）**：板子当前停在 RockOS 登录提示，串口
  SysRq 未启用，SSH 以 `debian`（uid 1000）可达但没有 passwordless sudo；
  在不猜测凭据的前提下无法软重启。需要一次物理复位或用户提供已授权的
  root 通道；恢复步骤见文末。
- 期望的板级 marker（待确认）：

```text
ASTERINAS_POWERVR_KCCB_PIPELINE status=completed count=4 slots=[0, 1, 2, 3] rtn=[0, 1, 0, 1] executed=2 write_off=4 read_off=4 faults=0 wake=irq irq_count=1
```

`rtn` 的预期是 HEALTH_CHECK 槽保持 0（vendor fire-and-forget），
STATEFLAGS_CTRL 槽为 `CMD_EXECUTED=1`；若固件对两者都不写返回槽，
`read_off` 前进仍是完成真相，B1 文档会按实测更新。

## 实现

### 环形队列（`dma.rs`）

- `KccbOffsets { write, read, wrap }`：从 staged kernel CCB control 读取；
- `kccb_enqueue(cmd_type)`：
  - 检查 `write & !wrap == 0`，再检查
    `(write - read) & wrap >= wrap` → `gpu_kccb_ring_full`（环容量为
    `wrap` 个槽，留一个空槽区分满/空）；
  - 槽号 `write & wrap`，64 B 槽清零后写 `eCmdType`（所有本轮命令都
    没有负载）；
  - return slot 先写 `NO_RESPONSE=0`；
  - `fence(SeqCst)` 后再写 `ui32WriteOffset = (write + 1) & wrap`；
  - 返回槽号；
- `kccb_return(slot)`：读回该槽的 return slot。

### 流水与等待（`power.rs`）

- 一次 `release_firmware()` 内依次入队：
  `HEALTH_CHECK, STATEFLAGS_CTRL, HEALTH_CHECK, STATEFLAGS_CTRL`；
- 记录最终 `write` 偏移，然后只写一次 `RGX_CR_MTS_SCHEDULE=0` 并读回；
- 等待条件：`read == final_write`（固件排空整条流水）**且** IRQ 计数
  有变化；100 ms deadline，无 IRQ 时退化为轮询并把 `wake=poll` 记入
  marker；
- 读回四个 return slot，`executed` 统计 `CMD_EXECUTED` 位；
- 排空后重新读 `firmware_status()`，把 `faults/hwr_state/hwr_count`
  写进 pipeline marker；faults 非零则 `status=faulted` 并返回 EIO；
- 保留 P2 的 `FW_KCCB status=completed slot=…`、`IRQ count=…`、`OWNER`
  等 marker 语义，只新增 `KCCB_PIPELINE`。

### 命令常量

| 命令 | 值 | 负载 |
|---|---|---|
| `RGXFWIF_KCCB_CMD_HEALTH_CHECK` | `0x2ABC006F` | 无 |
| `RGXFWIF_KCCB_CMD_STATEFLAGS_CTRL` | `0x2ABC00CE` | 无（vendor 对零负载命令只拷贝 8 B 头） |

`STATEFLAGS_CTRL` 的 vendor 语义：host 先更新
`RGXFWIF_SYSDATA.ui32ConfigFlags`，再发该命令让固件刷新缓存值，并等待
KCCB 返回槽。本实现两处都是零值，因此是诊断性的 no-op。

## ktest

| 测试 | 结果 |
|---|---|
| `gpu_kccb_pipeline_enqueues_multiple_slots_and_reads_returns` | `1 passed; 0 failed; 341 filtered out` |
| `gpu_kccb_ring_wraps_and_rejects_a_full_ring` | `1 passed; 0 failed; 341 filtered out` |

原始日志（未跟踪）：`target/kt-b1-final-pipeline.serial.log`、
`target/kt-b1-final-wrap.serial.log`、`target/kt-b1-*.serial.log`。

覆盖点：
- 4 条命令依次落在 slot 0..3，写指针 0→4；
- 每个槽的 `eCmdType` 正确、槽内无残留负载；
- 每个 return slot 入队时被清为 0；
- 模拟固件推进 `read=4` 并置 slot 1/3 为 `CMD_EXECUTED` 后，读回正确；
- 4 槽环（wrap=3）从 slot 3 回绕到 0/1，第三次入队后满，第四次返回
  `gpu_kccb_ring_full`。

## 输入

| 项 | 值 |
|---|---|
| 内核 | `893687b6b32587d0a2114e09e5da0c1713ab29ddcf982a900231e4862a5bf133`，16,574,672 B，CRC32 `5ce2c871`（`FEATURES=powervr_probe`） |
| initramfs | `daf09f4290037af1cd5730dbdf3636b6e6e9ed9ab94bf3b4d8f204cc488e4bd0`，1,245,184 B，CRC32 `8386b7de` |
| staging 工具 | `powervr-stage.py`，SHA-256 `29a1d1d0664e4121f89b962d8e3c49fcefa7262e73590eb66937deced3f90254` |
| DTB | `megrez_dtb-465cb129333cc334.dtb`，CRC32 `40ed4c65` |
| 启动 | 手工 U-Boot：`asterinas.powervr=1 asterinas.powervr_dma_stage=1 asterinas.powervr_release=1`，minimal profile，`asterinas.reboot_after=900` |

## 板级阻塞与恢复步骤

**当前状态（2026-10-22 00:5x）**：

- 串口停在 `rockos-eswin login:`（RockOS/Debian trixie 的 getty）；
- `10.100.19.200` 可 ping，22 端口开放，现有密钥可以 `debian`（uid 1000）
  登录；`sudo -n` 需要密码，未尝试任何凭据；
- 串口 BREAK + SysRq `b` 未触发重启（RockOS 内核未启用 serial SysRq 或
  控制台不处理 break）；
- 因此无法在不猜测凭据的前提下把板子带回 U-Boot。

**需要**：一次物理复位（或用户提供已授权的 root 通道）。复位后按以下
顺序恢复：

1. 在 U-Boot 提示符运行 `/tmp/board_upload_p3b1.py`：
   网络启动已安装的 Debian，HTTP 下载 `/tmp/tftp/kernel-pvr-b1.Image`
   （SHA-256 `893687b6…`），校验后重启回 U-Boot；
2. 运行 `/tmp/board_p3b1.py`：
   从 `mmc 1:2` 加载 `/root/kernel-pvr-b1.Image`，校验 CRC32
   `5ce2c871`，带 probe 标志启动，自动执行
   `/run/asterinas-tools/powervr-stage.py`，等待
   `ASTERINAS_POWERVR_KCCB_PIPELINE` marker 后退出；
3. 把串口日志/marker 落到 `target/megrez-powervr-b1-20261022/`，更新本文档
   “结论”中的板级行，再提交一次证据更新。

## 边界

- B1 只验证 KCCB 环形队列与多命令流水；不含渲染上下文、MMU 上下文/作业
  （B2）、DRM render node/GEM（B3）、fence/sync fd（B4）、GLES 后端（B5）；
- `STATEFLAGS_CTRL` 是诊断 no-op，不改变固件状态标志；
- OS-online 握手（`RGXFWIF_OS_STATE_CHANGE_DATA` + ONLINE 位）按用户要求
  留给 **B1b**，本轮不挖 vendor 全量语义；
- 板级 marker 是 B1 的唯一剩余验收门；在拿到它之前不宣称 B1 完整通过。
