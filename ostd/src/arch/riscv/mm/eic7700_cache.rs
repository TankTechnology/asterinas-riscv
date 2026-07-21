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

static L3_FLUSH_REGISTER: Once<SpinLock<IoMem<Sensitive>>> = Once::new();

pub(super) fn init(io_mem_builder: &IoMemAllocatorBuilder) {
    let is_eic7700 = DEVICE_TREE
        .get()
        .unwrap()
        .root()
        .compatible()
        .all()
        .any(|compatible| compatible == "eswin,eic7700");
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
        // SAFETY: A memory and I/O fence has no memory-safety preconditions.
        unsafe { core::arch::asm!("fence iorw, iorw", options(nostack)) };
    }

    Ok(())
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
    use super::{DIE0_DRAM_END, DIE0_DRAM_START, checked_cache_line_range};
    use crate::prelude::ktest;

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
        assert!(checked_cache_line_range(DIE0_DRAM_START + 64..DIE0_DRAM_START).is_none());
        assert!(checked_cache_line_range(usize::MAX - 31..usize::MAX).is_none());
    }
}
