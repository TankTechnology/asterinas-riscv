// SPDX-License-Identifier: MPL-2.0

//! Selected Volcanic META reset and bus preparation with the processor held.

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

#[cfg(ktest)]
mod tests {
    use alloc::vec::Vec;

    use ostd::prelude::ktest;

    use super::*;

    #[derive(Default)]
    struct FakeIo {
        writes: Vec<(usize, u64)>,
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
            Ok(self.read(offset) as u32)
        }

        fn write32(&mut self, offset: usize, value: u32) -> Result<(), &'static str> {
            self.writes.push((offset, value as u64));
            Ok(())
        }

        fn read64(&mut self, offset: usize) -> Result<u64, &'static str> {
            Ok(self.read(offset))
        }

        fn write64(&mut self, offset: usize, value: u64) -> Result<(), &'static str> {
            self.writes.push((offset, value));
            Ok(())
        }
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
