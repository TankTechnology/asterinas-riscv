# Megrez PowerVR P2：FWIF 连接与一个 KCCB 命令实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 P1 已启动的 META 固件上确认 native 启动成立（`SYSINIT.bFirmwareStarted=1`、无 fault/HWR），写入 vendor 默认的 `ui32FwOsConfigFlags`，并让固件执行一个 `RGXFWIF_KCCB_CMD_HEALTH_CHECK` 命令、写回 return slot。

**Architecture:** 全部在现有 Rust probe 内扩展，不引入 C DDK：`power.rs` 在 `release_firmware()` 成功后提交 KCCB 命令并通过 `StartIo` 访问 `RGX_CR_MTS_SCHEDULE` kick；`fwif.rs`/`dma.rs` 暴露 KCCB control/命令/return-slot 的 vaddr 与写入方法；用户态 staging 工具沿用 P1 的 PVR1/PVRR 协议。IRQ 留到 P2.4（本计划先轮询 return slot）。

**Tech Stack:** Rust（`kernel/src/device/dri/powervr_probe/`）、`ostd` IoMem/DMA、`aster_time`、cargo-osdk ktest、板级串口 marker、RockOS 参考寄存器定义（`hwdefs/volcanic/km/rgx_cr_defs_km.h` 与 Volcanic `rgx_fwif_km.h`）。

**Scope gate:** P2.1 的 VZ scratch 握手在本 vendor 构建上不存在（见 2026-10-21 板级修正）；native 门为 `SYSINIT.bFirmwareStarted` + 无 fault/HWR，已由 P1 达到。P2.3 的验收是**一个命令完成**，不是渲染。

---

## 事实基线（实现时引用）

| 项 | 值 |
|---|---|
| OS connection | `RGX_CR_OS0_SCRATCH2` offset `0x0890`；**仅 AutoVz 构建使用**（本 vendor 构建为 native，宏为 no-op） |
| FW connection | `RGX_CR_OS0_SCRATCH3` offset `0x0898`；**仅 AutoVz 构建使用**（native 下为普通 scratch） |
| FW boot stage | `RGX_CR_SCRATCH14` offset `0x0870`；`FW_BOOT_INIT_DONE=7`；vendor 仅在 `TRACK_FW_BOOT` 下采样，**不是 native 启动门** |
| Native 启动门 | `RGXFWIF_SYSINIT.bFirmwareStarted`（offset 208）=1，且 `ui32HWRStateFlags`/fault 无恢复标志 |
| KCCB control | `RGXFWIF_CCB_CTL { write_off, read_off, wrap_mask }`，offset 单位=命令槽；probe 用 `wrap_mask=(1<<10)-1` |
| KCCB 槽 | 64 B；命令类型 `eCmdType` 带 `RGX_CMD_MAGIC_DWORD(0x2ABC)<<16` |
| HEALTH_CHECK | `RGXFWIF_KCCB_CMD_HEALTH_CHECK = 111 | (0x2ABC<<16)` = `0x2ABC006F` |
| Return slot | `u32`/槽；host 先写 `NO_RESPONSE=0`，FW 完成写 `CMD_EXECUTED=1`（实现里定义 `RGXFWIF_KCCB_RTN_SLOT_CMD_EXECUTED: u32 = 1`） |
| Kick | `RGX_CR_MTS_SCHEDULE` offset `0x0B00`，`RGXFWIF_DM_GP=0`，写 `0` 后读回 |
| OSDATA 配置 | probe 已写 `osdata+572 = sPowerSync` FW addr；`ui32FwOsConfigFlags` 在 `osdata+0`。vendor 默认值为 `RGXFWIF_INICFG_OS_CTXSWITCH_DM_ALL=0x1F`（`PVRSRV_APPHINT_ENABLEFWCONTEXTSWITCH`），不是 `RGXFWIF_INICFG_OS_ALL=0x3FFFF` |
| 既有 ktest 范式 | `powervr_probe/start.rs` 的 `FakeStartIo` / `FakeFirmwareWait` |

参考源码（稀疏克隆，未入库）：
`eswincomputing/linux-stable:linux-6.6.18-EIC7X/drivers/gpu/drm/img/img-volcanic/`
- `services/server/devices/rgxpower.c:600-800`（native 连接状态迁移）
- `services/server/devices/volcanic/rgxfwutils.c:3550-3710`（KCCB 提交与 kick）
- `hwdefs/volcanic/km/rgx_cr_defs_km.h`、`include/volcanic/rgx_fwif_km.h`

---

### Task P2.1: native 连接握手（OS READY → FW READY/ACTIVE）

**Files:**
- Modify: `kernel/src/device/dri/powervr_probe/start.rs`（常量/状态机/ktest）
- Modify: `kernel/src/device/dri/powervr_probe/power.rs`（在 `release_firmware()` 中调用握手）
- Test: `kernel/src/device/dri/powervr_probe/start.rs`（`#[cfg(ktest)]`）

- [ ] **Step 1: 写失败测试**

在 `start.rs` 的 ktest 模块新增：

```rust
#[cfg(ktest)]
mod connection_tests {
    use super::*;
    use alloc::{collections::VecDeque, vec, vec::Vec};
    use core::time::Duration;
    use ostd::prelude::ktest;

    struct FakeConnectionIo {
        fw_state: VecDeque<u32>,
        boot_stage: VecDeque<u32>,
        writes: Vec<(usize, u32)>,
        elapsed: Duration,
    }

    impl ConnectionIo for FakeConnectionIo {
        fn read32(&mut self, offset: usize) -> Result<u32, &'static str> {
            match offset {
                RGX_CR_OS0_SCRATCH3 => self.fw_state.pop_front().ok_or("unexpected_read"),
                RGX_CR_SCRATCH14 => self.boot_stage.pop_front().ok_or("unexpected_read"),
                _ => Err("unexpected_read"),
            }
        }
        fn write32(&mut self, offset: usize, value: u32) -> Result<(), &'static str> {
            self.writes.push((offset, value));
            Ok(())
        }
        fn monotonic_time(&mut self) -> Duration {
            self.elapsed += Duration::from_millis(1);
            self.elapsed
        }
    }

    #[ktest]
    fn connection_writes_ready_then_active_and_accepts_init_done() {
        let mut io = FakeConnectionIo {
            fw_state: VecDeque::from(vec![0, 1, 1, 2]),
            boot_stage: VecDeque::from(vec![1, 3, 5, 7]),
            writes: Vec::new(),
            elapsed: Duration::ZERO,
        };
        let result = complete_native_connection(&mut io, Duration::from_millis(100));
        assert_eq!(result, Ok(ConnectionState { fw_state: 2, boot_stage: 7 }));
        assert_eq!(io.writes,
            vec![(RGX_CR_OS0_SCRATCH2, OS_READY), (RGX_CR_OS0_SCRATCH2, OS_ACTIVE)]);
    }

    #[ktest]
    fn connection_times_out_without_advancing_boot_stage() {
        let mut io = FakeConnectionIo {
            fw_state: VecDeque::from(vec![0; 4]),
            boot_stage: VecDeque::from(vec![1; 4]),
            writes: Vec::new(),
            elapsed: Duration::ZERO,
        };
        assert_eq!(
            complete_native_connection(&mut io, Duration::from_millis(2)),
            Err("gpu_fw_connection_timeout")
        );
    }
}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `tools/docker/run_dev_container.sh -- make ktest TARGET_ARCH=riscv64 SMP=4 FEATURES=powervr_probe`
Expected: 编译失败（`ConnectionIo`/`complete_native_connection` 未定义）。

- [ ] **Step 3: 最小实现**

在 `start.rs` 增加：

```rust
pub(super) const RGX_CR_SCRATCH14: usize = 0x0870;
pub(super) const RGX_CR_OS0_SCRATCH2: usize = 0x0890;
pub(super) const RGX_CR_OS0_SCRATCH3: usize = 0x0898;
pub(super) const OS_READY: u32 = 1;
pub(super) const OS_ACTIVE: u32 = 2;
const FW_READY: u32 = 1;
const FW_ACTIVE: u32 = 2;
const FW_BOOT_INIT_DONE: u32 = 7;

#[derive(Debug, PartialEq, Eq)]
pub(super) struct ConnectionState {
    pub(super) fw_state: u32,
    pub(super) boot_stage: u32,
}

pub(super) trait ConnectionIo {
    fn read32(&mut self, offset: usize) -> Result<u32, &'static str>;
    fn write32(&mut self, offset: usize, value: u32) -> Result<(), &'static str>;
    fn monotonic_time(&mut self) -> core::time::Duration;
}

/// Native-mode connection: OS READY first, then wait for FW READY/ACTIVE and
/// INIT_DONE, then escalate OS ACTIVE.  Bounded by `timeout`.
pub(super) fn complete_native_connection(
    io: &mut impl ConnectionIo,
    timeout: core::time::Duration,
) -> Result<ConnectionState, &'static str> {
    let start = io.monotonic_time();
    io.write32(RGX_CR_OS0_SCRATCH2, OS_READY)?;
    loop {
        if io.monotonic_time().saturating_sub(start) >= timeout {
            return Err("gpu_fw_connection_timeout");
        }
        let fw_state = io.read32(RGX_CR_OS0_SCRATCH3)?;
        let boot_stage = io.read32(RGX_CR_SCRATCH14)?;
        if boot_stage == FW_BOOT_INIT_DONE && (fw_state == FW_READY || fw_state == FW_ACTIVE) {
            io.write32(RGX_CR_OS0_SCRATCH2, OS_ACTIVE)?;
            return Ok(ConnectionState { fw_state, boot_stage });
        }
    }
}
```

在 `power.rs` 的 `release_firmware()` 中，`wait_selected_firmware(&mut FirmwareWait { staging: &mut staging })` 成功后：

```rust
let owner = hardware_power_io()
    .map_err(|reason| Error::with_message(Errno::EIO, reason))?;
let mut io = owner.lock();
let connection = start::complete_native_connection(
    &mut *io,
    Duration::from_millis(100),
)
.map_err(|reason| Error::with_message(Errno::ETIMEDOUT, reason))?;
drop(io);
aster_logger::println!(
    "ASTERINAS_POWERVR_FW_CONN status=active fw_state={} boot_stage={}",
    connection.fw_state, connection.boot_stage
);
let status = staging
    .firmware_status()
    .map_err(|reason| Error::with_message(Errno::EIO, reason))?;
let (started, compatibility, ddk_version, ddk_build) = status.startup_summary();
aster_logger::println!(
    "ASTERINAS_POWERVR_FW_COMPAT started={} compatibility={} ddk={}.{}",
    started, compatibility, ddk_version, ddk_build
);
```

- [ ] **Step 4: 运行测试确认通过**

Run: 同 Step 2。
Expected: 两个连接测试 PASS，原有 probe ktest 不回归。

- [ ] **Step 5: 板级验收**

用 P1 相同的手工 U-Boot 启动（probe kernel + 固件 initramfs + `asterinas.powervr=1 asterinas.powervr_dma_stage=1 asterinas.powervr_release=1`），
在 debug shell 运行 `/run/asterinas-tools/powervr-stage.py`。
Expected 串口新增且顺序正确：

```text
ASTERINAS_POWERVR_FW_CONN status=active fw_state=2 boot_stage=7
ASTERINAS_POWERVR_FW_COMPAT bvnc=30.3.408.101 compatibility=1 ddk=24.2
```

若超时：marker 为 `ASTERINAS_POWERVR_FW_CONN status=timeout fw_state=… boot_stage=…`，
停止并进入 Task P2.2 分析（不要继续 KCCB）。

- [ ] **Step 6: 提交**

```bash
git add kernel/src/device/dri/powervr_probe/start.rs kernel/src/device/dri/powervr_probe/power.rs
git commit -m "feat(gpu): complete the native PowerVR firmware connection handshake"
```

---

### Task P2.2: OSDATA 配置标志与兼容性回读（P2.1 超时时执行）

**Files:**
- Modify: `kernel/src/device/dri/powervr_probe/dma.rs`（OSDATA 字段写入）
- Modify: `kernel/src/device/dri/powervr_probe/power.rs`（marker）

**背景：** probe 已写 `osdata+572 = sPowerSync`，但 `ui32FwOsConfigFlags`（`osdata+0`）
仍为 0，marker 记为 `fw_config_initialized=0`。厂商 native 路径写入
`RGXFWIF_INICFG_OS_ALL=0x3FFFF`。

- [ ] **Step 1: 写失败测试**

在 `dma.rs` ktest 中：stage 四个段后断言 `read_mapped_u32(osdata + 0) == 0`，
再调用新方法 `stage.initialize_fw_os_config_flags(RGXFWIF_INICFG_OS_ALL)`，
断言读回 `0x3FFFF`，并断言越界/重复调用返回错误。

- [ ] **Step 2: 运行测试确认失败**

Run: 同 Task P2.1 Step 2。
Expected: `initialize_fw_os_config_flags` 未定义。

- [ ] **Step 3: 最小实现**

在 `GpuFirmwareStage` 保存 `osdata_vaddr`（已存在）并在 stage 完成后写：

```rust
pub(super) const RGXFWIF_INICFG_OS_ALL: u32 = 0x3FFFF;

pub(super) fn initialize_fw_os_config_flags(&self, flags: u32) -> Result<(), &'static str> {
    let mmu = self.mmu.as_ref().ok_or("gpu_dma_stage_mmu_missing")?;
    let osdata = self.osdata_vaddr.ok_or("gpu_dma_stage_osdata_missing")?;
    mmu.write_mapped_u32(osdata, flags)?;
    Ok(())
}
```

在 `release_firmware()` 的 catalogue/MMU 检查之后、`release_selected_meta` 之前调用，
marker 改为 `fw_config_initialized=1`。

- [ ] **Step 4: 运行测试确认通过**

Run: 同 Task P2.1 Step 2。

- [ ] **Step 5: 板级验收**

同 P2.1 Step 5；若 P2.1 的 boot stage 前进到 7 且 `FW_CONN` 出现，P2.2 已达成。
若仍超时，记录最后 `fw_state`/`boot_stage` 并停止。

- [ ] **Step 6: 提交**

```bash
git add kernel/src/device/dri/powervr_probe/dma.rs kernel/src/device/dri/powervr_probe/power.rs
git commit -m "feat(gpu): initialize the firmware OS configuration flags"
```

---

### Task P2.3: KCCB HEALTH_CHECK 提交与完成

**Files:**
- Modify: `kernel/src/device/dri/powervr_probe/dma.rs`（记录 KCCB ctl/cmd/rtn vaddr）
- Modify: `kernel/src/device/dri/powervr_probe/power.rs`（提交 + kick + 轮询）
- Test: `dma.rs`/`power.rs` ktest

**前置：** P2.1 的 `FW_CONN status=active boot_stage=7` 已出现。

- [ ] **Step 1: 写失败测试**

在 `dma.rs` ktest 中断言：
- `stage.kccb_vaddrs() == Some((ctl, cmds, rtn))`，且三者在 `FW_HEAP_BASE..FW_HEAP_MIN_END` 内；
- `stage.kccb_write_offset()` 初始为 0。
在 `power.rs` ktest 中用 fake `StartIo` 捕获 `MTS_SCHEDULE` 写入值为 0，并模拟
return slot 从 0 变为 1。

- [ ] **Step 2: 运行测试确认失败**

Run: 同 Task P2.1 Step 2。
Expected: 新访问器/提交函数未定义。

- [ ] **Step 3: 最小实现**

`dma.rs` 在 OS_OBJECTS 循环中按 `osinit_offset` 记录：
`0 => kccb_ctl`、`4 => kccb_cmds`、`8 => kccb_rtn`，并暴露：

```rust
pub(super) fn kccb_vaddrs(&self) -> Option<(usize, usize, usize)>;
pub(super) fn kccb_write_offset(&self) -> Result<u32, &'static str>;
pub(super) fn submit_kccb(&self, cmd: &[u8; 64]) -> Result<u32, &'static str>;
```

`submit_kccb`：
1. 读 ctl 的 `write_off`/`read_off`，`next=(write+1)&wrap_mask`；`next==read` 返回 `gpu_kccb_full`。
2. 把 64 B 命令按 16 个 LE `u32` 写到 `cmds + write*64`；return slot
   （`rtn + write*4`）先写 `RGXFWIF_KCCB_RTN_SLOT_NO_RESPONSE=0`。
3. 写 ctl 的 `write_off=next`（`write_mapped_u32`，写后读回）。
4. 返回 `write`。

`power.rs` 在 `FW_CONN` 成功后：

```rust
let cmd = {
    let mut c = [0u8; 64];
    c[0..4].copy_from_slice(&0x2ABC_006Fu32.to_le_bytes()); // HEALTH_CHECK
    c
};
let slot = staging.submit_kccb(&cmd)?;
let owner = hardware_power_io()
    .map_err(|reason| Error::with_message(Errno::EIO, reason))?;
let mut io = owner.lock();
io.write32(0x0B00, 0)?;  // RGX_CR_MTS_SCHEDULE, RGXFWIF_DM_GP = 0
let _ = io.read32(0x0B00)?;
drop(io);
let start = aster_time::read_monotonic_time();
loop {
    let rtn = staging.kccb_rtn(slot)?;
    if rtn & RGXFWIF_KCCB_RTN_SLOT_CMD_EXECUTED != 0 {
        break;
    }
    if aster_time::read_monotonic_time().saturating_sub(start) >= Duration::from_millis(100) {
        return Err(Error::with_message(Errno::ETIMEDOUT, "gpu_kccb_timeout"));
    }
}
aster_logger::println!(
    "ASTERINAS_POWERVR_KCCB status=completed slot={} rtn={}",
    slot, staging.kccb_rtn(slot)?
);
```

**注意：** 命令槽必须在 host 写入后对 FW 可见；`write_mapped_*` 使用的 DMA
内存是 CPU 缓存属性由 `GpuDmaAllocation` 决定，若读回一致但 FW 不响应，
在 `submit_kccb` 后加一次缓存 clean（复用 `DmaCoherent` 的同步或
`eic7700::clean_range` 的等价路径；不得直接调用未实现的 ostd API）。

- [ ] **Step 4: 运行测试确认通过**

Run: 同 Task P2.1 Step 2。

- [ ] **Step 5: 板级验收**

同 P2.1 Step 5；Expected：

```text
ASTERINAS_POWERVR_KCCB status=completed slot=0 rtn=1
```

若 `gpu_kccb_timeout`：记录 ctl 的 `write_off/read_off`、return slot 与 boot stage，
停止；不要盲目重试不同命令。

- [ ] **Step 6: 提交**

```bash
git add kernel/src/device/dri/powervr_probe/dma.rs kernel/src/device/dri/powervr_probe/power.rs
git commit -m "feat(gpu): submit and complete one KCCB health-check command"
```

---

### Task P2.4（后续增量，不在本计划验收内）: IRQ 与 fence

- 从 DT `interrupts`（probe 已校验 4 B）注册 ostd IRQ handler；
- 在 handler 中读取/确认固件事件寄存器（具体寄存器在实现前用 RockOS 参考
  的 `RGXIRQ` 路径确认，作为独立的 discovery task）；
- 用 `dma-fence` 替换 return-slot 轮询；
- 验收：KCCB 完成由中断唤醒，`ASTERINAS_POWERVR_IRQ count=N`。

本任务不启动，直到 P2.1–P2.3 的板级 marker 稳定。

---

## 验收与停止条件汇总

| 门 | 板级 marker | 失败动作 |
|---|---|---|
| P2.1 | `FW_CONN status=native started=1 faults=0 hwr_state=0x1 hwr_count=0`（`vz_fw_state`/`vz_boot_stage` 仅记录） | 记录最后的 fw_state/boot_stage，转 P2.2 或停止 |
| P2.2 | `FW_OSCONFIG fw_config_initialized=1 flags=0x1f` | 记录 OSDATA 字段读回，停止 |
| P2.3 | `KCCB status=completed slot=0 write_off=1 read_off=1`（HEALTH_CHECK 无 return-slot 契约，vendor 用 fire-and-forget；`read_off` 前进即证明 FW 已消费） | 记录 ctl 偏移/return slot，停止 |
| P2.4 | IRQ marker（后续） | 不阻塞 P2.1–P2.3 |

所有板级运行沿用 P1 的载具：
`/home/debian/asterinas-install-r1/kernel-pvr.Image` +
`stage1-pvr.cpio`（含固件与 staging 工具），probe 标志
`asterinas.powervr=1 asterinas.powervr_dma_stage=1 asterinas.powervr_release=1`。
每次提交前跑 `FEATURES=powervr_probe` 构建与 ktest，证据写入
`docs/porting/evidence/`。

---

## 2026-10-21 板级修正：native 构建没有 VZ 连接握手

**板级观察（P2.1 诊断内核，kernel SHA-256 `5dc3d23e…`）：**

```
STAGED 0..3
ASTERINAS_POWERVR_FW_CONN status=timeout reason=gpu_fw_connection_timeout fw_state=0 boot_stage=0
RELEASE_ERR [Errno 110] Connection timed out
STATUS_DECODED meta_release=1 started=1 faults=0 hwr_state=0x1 hwr_count=0 ddk=0.0
```

`SYSINIT.bFirmwareStarted=1` 且无 fault/HWR，说明 META 固件已运行；但 100 ms 内
`RGX_CR_OS0_SCRATCH3`/`RGX_CR_SCRATCH14` 始终为 0。

**根因（vendor 源码，稀疏克隆 `eswincomputing/linux-stable:linux-6.6.18-EIC7X`）：**

- `config_kernel.mk`：`RGX_NUM_DRIVERS_SUPPORTED := 1`，且未定义 `SUPPORT_AUTOVZ`
  或 `SUPPORT_AUTOVZ_HW_REGS`（`config_kernel.h`/`config_kernel.mk` 均无）。
- `services/server/devices/rgxfwutils.h:1239-1284`：
  - `SUPPORT_AUTOVZ_HW_REGS` 下 `KM_SET_OS_CONNECTION` 才写 `RGX_CR_OS0_SCRATCH2`，
    `KM_GET_FW_CONNECTION` 才读 `RGX_CR_OS0_SCRATCH3`；
  - 多驱动共享内存构建读 `psRGXFWIfConnectionCtl`；
  - **native 单驱动构建（本例）展开为常量**：`KM_GET_FW_CONNECTION` 恒为
    `RGXFW_CONNECTION_FW_ACTIVE`，`KM_SET_OS_CONNECTION` 为空操作。
- `hwdefs/volcanic/km/rgxdefs_km.h:385`：`RGX_FW_BOOT_STAGE_REGISTER = RGX_CR_SCRATCH14`，
  只在 `rgxpower.c` 的 `#if defined(TRACK_FW_BOOT)` 诊断路径里读取。
- `services/server/common/pvrsrv.c:3078-3096`：运行期兼容性等待 `sRGXCompChecks.bUpdated`
  只在 `PVRSRV_VZ_MODE_IS(GUEST)` 分支；native host 不做该等待，`ddk=0.0` 属正常。
- `services/server/devices/volcanic/rgxsrvinit.c:509-514` 与 `config_kernel.h:66`：
  native 的 `ui32FwOsConfigFlags` 默认是 `RGXFWIF_INICFG_OS_CTXSWITCH_DM_ALL = 0x1F`，
  不是 `RGXFWIF_INICFG_OS_ALL = 0x3FFFF`。

**结论：** P2.1 原定的 OS READY→FW READY/ACTIVE + INIT_DONE 握手在本 vendor 构建上
不可能出现，`fw_state=0 boot_stage=0` 是预期值，不是固件挂起。native 启动已由
P1 的 `bFirmwareStarted=1`/无 fault 证明。

**修正后的门：**

1. P2.1（已改代码）：读取 `startup_observed()`，打印
   `FW_CONN status=native started=1 faults=… hwr_state=… hwr_count=… vz_fw_state=… vz_boot_stage=…`；
   VZ 寄存器只作诊断记录，不再等待。
2. P2.2：在 `release_selected_meta` **之前**写 `OSDATA+0 = 0x1F`（vendor 默认），
   读回后打印 `FW_OSCONFIG fw_config_initialized=1 flags=0x1f`。
3. P2.3（真正的 native 连接测试）：`enqueue_health_check()` 写 64 B 命令槽、
   清 return slot、推进 `ui32WriteOffset`，然后写 `RGX_CR_MTS_SCHEDULE=0`（DM GP）
   并读回，轮询 return slot 直到 `CMD_EXECUTED=1`；marker 为
   `FW_KCCB status=executed slot=0 rtn=1 write_off=1 read_off=…`。

**ABI 交叉验证：** 用 vendor 头文件在本机编译 `offsetof` 探针，得到
`RGXFWIF_OSINIT.sRGXCompChecks=40`，`ui32DDKVersion/Build/BuildOptions/bUpdated=32/36/40/56`，
`RGXFWIF_OSDATA.ui32FwOsConfigFlags=0`，`sPowerSync=572`，`RGXFWIF_CCB_CTL`
write/read/wrap=`0/4/8`，`sizeof(RGXFWIF_KCCB_CMD)=64`，均与 probe 一致。

**ktest harness 备注：** `cargo osdk test TESTNAME` 的 TESTNAME 按测试路径
**元素后缀精确匹配**（不是帮助文本所说的子串匹配）；必须在 `kernel/` 目录下运行，
并显式给 `--target-arch riscv64 --scheme riscv`，否则会在仓库根的默认 workspace
成员上应用 `powervr_probe` 特性而失败。P2.1 的两个连接 ktest 在改成精确测试名后
均为 `1 passed; 0 failed; 339 filtered out`；修正后这两个测试随 VZ 握手一起移除，
由 `dma.rs` 的 `initialize_fw_os_config_flags` / `enqueue_health_check` ktest 取代。

**P2.3 首次板级结果（P2c，kernel SHA-256 `4d8e39e8…`）：**

```
ASTERINAS_POWERVR_FW_OSCONFIG fw_config_initialized=1 flags=0x1f
ASTERINAS_POWERVR_FW_CONN status=native started=1 faults=0 hwr_state=0x1 hwr_count=0 vz_fw_state=0 vz_boot_stage=0
ASTERINAS_POWERVR_FW_KCCB status=timeout slot=0 rtn=0 write_off=1 read_off=1
STATUS_DECODED meta_release=1 started=1 faults=0 hwr_state=0x1 hwr_count=0 ddk=0.0
```

`write_off=1` 是 host 推进的写指针；**`read_off=1` 是固件写回的读指针**，
证明固件已经消费了 slot 0 的命令——KCCB 提交、命令体和 MTS GP kick 全部生效。
`rtn=0` 是因为 `RGXFWHealthCheckCmdInt` 走的是不带 return-slot 等待的
`RGXScheduleCommand`（vendor 只对 KICK/CLEANUP/MMUCACHE 等调用
`RGXWaitForKCCBSlotUpdate`），HEALTH_CHECK 没有 return-slot 契约。
因此 P2.3 的完成判据改为 `read_off != slot`，marker 改为
`FW_KCCB status=completed slot=0 rtn=0 write_off=1 read_off=1`。

---

## 2026-10-22 P2.3 通过，下一步 P2.4

**P2e 板级 marker（kernel SHA-256 `537e9630…`）：**

```
ASTERINAS_POWERVR_FW_OSCONFIG fw_config_initialized=1 flags=0x1f
ASTERINAS_POWERVR_FW_CONN status=native started=1 faults=0 hwr_state=0x1 hwr_count=0 vz_fw_state=0 vz_boot_stage=0
ASTERINAS_POWERVR_FW_KCCB status=completed slot=0 rtn=0 write_off=1 read_off=1
STATUS_DECODED meta_release=1 started=1 faults=0 hwr_state=0x1 hwr_count=0 ddk=0.0
```

`read_off=1` 证明固件消费了 HEALTH_CHECK；`rtn=0` 符合 vendor
`RGXFWHealthCheckCmdInt` 的 fire-and-forget 语义。P2.1–P2.3 的板级 marker
已稳定，P2.4 的启动条件解除。

**P2.4（下一步）**：把 return-slot/`read_off` 轮询替换为 `RGXIRQ` 中断 +
fence；先完成 IRQ 注册与 `ASTERINAS_POWERVR_IRQ count=N` 收据，再考虑
`dma-fence` 和用户态接口。注意：native 下 `compatibility=0/ddk=0.0` 是
预期值，不阻塞 P2.4；若需要 return-slot 证据，应改用
KICK/CLEANUP/MMUCACHE 等会走 `RGXWaitForKCCBSlotUpdate` 的命令。

---

## P3 决策简报（1 页）：用户态 GPU 栈路线（供人工拍板，不实现）

**背景**：P2.3 已证明 Asterinas 能启动 AXM-8-256 固件并让固件消费 KCCB
命令。P3 的目标是把 GPU 变成可用的 DRM/GLES 设备（Firefox 硬件加速），
核心决策是复用厂商 C 核心还是纯 Rust 重写。

### 方案 A：厂商 C 核心 + Rust OS/DRM 层

保留 pvrsrvkm 的 services 内核 C 代码作为 GPU 服务（建议跑在用户态服务
进程，而不是 framekernel 内核里），Rust 侧只保留 P2 已有的固件启动、
MMU、IRQ、命令传输；用户态 UM DDK（`libsrv_um`/`libusc`）经 Asterinas
syscall/共享内存访问该服务。

| 改动 | 估计 |
|---|---|
| C services 适配 Asterinas 的 physmem/MMU/PMR/DMA/IRQ | 1.5–3 人月 |
| kernel↔service IPC（共享内存环、IRQ 转发、fence） | 1–2 人月 |
| UM DDK 需要的 Linux ABI（ioctl/mmap/dma-buf/sync） | 1–2 人月 |
| DRM 节点、权限、调试接口 | 0.5 人月 |
| **合计** | **4–7 人月** |

**风险**：framekernel 政策要求 unsafe C 不进 safe kernel，必须服务化并
承担 IPC 延迟/复杂度；ABI 锁定 DDK 24.2；闭源固件与二进制分发；后续
跟随厂商维护。**优点**：最快接近 RockOS 已验证的功能，行为可对照。

### 方案 B：纯 Rust 重写

以 P2 probe 为基础，纯 Rust 实现 KCCB/FWCCB、MMU 上下文、作业提交、
DRM UAPI；用户态用 Mesa，但上游 `powervr` 的 UAPI 与本 vendor 固件
不一致，需要自写或修改 Mesa 后端。

| 改动 | 估计 |
|---|---|
| P3 内核驱动（KCCB/FWCCB/MMU/作业/DRM） | 4–8 人月 |
| Mesa/GLES 后端（vendor 固件 UAPI） | 3–5 人月 |
| DRM UAPI、fence/sync、权限 | 1–2 人月 |
| 性能与稳定性 | 1–2 人月 |
| **合计** | **9–16 人月** |

**风险**：vendor 固件 ABI 文档缺失（需逆向/实测）、上游 Mesa 不兼容、
首个像素时间长、功能覆盖不确定。**优点**：符合 framekernel 政策、无 C/
ABI 锁定、长期可维护。

### 对比与建议

| 维度 | A（C 核心 + Rust 层） | B（纯 Rust） |
|---|---|---|
| 首个像素时间 | 短（复用已验证栈） | 长 |
| 成本 | 4–7 人月 | 9–16 人月 |
| framekernel 政策 | 需服务化规避 | 天然符合 |
| ABI 锁定 | DDK 24.2 | 自定 UAPI |
| 维护成本 | 跟随厂商 | 自有 |

**建议**：若目标是“尽快让 Firefox 用上 GPU”，选 A，并先做 1–2 周
spike（用户态服务 + 一个 KCCB 命令 + UM DDK ioctl 面）验证成本；若目标
是“Asterinas 自有长期 GPU 栈”，选 B。本节只供拍板，不在本轮实现。

---

## 2026-10-22 P2.4 通过

**P2f 板级 marker（kernel SHA-256 `493d297a…`）：**

```
ASTERINAS_POWERVR_IRQ status=installed source=15 parent=0x10 count=0
ASTERINAS_POWERVR_IRQ count=1 spurious=0 last_status=0x1 mts_status=0x0
ASTERINAS_POWERVR_FW_KCCB status=completed slot=0 rtn=0 write_off=1 read_off=1 wake=irq irq_count=1
```

P2.1–P2.3 marker 全部保持原样。IRQ 路径为 DT `interrupts=<15>` 的专用 PLIC
事件线；handler 按 vendor `RGXAckIrqDedicated` 读写
`RGX_CR_IRQ_OS0_EVENT_STATUS`/`CLEAR`（`0x0BD0`/`0x0BE0`，mask `0x3`）。
dma-fence 在本树不可行（无子系统），替代为 IRQ 计数 + `read_off` 完成判据，
详见 `docs/porting/evidence/2026-10-22-asterinas-powervr-irq.md`。

**下一步：P3 路线拍板**。上面的「P3 决策简报」给出 A/B 两案；P3 实现不在
本计划内，等人工选定后再开新计划。

---

## 2026-10-22 P3 路线拍板：方案 B（纯 Rust）

用户已确认 P3 走**纯 Rust 重写**，不再按 A/B 待定处理。内核侧按能力递进拆成
B1–B5，每项独立 bounded、单独证据与 commit：

| 项 | 内容 | 验收物 |
|---|---|---|
| **B1（本轮）** | KCCB/FWCCB 完整命令队列：多 slot、写指针推进、return slot 回读、IRQ 唤醒完成等待 | ktest + `ASTERINAS_POWERVR_KCCB_PIPELINE count=N` + `docs/porting/evidence/2026-10-22-asterinas-powervr-kccb-pipeline.md` |
| B1b（后续） | OS-online 握手（`RGXFWIF_OS_STATE_CHANGE_DATA` 最小字段 + ONLINE 状态位） | 板级 marker；若固件拒绝则记录原因并保持诊断命令流水 |
| B2 | 固件 MMU 上下文 + GPU VA/页表 + PMR 等价缓冲映射 | 作业可读写缓冲的板级 marker/ktest |
| B3 | DRM render node + GEM/PRIME（PMR handle 或池偏移二选一） | libdrm 风格 create/map/export/import 收据 |
| B4 | fence/sync：时间线、可等待 fd、dma-fence 等价物（syncobj 或 PVR sw_sync 语义） | kick 完成可 poll 的收据 |
| B5 | GLES 后端路线 | 路线拍板 + 首个 GLES clear 收据 |

**B5 预研备注**：上游 Mesa 的 PowerVR 驱动是 **Vulkan-only**
（`src/imagination`，支持 Rogue 起的 GPU；`pvrsrvkm` winsys 是 Rogue 时代的
bridge ABI，与 DDK 24.2 Volcanic 的函数编号/结构不兼容）。B5 需在
“复活/移植 Mesa Vulkan + Zink”与“自写最小 GLES”之间再拍板，不阻塞 B1–B4。

---

## 2026-10-22 B2 进展（主机侧）

- `gpu_job_noop_context_maps_buffer` 与
  `gpu_job_noop_submits_flush_command` 两个 ktest 在最终树通过
  （各 `1 passed; 0 failed; 343 filtered out`）；
- 作业缓冲映射在现有固件 MMU 上下文（`FW_ALIGN_VADDR + 2*PAGE_SIZE`），
  用 vendor 同款 `RGXFWIF_KCCB_CMD_SLCFLUSHINVAL` 做 context-free 范围
  flush，复用 P2.4 IRQ 唤醒等待 return slot；
- 作业路径 opt-in：cmdline `asterinas.powervr_job_noop=1` 才启用，P2/P3
  marker 语义不变；
- 板级 marker `ASTERINAS_POWERVR_JOB status=completed kind=noop ctx=0 …`
  延后：板子在上传启动的网络大传输中挂死，需物理复位后先补 B1、再补 B2；
- **B2a（下一步）**：真正的 per-job MMU 上下文（context ID/独立页表根/
  上下文切换）+ `RGXFWIF_KCCB_CMD_MMUCACHE` 失效 + OS-online（B1b）；
  板级补 B1/B2 收据后再进入 B3（DRM render node + GEM/PRIME）。
