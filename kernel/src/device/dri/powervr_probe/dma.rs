// SPDX-License-Identifier: MPL-2.0

//! Bounded DMA ownership for the selected Megrez PowerVR session.

use ostd::mm::{HasDaddr, HasPaddr, HasSize, PAGE_SIZE, dma::DmaCoherent, io::VmIoOnce};

// RockOS bf2ec5d5 eswin_cpu/sysconfig.c uses an identity UMA physical heap
// and a 40-bit DMA mask. Restrict initial allocations to Die 0 DRAM, where
// Asterinas can provide a guaranteed uncached CPU access path.
const DIE0_DRAM_START: usize = 0x8000_0000;
const DIE0_DRAM_END: usize = 0x4_8000_0000;
const GPU_DMA_LIMIT: usize = 1 << 40;
const MAX_PROBE_PAGES: usize = 256;

fn validate_dma_range(paddr: usize, daddr: usize, size: usize) -> Result<(), &'static str> {
    if size == 0 || !size.is_multiple_of(PAGE_SIZE) {
        return Err("gpu_dma_invalid_size");
    }
    let physical_end = paddr.checked_add(size).ok_or("gpu_dma_outside_die0_dram")?;
    if paddr < DIE0_DRAM_START || physical_end > DIE0_DRAM_END {
        return Err("gpu_dma_outside_die0_dram");
    }
    if daddr != paddr
        || daddr
            .checked_add(size)
            .is_none_or(|end| end > GPU_DMA_LIMIT)
    {
        return Err("gpu_dma_address_translation_unverified");
    }
    Ok(())
}

pub(super) struct GpuDmaAllocation {
    memory: DmaCoherent,
}

impl GpuDmaAllocation {
    pub(super) fn new(pages: usize) -> Result<Self, &'static str> {
        if pages == 0 || pages > MAX_PROBE_PAGES {
            return Err("gpu_dma_page_limit");
        }
        let memory = DmaCoherent::alloc_in(pages, false, DIE0_DRAM_START..DIE0_DRAM_END)
            .map_err(|_| "gpu_dma_allocation_failed")?
            .into_uncached()
            .map_err(|_| "gpu_dma_uncached_unavailable")?;
        validate_dma_range(memory.paddr(), memory.daddr(), memory.size())?;
        Ok(Self { memory })
    }

    pub(super) fn cpu_probe(&self) -> Result<(), &'static str> {
        if self
            .memory
            .read_once::<u64>(0)
            .map_err(|_| "gpu_dma_cpu_read_failed")?
            != 0
        {
            return Err("gpu_dma_not_zeroed");
        }
        const PATTERN: u64 = 0x5341_5245_5453_494e;
        self.memory
            .write_once(0, &PATTERN)
            .map_err(|_| "gpu_dma_cpu_write_failed")?;
        if self
            .memory
            .read_once::<u64>(0)
            .map_err(|_| "gpu_dma_cpu_read_failed")?
            != PATTERN
        {
            return Err("gpu_dma_cpu_readback_mismatch");
        }
        Ok(())
    }

    pub(super) fn paddr(&self) -> usize {
        self.memory.paddr()
    }

    pub(super) fn daddr(&self) -> usize {
        self.memory.daddr()
    }

    pub(super) fn uncached_alias_paddr(&self) -> Option<usize> {
        self.memory.uncached_alias_paddr()
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::validate_dma_range;

    #[ktest]
    fn gpu_dma_accepts_only_identity_mapped_die0_pages_below_40_bits() {
        assert_eq!(validate_dma_range(0x9000_0000, 0x9000_0000, 4096), Ok(()));
        assert_eq!(
            validate_dma_range(0x9000_0000, 0x8000_0000, 4096),
            Err("gpu_dma_address_translation_unverified")
        );
        assert_eq!(
            validate_dma_range(0x4_8000_0000, 0x4_8000_0000, 4096),
            Err("gpu_dma_outside_die0_dram")
        );
        assert_eq!(
            validate_dma_range(0x8000_0000, 0x8000_0000, 0),
            Err("gpu_dma_invalid_size")
        );
        assert_eq!(
            validate_dma_range(0x8000_0000, 0x8000_0000, 4095),
            Err("gpu_dma_invalid_size")
        );
        assert_eq!(
            validate_dma_range(usize::MAX - 4095, usize::MAX - 4095, 4096),
            Err("gpu_dma_outside_die0_dram")
        );
    }
}
