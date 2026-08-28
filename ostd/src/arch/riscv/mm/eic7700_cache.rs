// SPDX-License-Identifier: MPL-2.0

use core::ops::Range;

use spin::Once;

use crate::{
    Error, Result,
    arch::boot::DEVICE_TREE,
    io::{IoMem, IoMemAllocatorBuilder, Sensitive},
    mm::{CachePolicy, Paddr},
    sync::SpinLock,
};

const CACHE_LINE_SIZE: usize = 64;

// These addresses and the cache-line command contract come from ESWIN's U-Boot
// commit a69ce946b3787e7f24e876799cb3bd0c233d8a2d, file
// arch/riscv/cpu/eic770x/cache.c. This is an independent Rust implementation of
// that hardware interface; no source implementation is copied here.
const DIE0_DRAM_START: usize = 0x8000_0000;
const DIE0_DRAM_END: usize = 0x4_8000_0000;
const DIE0_L3_FLUSH_REGISTER: usize = 0x0201_0200;
const DIE0_UNCACHED_ALIAS_START: usize = 0xc0_0000_0000;
const DIE0_UNCACHED_ALIAS_END: usize = 0xc4_0000_0000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum UncachedDmaPath {
    PageBased,
    PlatformAlias,
}

pub(super) const fn select_uncached_dma_path(
    has_svpbmt: bool,
    has_platform_alias: bool,
) -> Option<UncachedDmaPath> {
    if has_svpbmt {
        Some(UncachedDmaPath::PageBased)
    } else if has_platform_alias {
        Some(UncachedDmaPath::PlatformAlias)
    } else {
        None
    }
}

static L3_FLUSH_REGISTER: Once<SpinLock<IoMem<Sensitive>>> = Once::new();

/// Returns whether one device-tree `compatible` string identifies an ESWIN
/// EIC7700 SoC.
///
/// This is the predicate behind the sole gate that decides whether the
/// EIC7700-specific L3 cache flush register is reserved. It is kept as a pure
/// function so that the isolation behavior (non-EIC7700 boards must not touch
/// the register) can be unit-tested without a device tree.
fn is_eic7700_compatible(compatible: &str) -> bool {
    compatible == "eswin,eic7700"
}

pub(super) fn init(io_mem_builder: &IoMemAllocatorBuilder) {
    let is_eic7700 = DEVICE_TREE
        .get()
        .unwrap()
        .root()
        .compatible()
        .all()
        .any(is_eic7700_compatible);
    if !is_eic7700 {
        return;
    }

    let flush_register = io_mem_builder.reserve(
        DIE0_L3_FLUSH_REGISTER..DIE0_L3_FLUSH_REGISTER + size_of::<u64>(),
        CachePolicy::Uncacheable,
    );
    L3_FLUSH_REGISTER.call_once(|| SpinLock::new(flush_register));
    crate::info!("EIC7700 L3 cache flush registered");
}

pub(super) fn sync_to_device(range: Range<Paddr>) -> Result<()> {
    let flush_register = L3_FLUSH_REGISTER.get().ok_or(Error::AccessDenied)?.lock();
    let lines = checked_cache_line_range(range).ok_or(Error::InvalidArgs)?;

    for line in lines.step_by(CACHE_LINE_SIZE) {
        // SAFETY: Initialization reserves the documented 64-bit EIC7700 L3 flush
        // register. Each submitted value is a checked, aligned Die 0 DRAM address.
        unsafe { flush_register.write_once(0, &(line as u64)) };

        // The controller consumes one address at a time. Order every submission
        // before reusing its command register for the next cache line.
        crate::arch::device::io_mem::fence();
    }

    Ok(())
}

pub(super) fn has_uncached_dram_alias() -> bool {
    L3_FLUSH_REGISTER.get().is_some()
}

pub(super) fn uncached_alias_range(range: Range<Paddr>) -> Option<Range<Paddr>> {
    if range.start >= range.end || range.start < DIE0_DRAM_START || range.end > DIE0_DRAM_END {
        return None;
    }
    let start = DIE0_UNCACHED_ALIAS_START.checked_add(range.start - DIE0_DRAM_START)?;
    let end = DIE0_UNCACHED_ALIAS_START.checked_add(range.end - DIE0_DRAM_START)?;
    (end <= DIE0_UNCACHED_ALIAS_END).then_some(start..end)
}

fn checked_cache_line_range(range: Range<Paddr>) -> Option<Range<Paddr>> {
    if range.start >= range.end || range.start < DIE0_DRAM_START || range.end > DIE0_DRAM_END {
        return None;
    }

    let start = range.start & !(CACHE_LINE_SIZE - 1);
    let end = range.end.checked_add(CACHE_LINE_SIZE - 1)? & !(CACHE_LINE_SIZE - 1);
    (end <= DIE0_DRAM_END).then_some(start..end)
}

#[cfg(ktest)]
mod tests {
    use super::{
        DIE0_DRAM_END, DIE0_DRAM_START, UncachedDmaPath, checked_cache_line_range,
        is_eic7700_compatible, select_uncached_dma_path, uncached_alias_range,
    };
    use crate::prelude::ktest;

    #[ktest]
    fn detects_eic7700_from_root_compatible() {
        assert!(is_eic7700_compatible("eswin,eic7700"));
    }

    #[ktest]
    fn rejects_non_eic7700_platforms() {
        // QEMU virt and SiFive board root compatibles must not trigger the
        // EIC7700 L3 flush register reservation.
        assert!(!is_eic7700_compatible("riscv-virtio"));
        assert!(!is_eic7700_compatible("sifive,hifive-unleashed-a00"));
        assert!(!is_eic7700_compatible("eswin,eic7700-prefix"));
        assert!(!is_eic7700_compatible("eswin,eic7701"));
    }

    #[ktest]
    fn aligns_flush_range_outward_to_64_bytes() {
        assert_eq!(
            checked_cache_line_range(DIE0_DRAM_START + 1..DIE0_DRAM_START + 65),
            Some(DIE0_DRAM_START..DIE0_DRAM_START + 128)
        );
    }

    #[ktest]
    fn accepts_die0_framebuffer_range() {
        assert_eq!(
            checked_cache_line_range(0xfd80_0000..0xfe00_0000),
            Some(0xfd80_0000..0xfe00_0000)
        );
    }

    #[ktest]
    fn rejects_ranges_outside_die0_dram() {
        assert!(checked_cache_line_range(0x0201_0200..0x0201_0240).is_none());
        assert!(checked_cache_line_range(DIE0_DRAM_END - 32..DIE0_DRAM_END + 1).is_none());
    }

    #[ktest]
    fn rejects_empty_reversed_and_overflowing_ranges() {
        assert!(checked_cache_line_range(DIE0_DRAM_START..DIE0_DRAM_START).is_none());
        let reversed = core::ops::Range {
            start: DIE0_DRAM_START + 64,
            end: DIE0_DRAM_START,
        };
        assert!(checked_cache_line_range(reversed).is_none());
        assert!(checked_cache_line_range(usize::MAX - 31..usize::MAX).is_none());
    }

    #[ktest]
    fn selects_one_real_uncached_dma_path_or_fails_closed() {
        assert_eq!(
            select_uncached_dma_path(true, false),
            Some(UncachedDmaPath::PageBased)
        );
        assert_eq!(
            select_uncached_dma_path(false, true),
            Some(UncachedDmaPath::PlatformAlias)
        );
        assert_eq!(select_uncached_dma_path(false, false), None);
    }

    #[ktest]
    fn maps_die0_dram_into_the_hardware_uncached_alias() {
        assert_eq!(
            uncached_alias_range(0x2_a082_a000..0x2_a082_b000),
            Some(0xc2_2082_a000..0xc2_2082_b000)
        );
        assert_eq!(
            uncached_alias_range(DIE0_DRAM_START..DIE0_DRAM_START + 0x1000),
            Some(0xc0_0000_0000..0xc0_0000_1000)
        );
        assert_eq!(
            uncached_alias_range(DIE0_DRAM_END - 0x1000..DIE0_DRAM_END),
            Some(0xc3_ffff_f000..0xc4_0000_0000)
        );
    }

    #[ktest]
    fn rejects_non_dram_and_empty_uncached_alias_ranges() {
        assert!(uncached_alias_range(0x1000..0x2000).is_none());
        assert!(uncached_alias_range(DIE0_DRAM_START..DIE0_DRAM_START).is_none());
        assert!(uncached_alias_range(DIE0_DRAM_END..DIE0_DRAM_END + 0x1000).is_none());
    }
}
