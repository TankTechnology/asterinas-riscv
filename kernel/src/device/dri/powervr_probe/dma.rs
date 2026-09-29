// SPDX-License-Identifier: MPL-2.0

//! Bounded DMA ownership for the selected Megrez PowerVR session.

use alloc::{vec, vec::Vec};

use ostd::mm::{
    HasDaddr, HasPaddr, HasSize, PAGE_SIZE,
    dma::DmaCoherent,
    io::{VmIo, VmIoOnce},
};

// RockOS bf2ec5d5 eswin_cpu/sysconfig.c uses an identity UMA physical heap
// and a 40-bit DMA mask. Restrict initial allocations to Die 0 DRAM, where
// Asterinas can provide a guaranteed uncached CPU access path.
const DIE0_DRAM_START: usize = 0x8000_0000;
const DIE0_DRAM_END: usize = 0x4_8000_0000;
const GPU_DMA_LIMIT: usize = 1 << 40;
const MAX_PROBE_PAGES: usize = 256;
const STAGE_HEADER_SIZE: usize = 12;
const STAGE_SEGMENT_SIZES: [usize; 4] = [52_064, 18_432, 73_312, 9_984];

pub(super) fn parse_stage_frame(frame: &[u8]) -> Result<(usize, &[u8]), &'static str> {
    if frame.len() < STAGE_HEADER_SIZE || &frame[..4] != b"PVR1" {
        return Err("gpu_dma_stage_invalid_header");
    }
    let segment = u32::from_le_bytes(
        frame[4..8]
            .try_into()
            .map_err(|_| "gpu_dma_stage_invalid_header")?,
    ) as usize;
    let length = u32::from_le_bytes(
        frame[8..12]
            .try_into()
            .map_err(|_| "gpu_dma_stage_invalid_header")?,
    ) as usize;
    if STAGE_SEGMENT_SIZES.get(segment) != Some(&length)
        || frame.len() != STAGE_HEADER_SIZE + length
    {
        return Err("gpu_dma_stage_invalid_segment");
    }
    Ok((segment, &frame[STAGE_HEADER_SIZE..]))
}

#[derive(Debug, Eq, PartialEq)]
pub(super) struct StagedSegment {
    pub(super) segment: usize,
    pub(super) bytes: usize,
    pub(super) pages: usize,
    pub(super) daddr: usize,
}

#[derive(Default)]
pub(super) struct GpuFirmwareStage {
    segments: [Option<GpuDmaAllocation>; 4],
}

impl GpuFirmwareStage {
    pub(super) fn stage_frame(&mut self, frame: &[u8]) -> Result<StagedSegment, &'static str> {
        let (segment, payload) = parse_stage_frame(frame)?;
        if self.segments[segment].is_some() {
            return Err("gpu_dma_stage_duplicate_segment");
        }
        let pages = payload.len().div_ceil(PAGE_SIZE);
        let allocation = GpuDmaAllocation::new(pages)?;
        allocation
            .memory
            .write_bytes(0, payload)
            .map_err(|_| "gpu_dma_stage_cpu_write_failed")?;
        let mut readback: Vec<u8> = vec![0; payload.len()];
        allocation
            .memory
            .read_bytes(0, &mut readback)
            .map_err(|_| "gpu_dma_stage_cpu_read_failed")?;
        if readback != payload {
            return Err("gpu_dma_stage_cpu_readback_mismatch");
        }
        let staged = StagedSegment {
            segment,
            bytes: payload.len(),
            pages,
            daddr: allocation.daddr(),
        };
        self.segments[segment] = Some(allocation);
        Ok(staged)
    }
}

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

    use super::{GpuFirmwareStage, parse_stage_frame, validate_dma_range};

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

    #[ktest]
    fn gpu_dma_stage_accepts_one_exact_segment_frame() {
        let mut frame = b"PVR1".to_vec();
        frame.extend_from_slice(&0u32.to_le_bytes());
        frame.extend_from_slice(&52_064u32.to_le_bytes());
        frame.extend(core::iter::repeat_n(0xa5, 52_064));

        let (segment, payload) = parse_stage_frame(&frame).unwrap();
        assert_eq!(segment, 0);
        assert_eq!(payload.len(), 52_064);
        assert!(payload.iter().all(|byte| *byte == 0xa5));
    }

    #[ktest]
    fn gpu_dma_stage_rejects_wrong_identity_and_truncated_or_trailing_data() {
        let mut frame = b"PVR1".to_vec();
        frame.extend_from_slice(&1u32.to_le_bytes());
        frame.extend_from_slice(&18_432u32.to_le_bytes());
        frame.extend(core::iter::repeat_n(0x5a, 18_432));

        assert_eq!(parse_stage_frame(&frame).unwrap().0, 1);
        frame[0] = b'X';
        assert!(parse_stage_frame(&frame).is_err());
        frame[0] = b'P';
        frame.pop();
        assert!(parse_stage_frame(&frame).is_err());
        frame.push(0);
        frame.push(0);
        assert!(parse_stage_frame(&frame).is_err());
        frame.pop();
        frame[4..8].copy_from_slice(&4u32.to_le_bytes());
        assert!(parse_stage_frame(&frame).is_err());
    }

    #[ktest]
    fn gpu_dma_stage_keeps_one_owned_cpu_verified_copy_per_segment() {
        let mut frame = b"PVR1".to_vec();
        frame.extend_from_slice(&3u32.to_le_bytes());
        frame.extend_from_slice(&9_984u32.to_le_bytes());
        frame.extend(core::iter::repeat_n(0x5a, 9_984));

        let mut stage = GpuFirmwareStage::default();
        let staged = stage.stage_frame(&frame).unwrap();
        assert_eq!(staged.segment, 3);
        assert_eq!(staged.bytes, 9_984);
        assert_eq!(staged.pages, 3);
        assert_eq!(
            stage.stage_frame(&frame),
            Err("gpu_dma_stage_duplicate_segment")
        );
    }
}
