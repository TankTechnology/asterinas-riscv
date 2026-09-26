// SPDX-License-Identifier: MPL-2.0

//! Opt-in, reversible PowerVR ID read after an exact Megrez CRG gate.

use core::{
    hint::spin_loop,
    sync::atomic::{AtomicU8, Ordering},
    time::Duration,
};

use device_id::{DeviceId, MinorId};
use ostd::{io::IoMem, mm::VmIoOnce, sync::Mutex};
use spin::Once;

use super::{
    CRG_BASE, CRG_GATE_BIT, CrgSnapshot, GPU_ACLK_OFFSET, GPU_CFG_OFFSET, GPU_GRAY_OFFSET,
    GPU_REG_SIZE, GPU_REG_START, GPU_RESET_OFFSET, inspect_gpu_crg_dt, print_gpu_crg_snapshot,
};
use crate::{
    device::{Device, DeviceType, DevtmpfsInodeMeta, registry::char},
    events::IoEvents,
    fs::{
        file::{PerOpenFileOps, StatusFlags},
        vfs::inode::FileOps,
    },
    prelude::*,
    process::{
        UserNamespace,
        credentials::capabilities::CapSet,
        posix_thread::AsPosixThread,
        signal::{PollHandle, Pollable},
    },
    security::lsm::hooks as lsm_hooks,
};

const EXPECTED_INITIAL: CrgSnapshot = CrgSnapshot {
    aclk: 0x20,
    cfg: 0,
    gray: 0,
    reset: 0,
};
const EXPECTED_GPU_ID: u64 = 0x001e_0003_0198_0065;
const GPU_ID_OFFSET: usize = 0x20;
const LEASE_IDLE: u8 = 0;
const LEASE_CLAIMING: u8 = 1;
const LEASE_ACTIVE: u8 = 2;
const LEASE_POISONED: u8 = 3;

static POWER_LEASE: AtomicU8 = AtomicU8::new(LEASE_IDLE);
static CONTROL_MAJOR: Once<char::MajorIdOwner> = Once::new();

trait PowerIo {
    fn snapshot(&mut self) -> Result<CrgSnapshot, &'static str>;
    fn write_clock(&mut self, index: usize, value: u32) -> Result<(), &'static str>;
    fn write_reset(&mut self, value: u32) -> Result<(), &'static str>;
    fn delay_reset_pulse(&mut self) -> Result<(), &'static str>;
    fn read_gpu_id(&mut self) -> Result<u64, &'static str>;
}

fn write_clock_checked(
    io: &mut impl PowerIo,
    index: usize,
    value: u32,
) -> Result<(), &'static str> {
    io.write_clock(index, value)?;
    let observed = io.snapshot()?;
    let actual = match index {
        0 => observed.aclk,
        1 => observed.cfg,
        2 => observed.gray,
        _ => return Err("invalid_clock_index"),
    };
    if actual != value {
        return Err("clock_readback_mismatch");
    }
    Ok(())
}

fn write_reset_checked(io: &mut impl PowerIo, value: u32) -> Result<(), &'static str> {
    io.write_reset(value)?;
    if io.snapshot()?.reset != value {
        return Err("reset_readback_mismatch");
    }
    Ok(())
}

fn restore_crg(io: &mut impl PowerIo, initial: CrgSnapshot) -> Result<CrgSnapshot, &'static str> {
    // Assert reset before gating clocks, matching the RockOS device deinit.
    // Attempt all writes even if one fails so a partial restore is visible.
    let mut restored = write_reset_checked(io, initial.reset).is_ok();
    for (index, value) in [(2, initial.gray), (1, initial.cfg), (0, initial.aclk)] {
        restored &= write_clock_checked(io, index, value).is_ok();
    }
    let observed = io.snapshot()?;
    if !restored || observed != initial {
        return Err("crg_restore_failed");
    }
    Ok(observed)
}

fn start_power_session(io: &mut impl PowerIo) -> Result<CrgSnapshot, &'static str> {
    let initial = io.snapshot()?;
    if initial != EXPECTED_INITIAL {
        return Err("unexpected_initial_crg");
    }

    let attempt = (|| {
        for (index, value) in [
            initial.aclk | CRG_GATE_BIT,
            initial.cfg | CRG_GATE_BIT,
            initial.gray | CRG_GATE_BIT,
        ]
        .into_iter()
        .enumerate()
        {
            write_clock_checked(io, index, value)?;
        }
        let mut reset = initial.reset;
        for mask in [1, 2, 4, 8, 16] {
            io.delay_reset_pulse()?;
            reset |= mask;
            write_reset_checked(io, reset)?;
        }
        if io.snapshot()?
            != (CrgSnapshot {
                aclk: initial.aclk | CRG_GATE_BIT,
                cfg: initial.cfg | CRG_GATE_BIT,
                gray: initial.gray | CRG_GATE_BIT,
                reset: 0x1f,
            })
        {
            return Err("powered_crg_readback_mismatch");
        }
        let id = io.read_gpu_id()?;
        if id != EXPECTED_GPU_ID {
            return Err("unexpected_gpu_id");
        }
        Ok(())
    })();

    if let Err(reason) = attempt {
        restore_crg(io, initial)?;
        return Err(reason);
    }
    Ok(initial)
}

#[must_use]
struct PowerSession<'a, I: PowerIo> {
    io: &'a mut I,
    initial: CrgSnapshot,
    restored: bool,
}

impl<'a, I: PowerIo> PowerSession<'a, I> {
    fn start(io: &'a mut I) -> Result<Self, &'static str> {
        let initial = start_power_session(io)?;
        Ok(Self {
            io,
            initial,
            restored: false,
        })
    }

    fn restore(mut self) -> Result<(), &'static str> {
        restore_crg(self.io, self.initial)?;
        self.restored = true;
        Ok(())
    }
}

impl<I: PowerIo> Drop for PowerSession<'_, I> {
    fn drop(&mut self) {
        if !self.restored {
            if let Err(reason) = restore_crg(self.io, self.initial) {
                aster_logger::println!(
                    "ASTERINAS_GPU_POWERED_ID status=restore_failed reason={}",
                    reason
                );
            }
        }
    }
}

fn run_powered_id(io: &mut impl PowerIo) -> Result<u64, &'static str> {
    PowerSession::start(io)?.restore()?;
    Ok(EXPECTED_GPU_ID)
}

fn claim_power(io: &mut impl PowerIo, lease: &AtomicU8) -> Result<CrgSnapshot, &'static str> {
    lease
        .compare_exchange(
            LEASE_IDLE,
            LEASE_CLAIMING,
            Ordering::AcqRel,
            Ordering::Acquire,
        )
        .map_err(|_| "gpu_control_busy")?;
    match start_power_session(io) {
        Ok(initial) => {
            lease.store(LEASE_ACTIVE, Ordering::Release);
            Ok(initial)
        }
        Err(reason) => {
            let next = if reason == "crg_restore_failed" {
                LEASE_POISONED
            } else {
                LEASE_IDLE
            };
            lease.store(next, Ordering::Release);
            Err(reason)
        }
    }
}

fn release_power(
    io: &mut impl PowerIo,
    lease: &AtomicU8,
    initial: CrgSnapshot,
) -> Result<CrgSnapshot, &'static str> {
    let result = restore_crg(io, initial);
    lease.store(
        if result.is_ok() {
            LEASE_IDLE
        } else {
            LEASE_POISONED
        },
        Ordering::Release,
    );
    result
}

struct HardwarePowerIo {
    clocks: IoMem,
    reset: IoMem,
    gpu: IoMem,
}

// The IoMem allocator does not recycle acquired ranges. Keep the CRG and GPU
// mappings together so a later selected GPU session can reuse this owner.
static HARDWARE_POWER_IO: Once<Result<Mutex<HardwarePowerIo>, &'static str>> = Once::new();

impl HardwarePowerIo {
    fn new() -> Result<Self, &'static str> {
        Ok(Self {
            clocks: IoMem::acquire(CRG_BASE + GPU_ACLK_OFFSET..CRG_BASE + GPU_GRAY_OFFSET + 4)
                .map_err(|_| "clock_registers_unavailable")?,
            reset: IoMem::acquire(CRG_BASE + GPU_RESET_OFFSET..CRG_BASE + GPU_RESET_OFFSET + 4)
                .map_err(|_| "reset_register_unavailable")?,
            gpu: IoMem::acquire(GPU_REG_START..GPU_REG_START + GPU_REG_SIZE)
                .map_err(|_| "gpu_registers_unavailable")?,
        })
    }
}

fn hardware_power_io() -> Result<&'static Mutex<HardwarePowerIo>, &'static str> {
    HARDWARE_POWER_IO
        .call_once(|| HardwarePowerIo::new().map(Mutex::new))
        .as_ref()
        .map_err(|reason| *reason)
}

fn check_control_access() -> Result<()> {
    let thread = current_thread!();
    let posix_thread = thread.as_posix_thread().unwrap();
    let initial_user_ns = UserNamespace::get_init_singleton();
    lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
        initial_user_ns.as_ref(),
        posix_thread,
        CapSet::SYS_RAWIO,
    ))
}

#[derive(Debug)]
struct PowerControlDevice {
    id: DeviceId,
}

impl Device for PowerControlDevice {
    fn type_(&self) -> DeviceType {
        DeviceType::Char
    }

    fn id(&self) -> DeviceId {
        self.id
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsInodeMeta<'_>> {
        Some(DevtmpfsInodeMeta::new("powervr-control"))
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        check_control_access()?;
        let owner =
            hardware_power_io().map_err(|reason| Error::with_message(Errno::ENODEV, reason))?;
        let mut io = owner.lock();
        let initial = claim_power(&mut *io, &POWER_LEASE).map_err(|reason| {
            Error::with_message(
                if reason == "gpu_control_busy" {
                    Errno::EBUSY
                } else {
                    Errno::EIO
                },
                reason,
            )
        })?;
        aster_logger::println!(
            "ASTERINAS_POWERVR_OWNER session=opened bvnc=30.3.408.101 gates={:#05b} deasserted={:#07b}",
            0b111,
            0b11111,
        );
        Ok(Box::new(PowerControlFile { initial }))
    }
}

struct PowerControlFile {
    initial: CrgSnapshot,
}

impl Drop for PowerControlFile {
    fn drop(&mut self) {
        let Ok(owner) = hardware_power_io() else {
            POWER_LEASE.store(LEASE_POISONED, Ordering::Release);
            aster_logger::println!(
                "ASTERINAS_POWERVR_OWNER session=close_failed reason=owner_unavailable"
            );
            return;
        };
        let mut io = owner.lock();
        match release_power(&mut *io, &POWER_LEASE, self.initial) {
            Ok(observed) => aster_logger::println!(
                "ASTERINAS_POWERVR_OWNER session=closed crg_restored=1 aclk={:#010x} cfg={:#010x} gray={:#010x} reset={:#010x}",
                observed.aclk,
                observed.cfg,
                observed.gray,
                observed.reset,
            ),
            Err(reason) => aster_logger::println!(
                "ASTERINAS_POWERVR_OWNER session=close_failed reason={}",
                reason
            ),
        }
    }
}

impl Pollable for PowerControlFile {
    fn poll(&self, _mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        IoEvents::empty()
    }
}

impl FileOps for PowerControlFile {
    fn read_at(
        &self,
        _offset: usize,
        _writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "GPU control does not support read");
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "GPU control does not support write");
    }
}

impl PerOpenFileOps for PowerControlFile {
    fn check_seekable(&self) -> Result<()> {
        return_errno!(Errno::ESPIPE);
    }

    fn is_offset_aware(&self) -> bool {
        false
    }
}

pub(super) fn register_control_on_request(emit_crg_snapshot: bool) -> Result<(), &'static str> {
    inspect_gpu_crg_dt()?;
    let owner = hardware_power_io()?;
    let mut io = owner.lock();
    if emit_crg_snapshot {
        print_gpu_crg_snapshot(io.snapshot()?);
    }
    run_powered_id(&mut *io)?;
    if io.snapshot()? != EXPECTED_INITIAL {
        return Err("crg_restore_failed");
    }
    drop(io);
    let major = char::allocate_major().map_err(|_| "gpu_major_unavailable")?;
    let id = DeviceId::new(major.get(), MinorId::new(0));
    char::register(Arc::new(PowerControlDevice { id }))
        .map_err(|_| "gpu_control_registration_failed")?;
    CONTROL_MAJOR.call_once(|| major);
    Ok(())
}

impl PowerIo for HardwarePowerIo {
    fn snapshot(&mut self) -> Result<CrgSnapshot, &'static str> {
        Ok(CrgSnapshot {
            aclk: self.clocks.read_once(0).map_err(|_| "clock_read_failed")?,
            cfg: self
                .clocks
                .read_once(GPU_CFG_OFFSET - GPU_ACLK_OFFSET)
                .map_err(|_| "clock_read_failed")?,
            gray: self
                .clocks
                .read_once(GPU_GRAY_OFFSET - GPU_ACLK_OFFSET)
                .map_err(|_| "clock_read_failed")?,
            reset: self.reset.read_once(0).map_err(|_| "reset_read_failed")?,
        })
    }

    fn write_clock(&mut self, index: usize, value: u32) -> Result<(), &'static str> {
        let offset = match index {
            0..=2 => index * 4,
            _ => return Err("invalid_clock_index"),
        };
        self.clocks
            .write_once(offset, &value)
            .map_err(|_| "clock_write_failed")
    }

    fn write_reset(&mut self, value: u32) -> Result<(), &'static str> {
        self.reset
            .write_once(0, &value)
            .map_err(|_| "reset_write_failed")
    }

    fn delay_reset_pulse(&mut self) -> Result<(), &'static str> {
        let deadline = aster_time::read_monotonic_time()
            .checked_add(Duration::from_micros(15))
            .ok_or("reset_delay_overflow")?;
        while aster_time::read_monotonic_time() < deadline {
            spin_loop();
        }
        Ok(())
    }

    fn read_gpu_id(&mut self) -> Result<u64, &'static str> {
        let id = self
            .gpu
            .read_once::<u64>(GPU_ID_OFFSET)
            .map_err(|_| "gpu_id_read_failed")?;
        aster_logger::println!("ASTERINAS_GPU_POWERED_ID raw={:#018x}", id);
        Ok(id)
    }
}

pub(super) fn probe_on_request(emit_crg_snapshot: bool) {
    if let Err(reason) = inspect_gpu_crg_dt() {
        if emit_crg_snapshot {
            aster_logger::println!("ASTERINAS_GPU_CRG_PROBE status=skipped reason={}", reason);
        }
        aster_logger::println!("ASTERINAS_GPU_POWERED_ID status=skipped reason={}", reason);
        return;
    }
    let mut io = match hardware_power_io() {
        Ok(io) => io.lock(),
        Err(reason) => {
            if emit_crg_snapshot {
                aster_logger::println!("ASTERINAS_GPU_CRG_PROBE status=skipped reason={}", reason);
            }
            aster_logger::println!("ASTERINAS_GPU_POWERED_ID status=skipped reason={}", reason);
            return;
        }
    };
    if emit_crg_snapshot {
        match io.snapshot() {
            Ok(snapshot) => print_gpu_crg_snapshot(snapshot),
            Err(reason) => {
                aster_logger::println!("ASTERINAS_GPU_CRG_PROBE status=skipped reason={}", reason);
                aster_logger::println!("ASTERINAS_GPU_POWERED_ID status=skipped reason={}", reason);
                return;
            }
        }
    }
    aster_logger::println!("ASTERINAS_GPU_POWERED_ID status=starting");
    match run_powered_id(&mut *io) {
        Ok(_) => aster_logger::println!(
            "ASTERINAS_GPU_POWERED_ID status=validated bvnc=30.3.408.101 crg_restored=1 firmware=untouched render=unavailable"
        ),
        Err(reason) => {
            aster_logger::println!("ASTERINAS_GPU_POWERED_ID status=skipped reason={}", reason)
        }
    }
}

#[cfg(ktest)]
mod tests {
    use alloc::{vec, vec::Vec};

    use ostd::prelude::ktest;

    use super::*;

    #[derive(Debug, Eq, PartialEq)]
    enum Action {
        Clock(usize, u32),
        Reset(u32),
        Delay,
        ReadId,
    }

    struct FakePowerIo {
        state: CrgSnapshot,
        id: u64,
        actions: Vec<Action>,
        reject_reset: Option<u32>,
    }

    impl FakePowerIo {
        fn new(id: u64) -> Self {
            Self {
                state: CrgSnapshot {
                    aclk: 0x20,
                    cfg: 0,
                    gray: 0,
                    reset: 0,
                },
                id,
                actions: Vec::new(),
                reject_reset: None,
            }
        }
    }

    impl PowerIo for FakePowerIo {
        fn snapshot(&mut self) -> Result<CrgSnapshot, &'static str> {
            Ok(self.state)
        }

        fn write_clock(&mut self, index: usize, value: u32) -> Result<(), &'static str> {
            match index {
                0 => self.state.aclk = value,
                1 => self.state.cfg = value,
                2 => self.state.gray = value,
                _ => return Err("invalid_clock_index"),
            }
            self.actions.push(Action::Clock(index, value));
            Ok(())
        }

        fn write_reset(&mut self, value: u32) -> Result<(), &'static str> {
            if self.reject_reset == Some(value) {
                return Err("reset_write_failed");
            }
            self.state.reset = value;
            self.actions.push(Action::Reset(value));
            Ok(())
        }

        fn delay_reset_pulse(&mut self) -> Result<(), &'static str> {
            self.actions.push(Action::Delay);
            Ok(())
        }

        fn read_gpu_id(&mut self) -> Result<u64, &'static str> {
            self.actions.push(Action::ReadId);
            Ok(self.id)
        }
    }

    #[ktest]
    fn powered_id_sequence_restores_original_crg() {
        let mut io = FakePowerIo::new(0x001e_0003_0198_0065);
        assert_eq!(run_powered_id(&mut io), Ok(0x001e_0003_0198_0065));
        assert_eq!(io.state.aclk, 0x20);
        assert_eq!(io.state.cfg, 0);
        assert_eq!(io.state.gray, 0);
        assert_eq!(io.state.reset, 0);
        assert_eq!(
            io.actions,
            vec![
                Action::Clock(0, 0x8000_0020),
                Action::Clock(1, 0x8000_0000),
                Action::Clock(2, 0x8000_0000),
                Action::Delay,
                Action::Reset(1),
                Action::Delay,
                Action::Reset(3),
                Action::Delay,
                Action::Reset(7),
                Action::Delay,
                Action::Reset(15),
                Action::Delay,
                Action::Reset(31),
                Action::ReadId,
                Action::Reset(0),
                Action::Clock(2, 0),
                Action::Clock(1, 0),
                Action::Clock(0, 0x20),
            ]
        );
    }

    #[ktest]
    fn powered_session_retains_clocks_until_explicit_restore() {
        let mut io = FakePowerIo::new(EXPECTED_GPU_ID);
        let original = start_power_session(&mut io).unwrap();
        assert_eq!(original, EXPECTED_INITIAL);
        assert_eq!(io.state.clock_gates(), 0b111);
        assert_eq!(io.state.deasserted_resets(), 0b11111);

        restore_crg(&mut io, original).unwrap();
        assert_eq!(io.state, EXPECTED_INITIAL);
    }

    #[ktest]
    fn dropping_power_session_restores_original_crg() {
        let mut io = FakePowerIo::new(EXPECTED_GPU_ID);
        {
            let session = PowerSession::start(&mut io).unwrap();
            assert_eq!(session.io.snapshot().unwrap().clock_gates(), 0b111);
        }
        assert_eq!(io.state, EXPECTED_INITIAL);
    }

    #[ktest]
    fn exclusive_power_lease_restores_on_release() {
        let mut io = FakePowerIo::new(EXPECTED_GPU_ID);
        let lease = AtomicU8::new(LEASE_IDLE);
        let original = claim_power(&mut io, &lease).unwrap();
        assert_eq!(lease.load(Ordering::Acquire), LEASE_ACTIVE);
        assert_eq!(claim_power(&mut io, &lease), Err("gpu_control_busy"));
        assert_eq!(io.state.clock_gates(), 0b111);
        assert_eq!(
            release_power(&mut io, &lease, original),
            Ok(EXPECTED_INITIAL)
        );
        assert_eq!(lease.load(Ordering::Acquire), LEASE_IDLE);
        assert_eq!(io.state, EXPECTED_INITIAL);
    }

    #[ktest]
    fn failed_power_lease_releases_or_poisons_ownership() {
        let mut io = FakePowerIo::new(0);
        let lease = AtomicU8::new(LEASE_IDLE);
        assert_eq!(claim_power(&mut io, &lease), Err("unexpected_gpu_id"));
        assert_eq!(lease.load(Ordering::Acquire), LEASE_IDLE);
        assert_eq!(io.state, EXPECTED_INITIAL);

        let mut io = FakePowerIo::new(EXPECTED_GPU_ID);
        io.reject_reset = Some(0);
        let original = claim_power(&mut io, &lease).unwrap();
        assert_eq!(
            release_power(&mut io, &lease, original),
            Err("crg_restore_failed")
        );
        assert_eq!(lease.load(Ordering::Acquire), LEASE_POISONED);
        assert_eq!(claim_power(&mut io, &lease), Err("gpu_control_busy"));
    }

    #[ktest]
    fn powered_id_mismatch_and_initial_drift_cannot_leave_gpu_enabled() {
        let mut io = FakePowerIo::new(0);
        assert_eq!(run_powered_id(&mut io), Err("unexpected_gpu_id"));
        assert_eq!(io.state.aclk, 0x20);
        assert_eq!(io.state.cfg, 0);
        assert_eq!(io.state.gray, 0);
        assert_eq!(io.state.reset, 0);
        assert_eq!(io.actions.last(), Some(&Action::Clock(0, 0x20)));

        let mut io = FakePowerIo::new(0x001e_0003_0198_0065);
        io.state.reset = 1;
        assert_eq!(run_powered_id(&mut io), Err("unexpected_initial_crg"));
        assert!(io.actions.is_empty());
    }

    #[ktest]
    fn powered_id_reset_write_failure_restores_all_registers() {
        let mut io = FakePowerIo::new(EXPECTED_GPU_ID);
        io.reject_reset = Some(7);
        assert_eq!(run_powered_id(&mut io), Err("reset_write_failed"));
        assert_eq!(io.state, EXPECTED_INITIAL);
        assert!(!io.actions.contains(&Action::ReadId));
        assert_eq!(io.actions.last(), Some(&Action::Clock(0, 0x20)));
    }
}
