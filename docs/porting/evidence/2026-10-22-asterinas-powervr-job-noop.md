# Asterinas PowerVR no-op flush 作业（Megrez P3/B2，2026-10-22）

## 结论

- B2 主机侧完成：在现有固件 MMU 上下文里为一次作业分配并映射一页缓冲，
  通过 KCCB 提交一条 **`RGXFWIF_KCCB_CMD_SLCFLUSHINVAL`**（context-free
  range flush：`bDMContext = FALSE`、`psContext = NULL`），并用 IRQ 唤醒
  等待 return slot；
- 两个 QEMU ktest 在最终树通过：
  `gpu_job_noop_context_maps_buffer`、`gpu_job_noop_submits_flush_command`
  （各 `1 passed; 0 failed; 343 filtered out`）；
- **板级 marker 已定义但延后**：板子在上一次上传启动的网络大传输中挂死
  （串口/网络均无响应），B1/B2 的板级收据都需要一次物理复位后补；
- 作业路径是 **opt-in**：只有 cmdline 含 `asterinas.powervr_job_noop=1`
  才在 KCCB pipeline 之后提交，P2/P3 既有 marker 与默认 release 路径不变。

期望的板级 marker：

```text
ASTERINAS_POWERVR_JOB status=completed kind=noop ctx=0 vaddr=0xe1c0076000 size=4096 slot=4 rtn=0x1 write_off=5 read_off=5 wake=irq irq_count=2
```

`ctx=0` 表示复用现有固件 MMU 上下文；`vaddr` 是作业缓冲在该上下文中的
GPU VA。真正的 per-job MMU 上下文（context ID、独立页表根、MMU cache
失效）拆到 **B2a**。

## 实现

### 作业缓冲与 GPU VA 上下文（`dma.rs`）

- `GpuJobBuffer { vaddr, size }`；
- `create_job_buffer(bytes)`：
  - 1–65,536 B；按页分配 DMA（`GpuDmaAllocation`）；
  - VA 固定在 `FW_ALIGN_VADDR + 2 * PAGE_SIZE`（`0xe1c0076000`），紧接
    staged fault/counter/align 页之后的 host-owned 区域，避开固件
    runtime heap 与 config slots；
  - 用现有 `GpuMmu4::map_owned()` 建立 PTE（可读写、PM_META_PROTECT），
    返回 GPU VA 与大小；
  - 超过 config slot 起始地址或重复映射由 MMU 的重叠检查拒绝。

### KCCB 提交（`dma.rs`）

- 抽出 `kccb_alloc_slot()` / `kccb_commit_slot()`：
  - 分配槽、清零 64 B、return slot 清 0；
  - 满环检查 `(write - read) & wrap >= wrap`；
  - commit 时 `fence(SeqCst)` 再推进写指针；
- `kccb_enqueue_slc_flush(gpu_va, size, invalidate)`：
  - `eCmdType = 0x2ABC0066`；
  - `RGXFWIF_SLCFLUSHINVALDATA` 的 packed 布局（vendor 头文件 offsetof
    探针实测，结构 24 B）：
    `psContext` 在 `uCmdData+0`（写 0）、`bInval` 在 `+4`、
    `bDMContext` 在 `+5`（写 0）、`ui64Address` 在 `+8`、`ui64Size` 在
    `+16`；`uCmdData` 在 KCCB 槽内偏移 8；
  - 写入后 commit。

### 完成等待（`power.rs`）

- 在 KCCB pipeline 完成后，若 cmdline 有 `asterinas.powervr_job_noop=1`：
  1. `create_job_buffer(PAGE_SIZE)`；
  2. `kccb_enqueue_slc_flush(vaddr, size, invalidate=true)`；
  3. 写一次 `RGX_CR_MTS_SCHEDULE=0` 并读回；
  4. 100 ms 内等待 `read == final_write` **且** return slot 的
     `CMD_EXECUTED` 位；IRQ 计数变化记为 `wake=irq`，否则 `wake=poll`；
  5. 打印 `ASTERINAS_POWERVR_JOB status=completed|timeout …`；
- 失败路径打印 `status=timeout` 的 slot/rtn/write/read 偏移并返回
  `ETIMEDOUT`，不掩盖固件未响应。

### 为什么选 SLCFLUSHINVAL 而不是 KICK

- vendor 的 `PVRSRVRGXFlushComputeDataKM()` 用同一条命令做“flush 作业”，
  并 `RGXWaitForKCCBSlotUpdate()` 等待 return slot，语义明确；
- 它支持无上下文的范围路径（`bDMContext=FALSE` + address/size），不需要
  先创建 render context，适合作为 B2 的最小作业；
- KICK 需要完整的 render context/freelist/CCB 结构，属于 B2a/B3。

## ktest

| 测试 | 覆盖 | 结果 |
|---|---|---|
| `gpu_job_noop_context_maps_buffer` | 作业 VA 的 PTE 存在、guard 页未映射、CPU 写入/读回、非法大小拒绝 | `1 passed; 0 failed; 343 filtered out` |
| `gpu_job_noop_submits_flush_command` | SLC flush 槽的 `eCmdType`、`psContext/bInval/bDMContext/address/size` 字段、写指针推进、return slot 清零；模拟固件 `read=1` + `CMD_EXECUTED` 后回读 | `1 passed; 0 failed; 343 filtered out` |

原始日志（未跟踪）：`target/kt-b2-final-context.serial.log`、
`target/kt-b2-final-flush.serial.log`、`target/kt-b2-*.serial.log`。

## 输入

| 项 | 值 |
|---|---|
| 内核 | `1871dda9151c6564e74b98e9e5a78f5afc853918ea95453f6aea2acdc4c70f21`，16,582,640 B，CRC32 `3d6e5920`（`FEATURES=powervr_probe`） |
| 启动（板级延后） | 在既有 probe 标志上加 `asterinas.powervr_job_noop=1` |
| 其余 | initramfs/staging 工具/DTB 同 P2/P3 收据 |

## 板级状态与延后项

- 板子在上一次 B1 上传启动（已安装内核的 HTTP 下载）中挂死：串口无输出、
  网络不通，600 s 软件看门狗未恢复；需要物理复位；
- 复位后先补 B1 板级 marker（见
  `2026-10-22-asterinas-powervr-kccb-pipeline.md` 的恢复步骤），再用
  `/tmp/kernel-pvr-b2-noop.Image` + `asterinas.powervr_job_noop=1` 补跑
  B2 marker；
- 由于板级未验证，以下两点必须在 B2a 或板级收据中确认：
  1. 固件是否接受 context-free 的 SLC flush 范围路径（若只接受
     `bDMContext=TRUE`，需要先建 compute/render context，转 B2a）；
  2. 新建 PTE 是否被固件 MMU cache 观察到；若 SLC flush 未生效或出现
     fault，B2a 在 flush 前加一条 `RGXFWIF_KCCB_CMD_MMUCACHE`。

## 边界与下一步

- **B2a（后续）**：真正的 per-job MMU 上下文（context ID、独立页表根、
  上下文切换）、MMU cache 失效、OS-online（B1b）最小握手；板级补 B1/B2
  marker；
- **B3**：DRM render node + GEM/PRIME；
- **B4**：fence/sync（时间线、可等待 fd）；
- **B5**：GLES 后端路线。

本轮不改变 P2/P3 marker 语义，不宣称板级作业已完成。
