# Megrez PowerVR P2：FWIF 连接与一个 KCCB 命令实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 P1 已启动的 META 固件上完成 native 连接握手（OS/FW READY→ACTIVE、boot stage 到 `INIT_DONE`），并让固件执行一个 `RGXFWIF_KCCB_CMD_HEALTH_CHECK` 命令、写回 return slot，最后读取兼容性字段。

**Architecture:** 全部在现有 Rust probe 内扩展，不引入 C DDK：`power.rs` 在 `release_firmware()` 成功后通过 `StartIo` 访问 GPU 寄存器完成连接握手；`fwif.rs`/`dma.rs` 暴露 KCCB control/命令/return-slot 的 vaddr 与写入方法；用户态 staging 工具沿用 P1 的 PVR1/PVRR 协议。IRQ 留到 P2.4（本计划先轮询 return slot）。

**Tech Stack:** Rust（`kernel/src/device/dri/powervr_probe/`）、`ostd` IoMem/DMA、`aster_time`、cargo-osdk ktest、板级串口 marker、RockOS 参考寄存器定义（`hwdefs/volcanic/km/rgx_cr_defs_km.h` 与 Volcanic `rgx_fwif_km.h`）。

**Scope gate:** P2.1 若在 100 ms 内看不到 `FW_READY/ACTIVE` 或 boot stage 不前进，先停 P2.2 分析，不做 KCCB。P2.3 的验收是**一个命令完成**，不是渲染。

---

## 事实基线（实现时引用）

| 项 | 值 |
|---|---|
| OS connection | `RGX_CR_OS0_SCRATCH2` offset `0x0890`；`OS_OFFLINE=0, READY=1, ACTIVE=2` |
| FW connection | `RGX_CR_OS0_SCRATCH3` offset `0x0898`；`FW_OFFLINE=0, READY=1, ACTIVE=2` |
| FW boot stage | `RGX_CR_SCRATCH14` offset `0x0870`；`FW_BOOT_INIT_DONE=7` |
| KCCB control | `RGXFWIF_CCB_CTL { write_off, read_off, wrap_mask }`，offset 单位=命令槽；probe 用 `wrap_mask=(1<<10)-1` |
| KCCB 槽 | 64 B；命令类型 `eCmdType` 带 `RGX_CMD_MAGIC_DWORD(0x2ABC)<<16` |
| HEALTH_CHECK | `RGXFWIF_KCCB_CMD_HEALTH_CHECK = 111 | (0x2ABC<<16)` = `0x2ABC006F` |
| Return slot | `u32`/槽；host 先写 `NO_RESPONSE=0`，FW 完成写 `CMD_EXECUTED=1`（实现里定义 `RGXFWIF_KCCB_RTN_SLOT_CMD_EXECUTED: u32 = 1`） |
| Kick | `RGX_CR_MTS_SCHEDULE` offset `0x0B00`，`RGXFWIF_DM_GP=0`，写 `0` 后读回 |
| OSDATA 配置 | probe 已写 `osdata+572 = sPowerSync` FW addr；`ui32FwOsConfigFlags` 在 `osdata+0`（P2.2 按需写 `RGXFWIF_INICFG_OS_ALL=0x3FFFF`） |
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
| P2.1 | `FW_CONN status=active fw_state=2 boot_stage=7` | 记录最后的 fw_state/boot_stage，转 P2.2 或停止 |
| P2.2 | `FW_CONN` 在写入 `INICFG_OS_ALL` 后出现 | 记录 OSDATA 字段读回，停止 |
| P2.3 | `KCCB status=completed slot=0 rtn=1` | 记录 ctl 偏移/return slot，停止 |
| P2.4 | IRQ marker（后续） | 不阻塞 P2.1–P2.3 |

所有板级运行沿用 P1 的载具：
`/home/debian/asterinas-install-r1/kernel-pvr.Image` +
`stage1-pvr.cpio`（含固件与 staging 工具），probe 标志
`asterinas.powervr=1 asterinas.powervr_dma_stage=1 asterinas.powervr_release=1`。
每次提交前跑 `FEATURES=powervr_probe` 构建与 ktest，证据写入
`docs/porting/evidence/`。
