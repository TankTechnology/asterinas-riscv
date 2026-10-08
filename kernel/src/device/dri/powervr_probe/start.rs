// SPDX-License-Identifier: MPL-2.0

//! Selected Volcanic META reset and bus preparation with the processor held.

use core::{
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

pub(super) trait FirmwareStartIo {
    fn monotonic_time(&mut self) -> Duration;
    fn observe_start(&mut self) -> Result<bool, &'static str>;
    fn wait_interval(&mut self) -> Result<(), &'static str>;
}

/// Observe the native started flag with a one-second deadline and an
/// independent read limit. The observer must reject firmware faults before
/// reporting startup; a successful observation does not prove command execution.
pub(super) fn wait_selected_firmware(io: &mut impl FirmwareStartIo) -> Result<(), &'static str> {
    let deadline = io
        .monotonic_time()
        .checked_add(Duration::from_secs(1))
        .ok_or("gpu_firmware_start_deadline_overflow")?;
    // The eventual hardware adapter must use a bounded wait interval and
    // keep the GPU owner and its DMA allocations alive throughout this call.
    for attempt in 0..1000 {
        if io.monotonic_time() >= deadline {
            return Err("gpu_firmware_start_timeout");
        }
        if io.observe_start()? {
            return if io.monotonic_time() < deadline {
                Ok(())
            } else {
                Err("gpu_firmware_start_timeout")
            };
        }
        if attempt != 999 {
            io.wait_interval()?;
        }
    }
    Err("gpu_firmware_start_timeout")
}

// Values are measured from the pinned RockOS Volcanic headers by
// tools/riscv/drm/measure_rgx_start_registers.sh. This is only the prefix of
// RGXStart: the firmware catalogue and META release are separate operations.
const SYS_BUS_SECURE: usize = 0xa100;
const MERCER_SOFT_RESET: usize = 0x0630;
const TEXAS_SOFT_RESET: usize = 0x0640;
const SWIFT_SOFT_RESET: usize = 0x0650;
const SOFT_RESET: usize = 0x0100;
const META_BOOT: usize = 0x0bf8;
const META_WRAPPER: usize = 0x0b50;
const MMU_RANGE_ONE: usize = 0xe350;
const ACE_CTRL: usize = 0xe320;

const MERCER0: u64 = 0x1249_2492_4924_9249;
const MERCER1: u64 = 0x2492_4924_9249_2492;
const MERCER2: u64 = 0x4924_9249_2492_4924;
const JONES_ALL: u64 = 0x3860_0000_0800;
const EXTRA: u64 = 0x80f2;
pub(super) const GARTEN_RESET: u64 = 0x20_0000_0000;

const MMU_NON4K_16K: u64 = 0x6f_fffd_c000;
const MMU_GLOBAL_4K: u64 = 0x3f_fff8_0000;
const SELECTED_ACE_CTRL: u64 = 0x7_8ff2;

pub(super) trait StartIo {
    fn read32(&mut self, offset: usize) -> Result<u32, &'static str>;
    fn write32(&mut self, offset: usize, value: u32) -> Result<(), &'static str>;
    fn read64(&mut self, offset: usize) -> Result<u64, &'static str>;
    fn write64(&mut self, offset: usize, value: u64) -> Result<(), &'static str>;
    fn delay_meta_cycles(&mut self) -> Result<(), &'static str>;
}

fn write32_fenced(io: &mut impl StartIo, offset: usize, value: u32) -> Result<(), &'static str> {
    io.write32(offset, value)?;
    io.read32(offset)?;
    Ok(())
}

fn write64_fenced(io: &mut impl StartIo, offset: usize, value: u64) -> Result<(), &'static str> {
    io.write64(offset, value)?;
    io.read64(offset)?;
    Ok(())
}

/// Reproduce the selected `RGXStart` prefix through AXI setup, with META
/// still held in reset. The caller must own the exclusive powered GPU lease.
pub(super) fn prepare_selected_meta(io: &mut impl StartIo) -> Result<(), &'static str> {
    // HOST_SECURITY_VERSION=1 and SYS_BUS_SECURE_RESET are selected, while
    // SUPPORT_TRUSTED_DEVICE is disabled in this RockOS configuration.
    write32_fenced(io, SYS_BUS_SECURE, 0)?;

    let mercer01 = MERCER0 | MERCER1;
    let mercer012 = mercer01 | MERCER2;
    for value in [MERCER0, mercer01, mercer012] {
        write64_fenced(io, MERCER_SOFT_RESET, value)?;
    }
    write32_fenced(io, SWIFT_SOFT_RESET, u32::MAX)?;
    write32_fenced(io, TEXAS_SOFT_RESET, u32::MAX)?;

    for value in [
        JONES_ALL,
        JONES_ALL | EXTRA,
        EXTRA | GARTEN_RESET,
        GARTEN_RESET,
    ] {
        write64_fenced(io, SOFT_RESET, value)?;
    }
    write32_fenced(io, TEXAS_SOFT_RESET, 0)?;
    write32_fenced(io, SWIFT_SOFT_RESET, 0)?;
    for value in [mercer01, MERCER0, 0] {
        write64_fenced(io, MERCER_SOFT_RESET, value)?;
    }
    if io.read64(SOFT_RESET)? != GARTEN_RESET {
        return Err("gpu_meta_reset_not_held");
    }

    // BVNC 30.3.408.101 uses META master boot and FW-private context 0.
    // The S7 wrapper's fence-PC base and META idle-control both encode zero.
    write32_fenced(io, META_BOOT, 1)?;
    write64_fenced(io, META_WRAPPER, 0)?;
    // MMUv4: selected 16 KiB non-4K heap, two empty fallback ranges and a
    // 4 KiB global range covering the firmware raw heap.
    for (index, value) in [MMU_NON4K_16K, 0, 0, MMU_GLOBAL_4K].into_iter().enumerate() {
        write64_fenced(io, MMU_RANGE_ONE + index * 8, value)?;
    }
    write64_fenced(io, ACE_CTRL, SELECTED_ACE_CTRL)?;
    if io.read64(SOFT_RESET)? != GARTEN_RESET {
        return Err("gpu_meta_reset_released_early");
    }
    Ok(())
}

/// Complete the selected META master-boot reset sequence after its catalogue
/// and firmware objects have been installed. The owner-provided flag is set
/// before the first release write, including when the write or readback fails.
pub(super) fn release_selected_meta(
    io: &mut impl StartIo,
    release_attempted: &AtomicBool,
) -> Result<(), &'static str> {
    if io.read64(SOFT_RESET)? != GARTEN_RESET {
        return Err("gpu_meta_reset_not_held");
    }
    if io.read32(META_BOOT)? != 1 {
        return Err("gpu_meta_master_boot_not_selected");
    }

    // RockOS DeassertMetaReset waits at least 32 GPU cycles on either side
    // of the write and reads SOFT_RESET to fence the release.
    io.delay_meta_cycles()?;
    release_attempted.store(true, Ordering::Release);
    io.write64(SOFT_RESET, 0)?;
    if io.read64(SOFT_RESET)? != 0 {
        return Err("gpu_meta_release_readback_mismatch");
    }
    io.delay_meta_cycles()?;
    Ok(())
}

#[cfg(ktest)]
mod tests {
    use alloc::{vec, vec::Vec};
    use core::time::Duration;

    use ostd::prelude::ktest;

    use super::*;

    struct FakeFirmwareWait {
        observations: Vec<Result<bool, &'static str>>,
        reads: usize,
        waits: usize,
        elapsed: Duration,
        step: Duration,
        wait_error: Option<&'static str>,
    }

    impl FirmwareStartIo for FakeFirmwareWait {
        fn monotonic_time(&mut self) -> Duration {
            self.elapsed
        }

        fn observe_start(&mut self) -> Result<bool, &'static str> {
            let observation = self
                .observations
                .get(self.reads)
                .copied()
                .unwrap_or(Ok(false));
            self.reads += 1;
            observation
        }

        fn wait_interval(&mut self) -> Result<(), &'static str> {
            self.waits += 1;
            if let Some(reason) = self.wait_error {
                return Err(reason);
            }
            self.elapsed += self.step;
            Ok(())
        }
    }

    #[ktest]
    fn selected_firmware_start_wait_is_bounded_and_propagates_faults() {
        let mut io = FakeFirmwareWait {
            observations: vec![Ok(false), Ok(false), Ok(true)],
            reads: 0,
            waits: 0,
            elapsed: Duration::ZERO,
            step: Duration::from_millis(1),
            wait_error: None,
        };
        assert_eq!(wait_selected_firmware(&mut io), Ok(()));
        assert_eq!((io.reads, io.waits), (3, 2));

        // A success flag first available at the deadline is not accepted.
        io.reads = 0;
        io.waits = 0;
        io.elapsed = Duration::ZERO;
        io.step = Duration::from_millis(500);
        assert_eq!(
            wait_selected_firmware(&mut io),
            Err("gpu_firmware_start_timeout")
        );
        assert_eq!((io.reads, io.waits), (2, 2));

        // Even a stalled clock must leave the wait after a finite number of reads.
        io.observations.clear();
        io.reads = 0;
        io.waits = 0;
        io.elapsed = Duration::ZERO;
        io.step = Duration::ZERO;
        assert_eq!(
            wait_selected_firmware(&mut io),
            Err("gpu_firmware_start_timeout")
        );
        assert_eq!((io.reads, io.waits), (1000, 999));

        io.observations = vec![Err("gpu_firmware_fault_observed"), Ok(true)];
        io.reads = 0;
        io.waits = 0;
        assert_eq!(
            wait_selected_firmware(&mut io),
            Err("gpu_firmware_fault_observed")
        );
        assert_eq!((io.reads, io.waits), (1, 0));

        io.observations = vec![Ok(false), Ok(true)];
        io.reads = 0;
        io.waits = 0;
        io.wait_error = Some("test_wait_failed");
        assert_eq!(wait_selected_firmware(&mut io), Err("test_wait_failed"));
        assert_eq!((io.reads, io.waits), (1, 1));

        io.reads = 0;
        io.waits = 0;
        io.elapsed = Duration::MAX;
        assert_eq!(
            wait_selected_firmware(&mut io),
            Err("gpu_firmware_start_deadline_overflow")
        );
        assert_eq!((io.reads, io.waits), (0, 0));
    }

    #[derive(Debug, Eq, PartialEq)]
    enum Operation {
        Read32(usize),
        Read64(usize),
        Write64(usize, u64),
        Delay,
    }

    #[derive(Default)]
    struct FakeIo {
        writes: Vec<(usize, u64)>,
        operations: Vec<Operation>,
        release_readback_mismatch: bool,
    }

    impl FakeIo {
        fn read(&self, offset: usize) -> u64 {
            self.writes
                .iter()
                .rev()
                .find(|(written_offset, _)| *written_offset == offset)
                .map_or(0, |(_, value)| *value)
        }
    }

    impl StartIo for FakeIo {
        fn read32(&mut self, offset: usize) -> Result<u32, &'static str> {
            self.operations.push(Operation::Read32(offset));
            Ok(self.read(offset) as u32)
        }

        fn write32(&mut self, offset: usize, value: u32) -> Result<(), &'static str> {
            self.writes.push((offset, value as u64));
            Ok(())
        }

        fn read64(&mut self, offset: usize) -> Result<u64, &'static str> {
            self.operations.push(Operation::Read64(offset));
            if self.release_readback_mismatch && offset == SOFT_RESET && self.read(offset) == 0 {
                return Ok(GARTEN_RESET);
            }
            Ok(self.read(offset))
        }

        fn write64(&mut self, offset: usize, value: u64) -> Result<(), &'static str> {
            self.writes.push((offset, value));
            self.operations.push(Operation::Write64(offset, value));
            Ok(())
        }

        fn delay_meta_cycles(&mut self) -> Result<(), &'static str> {
            self.operations.push(Operation::Delay);
            Ok(())
        }
    }

    #[ktest]
    fn selected_meta_release_is_fenced_between_cycle_waits() {
        let mut io = FakeIo::default();
        prepare_selected_meta(&mut io).unwrap();
        io.operations.clear();
        let attempted = AtomicBool::new(false);

        assert_eq!(release_selected_meta(&mut io, &attempted), Ok(()));
        assert!(attempted.load(Ordering::Acquire));
        assert_eq!(io.read(SOFT_RESET), 0);
        assert_eq!(
            io.operations,
            vec![
                Operation::Read64(SOFT_RESET),
                Operation::Read32(META_BOOT),
                Operation::Delay,
                Operation::Write64(SOFT_RESET, 0),
                Operation::Read64(SOFT_RESET),
                Operation::Delay,
            ]
        );
    }

    #[ktest]
    fn selected_meta_release_rejects_unprepared_registers() {
        let mut io = FakeIo::default();
        let attempted = AtomicBool::new(false);
        assert_eq!(
            release_selected_meta(&mut io, &attempted),
            Err("gpu_meta_reset_not_held")
        );
        assert!(io.writes.is_empty());
        assert!(!attempted.load(Ordering::Acquire));

        prepare_selected_meta(&mut io).unwrap();
        io.write32(META_BOOT, 0).unwrap();
        assert_eq!(
            release_selected_meta(&mut io, &attempted),
            Err("gpu_meta_master_boot_not_selected")
        );
        assert!(!attempted.load(Ordering::Acquire));
        assert_eq!(io.read(SOFT_RESET), GARTEN_RESET);
    }

    #[ktest]
    fn selected_meta_release_readback_failure_requires_running_cleanup() {
        let mut io = FakeIo::default();
        prepare_selected_meta(&mut io).unwrap();
        io.release_readback_mismatch = true;
        let attempted = AtomicBool::new(false);
        assert_eq!(
            release_selected_meta(&mut io, &attempted),
            Err("gpu_meta_release_readback_mismatch")
        );
        assert!(attempted.load(Ordering::Acquire));
        assert_eq!(io.writes.last(), Some(&(SOFT_RESET, 0)));
    }

    #[ktest]
    fn selected_meta_start_prefix_keeps_processor_in_reset_until_catalogue() {
        let mut io = FakeIo::default();
        prepare_selected_meta(&mut io).unwrap();
        assert_eq!(
            io.writes.as_slice(),
            &[
                (SYS_BUS_SECURE, 0),
                (MERCER_SOFT_RESET, MERCER0),
                (MERCER_SOFT_RESET, MERCER0 | MERCER1),
                (MERCER_SOFT_RESET, MERCER0 | MERCER1 | MERCER2),
                (SWIFT_SOFT_RESET, u32::MAX as u64),
                (TEXAS_SOFT_RESET, u32::MAX as u64),
                (SOFT_RESET, JONES_ALL),
                (SOFT_RESET, JONES_ALL | EXTRA),
                (SOFT_RESET, EXTRA | GARTEN_RESET),
                (SOFT_RESET, GARTEN_RESET),
                (TEXAS_SOFT_RESET, 0),
                (SWIFT_SOFT_RESET, 0),
                (MERCER_SOFT_RESET, MERCER0 | MERCER1),
                (MERCER_SOFT_RESET, MERCER0),
                (MERCER_SOFT_RESET, 0),
                (META_BOOT, 1),
                (META_WRAPPER, 0),
                (MMU_RANGE_ONE, MMU_NON4K_16K),
                (MMU_RANGE_ONE + 8, 0),
                (MMU_RANGE_ONE + 16, 0),
                (MMU_RANGE_ONE + 24, MMU_GLOBAL_4K),
                (ACE_CTRL, SELECTED_ACE_CTRL),
            ]
        );
        assert_eq!(io.read(SOFT_RESET), GARTEN_RESET);
        assert_eq!(io.read(META_BOOT), 1);
        assert_eq!(io.read(META_WRAPPER), 0);
        assert_eq!(io.read(MMU_RANGE_ONE), MMU_NON4K_16K);
        assert_eq!(io.read(MMU_RANGE_ONE + 3 * 8), MMU_GLOBAL_4K);
        assert_eq!(io.read(ACE_CTRL), SELECTED_ACE_CTRL);
        assert_eq!(
            io.writes
                .iter()
                .filter(|(offset, _)| *offset == SOFT_RESET)
                .map(|(_, value)| *value)
                .collect::<Vec<_>>(),
            [
                JONES_ALL,
                JONES_ALL | EXTRA,
                EXTRA | GARTEN_RESET,
                GARTEN_RESET,
            ]
        );
        assert!(io
            .writes
            .iter()
            .all(|(offset, value)| *offset != SOFT_RESET || *value != 0));
    }
}
