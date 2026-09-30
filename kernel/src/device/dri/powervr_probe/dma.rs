// SPDX-License-Identifier: MPL-2.0

//! Bounded DMA ownership for the selected Megrez PowerVR session.

use alloc::{vec, vec::Vec};

use ostd::mm::{
    HasDaddr, HasPaddr, HasSize, PAGE_SIZE,
    dma::DmaCoherent,
    io::{VmIo, VmIoOnce},
};

use super::{
    fwif::{ALIGN_CHECKS_KM, OS_OBJECTS, SYSINIT_HEAP_BASES, SYSTEM_OBJECTS, meta_fwif_address},
    mmu::GpuMmu4,
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
pub(super) const FW_HEAP_BASE: usize = 0xe1c0_000000;
// The pinned DDK accepts firmware heaps no smaller than 4 MiB. Keep this
// selected layout within that minimum, even if RockOS configures a larger one.
const FW_HEAP_MIN_END: usize = FW_HEAP_BASE + (1 << 22);
// RockOS bf2ec5d5 config_kernel.h and rgx_heap_firmware.h reserve the final
// three 64 KiB granules of a 32 MiB raw heap for FW connection, OS and system
// init data. These fixed VAs are part of the META firmware ABI.
pub(super) const FW_RAW_HEAP_SIZE: usize = 1 << 25;
const FW_CONFIG_SLOT_SIZE: usize = 0x10000;
const FW_CONFIG_START: usize = FW_HEAP_BASE + FW_RAW_HEAP_SIZE - 3 * FW_CONFIG_SLOT_SIZE;
const FW_SYSTEM_OBJECT_START: usize = FW_HEAP_BASE + 0x40000;
const FW_OS_OBJECT_START: usize = FW_HEAP_BASE + 0x50000;
const FW_FAULT_VADDR: usize = FW_HEAP_BASE + 0x70000;
const FW_COUNTER_VADDR: usize = FW_FAULT_VADDR + 2 * PAGE_SIZE;
const FW_ALIGN_VADDR: usize = FW_COUNTER_VADDR + 2 * PAGE_SIZE;
const FW_SYSINIT_VADDR: usize = FW_CONFIG_START + 2 * FW_CONFIG_SLOT_SIZE;
const FW_OSINIT_VADDR: usize = FW_CONFIG_START + FW_CONFIG_SLOT_SIZE;

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
    pub(super) mmu_code_root: Option<usize>,
}

#[derive(Default)]
pub(super) struct GpuFirmwareStage {
    segments: [Option<GpuDmaAllocation>; 4],
    staged_mask: u8,
    mmu: Option<GpuMmu4>,
    mmu_vaddrs: Option<[usize; 4]>,
}

impl GpuFirmwareStage {
    pub(super) fn stage_frame(&mut self, frame: &[u8]) -> Result<StagedSegment, &'static str> {
        let (segment, payload) = parse_stage_frame(frame)?;
        if self.staged_mask & (1 << segment) != 0 {
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
        let mut staged = StagedSegment {
            segment,
            bytes: payload.len(),
            pages,
            daddr: allocation.daddr(),
            mmu_code_root: None,
        };
        self.segments[segment] = Some(allocation);
        self.staged_mask |= 1 << segment;
        if self.staged_mask == 0b1111 {
            // The META bootloader requires code at the firmware heap base.
            // Data/coremem VAs are supplied separately to RGXProcessFWImage;
            // place them at owned page boundaries with unmapped guard pages.
            // LDR section destinations are META addresses, not these GPU VAs.
            let mut mmu = GpuMmu4::new()?;
            let mut next_vaddr = FW_HEAP_BASE;
            let mut vaddrs = [0; 4];
            for (index, slot) in self.segments.iter_mut().enumerate() {
                let allocation = slot.take().ok_or("gpu_dma_stage_segment_missing")?;
                let next_end = next_vaddr
                    .checked_add(allocation.size())
                    .ok_or("gpu_dma_stage_firmware_heap_overflow")?;
                if next_end > FW_HEAP_MIN_END {
                    return Err("gpu_dma_stage_firmware_heap_overflow");
                }
                vaddrs[index] = next_vaddr;
                mmu.map_owned(next_vaddr, allocation, index == 2, true)?;
                next_vaddr = next_end
                    .checked_add(PAGE_SIZE)
                    .ok_or("gpu_dma_stage_firmware_heap_overflow")?;
            }
            if next_vaddr > FW_SYSTEM_OBJECT_START {
                return Err("gpu_dma_stage_system_object_overlap");
            }
            for slot in 0..3 {
                let allocation = GpuDmaAllocation::new(FW_CONFIG_SLOT_SIZE / PAGE_SIZE)?;
                let vaddr = FW_CONFIG_START + slot * FW_CONFIG_SLOT_SIZE;
                mmu.map_owned(vaddr, allocation, false, true)?;
            }
            let mut next_object_vaddr = FW_SYSTEM_OBJECT_START;
            for object in SYSTEM_OBJECTS {
                let pages = object.bytes.div_ceil(PAGE_SIZE);
                let allocation = GpuDmaAllocation::new(pages)?;
                let end = next_object_vaddr
                    .checked_add(allocation.size())
                    .ok_or("gpu_dma_stage_firmware_heap_overflow")?;
                if end > FW_HEAP_MIN_END {
                    return Err("gpu_dma_stage_firmware_heap_overflow");
                }
                let fwaddr = meta_fwif_address(
                    next_object_vaddr,
                    object.firmware_cached,
                    object.gpu_cached,
                )?;
                mmu.map_owned(next_object_vaddr, allocation, object.gpu_read_only, true)?;
                mmu.write_mapped_u32(FW_SYSINIT_VADDR + object.sysinit_offset, fwaddr)?;
                next_object_vaddr = end
                    .checked_add(PAGE_SIZE)
                    .ok_or("gpu_dma_stage_firmware_heap_overflow")?;
            }
            if next_object_vaddr > FW_OS_OBJECT_START {
                return Err("gpu_dma_stage_os_object_overlap");
            }
            next_object_vaddr = FW_OS_OBJECT_START;
            let mut osdata_vaddr = None;
            for object in OS_OBJECTS {
                let pages = object.bytes.div_ceil(PAGE_SIZE);
                let allocation = GpuDmaAllocation::new(pages)?;
                let end = next_object_vaddr
                    .checked_add(allocation.size())
                    .ok_or("gpu_dma_stage_firmware_heap_overflow")?;
                if end > FW_HEAP_MIN_END {
                    return Err("gpu_dma_stage_firmware_heap_overflow");
                }
                let fwaddr = meta_fwif_address(next_object_vaddr, object.firmware_cached, false)?;
                mmu.map_owned(next_object_vaddr, allocation, object.gpu_read_only, true)?;
                mmu.write_mapped_u32(FW_OSINIT_VADDR + object.osinit_offset, fwaddr)?;
                if let Some(mask) = object.ccb_wrap_mask {
                    mmu.write_mapped_u32(next_object_vaddr + 8, mask)?;
                }
                if object.osinit_offset == 36 {
                    osdata_vaddr = Some(next_object_vaddr);
                }
                next_object_vaddr = end
                    .checked_add(PAGE_SIZE)
                    .ok_or("gpu_dma_stage_firmware_heap_overflow")?;
            }
            let sync_vaddr = next_object_vaddr;
            let allocation = GpuDmaAllocation::new(1)?;
            if sync_vaddr
                .checked_add(allocation.size())
                .is_none_or(|end| end > FW_HEAP_MIN_END)
            {
                return Err("gpu_dma_stage_firmware_heap_overflow");
            }
            mmu.map_owned(sync_vaddr, allocation, false, true)?;
            mmu.write_mapped_u32(
                osdata_vaddr.ok_or("gpu_dma_stage_osdata_missing")? + 572,
                meta_fwif_address(sync_vaddr, false, false)?,
            )?;
            if sync_vaddr + 2 * PAGE_SIZE > FW_FAULT_VADDR {
                return Err("gpu_dma_stage_system_extra_overlap");
            }
            let fault = GpuDmaAllocation::new(1)?;
            let mut fault_pattern = vec![0; PAGE_SIZE];
            for word in fault_pattern.chunks_exact_mut(size_of::<u32>()) {
                word.copy_from_slice(&0xdead_beefu32.to_le_bytes());
            }
            fault
                .memory
                .write_bytes(0, &fault_pattern)
                .map_err(|_| "gpu_dma_fault_page_write_failed")?;
            let mut fault_readback = vec![0; PAGE_SIZE];
            fault
                .memory
                .read_bytes(0, &mut fault_readback)
                .map_err(|_| "gpu_dma_fault_page_read_failed")?;
            if fault_readback != fault_pattern {
                return Err("gpu_dma_fault_page_readback_mismatch");
            }
            let fault_phys = fault.daddr() as u64;
            mmu.map_owned(FW_FAULT_VADDR, fault, false, true)?;
            mmu.write_mapped_u32(FW_SYSINIT_VADDR, fault_phys as u32)?;
            mmu.write_mapped_u32(FW_SYSINIT_VADDR + 4, (fault_phys >> 32) as u32)?;

            let counter = GpuDmaAllocation::new(1)?;
            mmu.map_owned(FW_COUNTER_VADDR, counter, false, true)?;
            mmu.write_mapped_u32(
                FW_SYSINIT_VADDR + 184,
                meta_fwif_address(FW_COUNTER_VADDR, true, false)?,
            )?;
            mmu.write_mapped_u32(FW_SYSINIT_VADDR + 188, (PAGE_SIZE / 4) as u32)?;

            let align = GpuDmaAllocation::new(1)?;
            let mut align_data = vec![0; 4 + ALIGN_CHECKS_KM.len() + 4];
            align_data[..4].copy_from_slice(&((ALIGN_CHECKS_KM.len() / 4) as u32).to_le_bytes());
            align_data[4..4 + ALIGN_CHECKS_KM.len()].copy_from_slice(ALIGN_CHECKS_KM);
            align
                .memory
                .write_bytes(0, &align_data)
                .map_err(|_| "gpu_dma_align_checks_write_failed")?;
            let mut align_readback = vec![0; align_data.len()];
            align
                .memory
                .read_bytes(0, &mut align_readback)
                .map_err(|_| "gpu_dma_align_checks_read_failed")?;
            if align_readback != align_data {
                return Err("gpu_dma_align_checks_readback_mismatch");
            }
            mmu.map_owned(FW_ALIGN_VADDR, align, false, true)?;
            mmu.write_mapped_u32(
                FW_SYSINIT_VADDR + 192,
                meta_fwif_address(FW_ALIGN_VADDR, false, false)?,
            )?;
            for (offset, base) in SYSINIT_HEAP_BASES {
                mmu.write_mapped_u64(FW_SYSINIT_VADDR + offset, base)?;
            }
            // The selected driver starts with a cleared ready flag and marker 1.
            mmu.write_mapped_u32(FW_SYSINIT_VADDR + 208, 0)?;
            mmu.write_mapped_u32(FW_SYSINIT_VADDR + 212, 1)?;
            staged.mmu_code_root = Some(mmu.root_daddr());
            self.mmu_vaddrs = Some(vaddrs);
            self.mmu = Some(mmu);
        }
        Ok(staged)
    }

    pub(super) fn mapped_firmware_vaddrs(&self) -> Option<[usize; 4]> {
        self.mmu_vaddrs
    }

    pub(super) fn mapped_fw_config_vaddrs(&self) -> Option<[usize; 3]> {
        self.mmu.as_ref().map(|_| {
            [
                FW_CONFIG_START,
                FW_CONFIG_START + FW_CONFIG_SLOT_SIZE,
                FW_CONFIG_START + 2 * FW_CONFIG_SLOT_SIZE,
            ]
        })
    }

    pub(super) fn mapped_fw_config_fwaddrs(&self) -> Result<[u32; 3], &'static str> {
        let [connection, osinit, sysinit] = self
            .mapped_fw_config_vaddrs()
            .ok_or("gpu_dma_stage_firmware_layout_missing")?;
        Ok([
            meta_fwif_address(connection, false, false)?,
            meta_fwif_address(osinit, true, false)?,
            meta_fwif_address(sysinit, true, false)?,
        ])
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

    pub(super) fn size(&self) -> usize {
        self.memory.size()
    }

    pub(super) fn write_u32(&self, offset: usize, value: u32) -> Result<(), &'static str> {
        self.memory
            .write_once(offset, &value)
            .map_err(|_| "gpu_dma_table_write_failed")
    }

    pub(super) fn read_u32(&self, offset: usize) -> Result<u32, &'static str> {
        self.memory
            .read_once(offset)
            .map_err(|_| "gpu_dma_table_read_failed")
    }

    pub(super) fn write_u64(&self, offset: usize, value: u64) -> Result<(), &'static str> {
        self.memory
            .write_once(offset, &value)
            .map_err(|_| "gpu_dma_table_write_failed")
    }

    pub(super) fn read_u64(&self, offset: usize) -> Result<u64, &'static str> {
        self.memory
            .read_once(offset)
            .map_err(|_| "gpu_dma_table_read_failed")
    }

    pub(super) fn uncached_alias_paddr(&self) -> Option<usize> {
        self.memory.uncached_alias_paddr()
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::{
        GpuFirmwareStage, GpuMmu4, PAGE_SIZE, STAGE_SEGMENT_SIZES, parse_stage_frame,
        validate_dma_range,
    };

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

    #[ktest]
    fn gpu_dma_stage_maps_four_owned_firmware_segments_with_guard_pages() {
        let mut stage = GpuFirmwareStage::default();
        let mut root = None;
        let mut daddrs = [0; 4];
        for (segment, size) in [52_064, 18_432, 73_312, 9_984].into_iter().enumerate() {
            let mut frame = b"PVR1".to_vec();
            frame.extend_from_slice(&(segment as u32).to_le_bytes());
            frame.extend_from_slice(&(size as u32).to_le_bytes());
            frame.extend(core::iter::repeat_n(0x5a, size));
            let result = stage.stage_frame(&frame).unwrap();
            daddrs[segment] = result.daddr;
            if segment < 3 {
                assert_eq!(result.mmu_code_root, None);
            } else {
                root = result.mmu_code_root;
            }
        }
        assert!(root.is_some());
        assert_eq!(stage.mmu.as_ref().map(GpuMmu4::root_daddr), root);
        assert_eq!(
            stage.mapped_firmware_vaddrs(),
            Some([0xe1c0_000000, 0xe1c0_00e000, 0xe1c0_014000, 0xe1c0_027000,])
        );
        let mmu = stage.mmu.as_ref().unwrap();
        let vaddrs = stage.mapped_firmware_vaddrs().unwrap();
        for (index, vaddr) in vaddrs.into_iter().enumerate() {
            let first_pte = mmu.test_pte(vaddr).unwrap();
            assert_eq!(first_pte & 0xff_ffff_f000, daddrs[index] as u64);
            assert_eq!(first_pte & 1, 1);
            assert_eq!((first_pte >> 1) & 1, u64::from(index == 2));
            let pages = STAGE_SEGMENT_SIZES[index].div_ceil(PAGE_SIZE);
            let last_pte = mmu.test_pte(vaddr + (pages - 1) * PAGE_SIZE).unwrap();
            assert_eq!(
                last_pte & 0xff_ffff_f000,
                (daddrs[index] + (pages - 1) * PAGE_SIZE) as u64
            );
            assert_eq!(mmu.test_pte(vaddr + pages * PAGE_SIZE), Ok(0));
        }
        let mut duplicate = b"PVR1".to_vec();
        duplicate.extend_from_slice(&0u32.to_le_bytes());
        duplicate.extend_from_slice(&52_064u32.to_le_bytes());
        duplicate.extend(core::iter::repeat_n(0x5a, 52_064));
        assert_eq!(
            stage.stage_frame(&duplicate),
            Err("gpu_dma_stage_duplicate_segment")
        );
    }

    #[ktest]
    fn gpu_dma_stage_maps_vendor_firmware_config_heap() {
        let mut stage = GpuFirmwareStage::default();
        assert_eq!(
            stage.mapped_fw_config_fwaddrs(),
            Err("gpu_dma_stage_firmware_layout_missing")
        );
        for (segment, size) in STAGE_SEGMENT_SIZES.into_iter().enumerate() {
            let mut frame = b"PVR1".to_vec();
            frame.extend_from_slice(&(segment as u32).to_le_bytes());
            frame.extend_from_slice(&(size as u32).to_le_bytes());
            frame.extend(core::iter::repeat_n(0x5a, size));
            stage.stage_frame(&frame).unwrap();
        }

        let mmu = stage.mmu.as_ref().unwrap();
        // RockOS bf2ec5d5: the final 3 x 64 KiB of the 32 MiB raw FW heap
        // hold connection control, OSINIT, and SYSINIT in that order.
        let config_start = 0xe1c1_fd0000;
        assert_eq!(
            stage.mapped_fw_config_vaddrs(),
            Some([config_start, config_start + 0x10000, config_start + 0x20000])
        );
        assert_eq!(
            stage.mapped_fw_config_fwaddrs(),
            Ok([0xf1fd_0000, 0x71fe_0000, 0x71ff_0000])
        );
        for slot in 0..3 {
            let start = config_start + slot * 0x10000;
            let first = mmu.test_pte(start).unwrap();
            let last = mmu.test_pte(start + 0xf000).unwrap();
            assert_eq!(first & 0x7c00_0000_0000_0003, 0x7c00_0000_0000_0001);
            assert_eq!(last & 0x7c00_0000_0000_0003, 0x7c00_0000_0000_0001);
            assert_eq!(last & 0xff_ffff_f000, (first & 0xff_ffff_f000) + 0xf000);
        }
        assert_eq!(mmu.test_pte(config_start - PAGE_SIZE), Ok(0));
        assert_eq!(mmu.test_pte(config_start + 0x30000), Ok(0));
    }

    #[ktest]
    fn gpu_dma_stage_links_owned_sysinit_buffers_without_installing_root() {
        let mut stage = GpuFirmwareStage::default();
        for (segment, size) in STAGE_SEGMENT_SIZES.into_iter().enumerate() {
            let mut frame = b"PVR1".to_vec();
            frame.extend_from_slice(&(segment as u32).to_le_bytes());
            frame.extend_from_slice(&(size as u32).to_le_bytes());
            frame.extend(core::iter::repeat_n(0x5a, size));
            stage.stage_frame(&frame).unwrap();
        }

        let mmu = stage.mmu.as_ref().unwrap();
        let sysinit = super::FW_CONFIG_START + 2 * super::FW_CONFIG_SLOT_SIZE;
        // Offsets and object sizes come from the pinned RockOS FWIF ABI probe.
        for (offset, expected, vaddr, pages, gpu_read_only) in [
            (160, 0xf004_8000, 0xe1c0_048000, 1, true),
            (164, 0xf004_0000, 0xe1c0_040000, 1, false),
            (168, 0xf004_2000, 0xe1c0_042000, 1, false),
            (172, 0x7004_4000, 0xe1c0_044000, 3, false),
        ] {
            assert_eq!(mmu.read_mapped_u32(sysinit + offset), Ok(expected));
            let first_pte = mmu.test_pte(vaddr).unwrap();
            assert_eq!(first_pte & 1, 1);
            assert_eq!((first_pte >> 1) & 1, u64::from(gpu_read_only));
            assert_eq!(mmu.test_pte(vaddr + pages * PAGE_SIZE), Ok(0));
        }
        assert_eq!(mmu.read_mapped_u32(sysinit + 208), Ok(0));
        assert_eq!(mmu.read_mapped_u32(sysinit + 212), Ok(1));
    }

    #[ktest]
    fn gpu_dma_stage_links_owned_osinit_ccbs_and_power_sync() {
        let mut stage = GpuFirmwareStage::default();
        for (segment, size) in STAGE_SEGMENT_SIZES.into_iter().enumerate() {
            let mut frame = b"PVR1".to_vec();
            frame.extend_from_slice(&(segment as u32).to_le_bytes());
            frame.extend_from_slice(&(size as u32).to_le_bytes());
            frame.extend(core::iter::repeat_n(0x5a, size));
            stage.stage_frame(&frame).unwrap();
        }

        let mmu = stage.mmu.as_ref().unwrap();
        let osinit = super::FW_CONFIG_START + super::FW_CONFIG_SLOT_SIZE;
        for (offset, expected, vaddr, pages, gpu_read_only) in [
            (28, 0xf005_0000, 0xe1c0_050000, 1, false),
            (0, 0xf005_2000, 0xe1c0_052000, 1, false),
            (4, 0x7005_4000, 0xe1c0_054000, 16, true),
            (8, 0xf006_5000, 0xe1c0_065000, 1, false),
            (12, 0xf006_7000, 0xe1c0_067000, 1, false),
            (16, 0xf006_9000, 0xe1c0_069000, 1, false),
            (36, 0xf006_b000, 0xe1c0_06b000, 1, false),
        ] {
            assert_eq!(mmu.read_mapped_u32(osinit + offset), Ok(expected));
            let pte = mmu.test_pte(vaddr).unwrap();
            assert_eq!(pte & 1, 1);
            assert_eq!((pte >> 1) & 1, u64::from(gpu_read_only));
            assert_eq!(
                mmu.test_pte(vaddr + (pages - 1) * PAGE_SIZE).unwrap() & 1,
                1
            );
            assert_eq!(mmu.test_pte(vaddr + pages * PAGE_SIZE), Ok(0));
        }
        assert_eq!(mmu.read_mapped_u32(0xe1c0_052000 + 8), Ok(1023));
        assert_eq!(mmu.read_mapped_u32(0xe1c0_067000 + 8), Ok(31));
        assert_eq!(mmu.read_mapped_u32(0xe1c0_06b000 + 572), Ok(0xf006_d000));
        assert_eq!(mmu.read_mapped_u32(0xe1c0_06d000), Ok(0));
        assert_eq!(mmu.test_pte(0xe1c0_06e000), Ok(0));
    }

    #[ktest]
    fn gpu_dma_stage_prepares_sysinit_fault_counter_and_alignment_checks() {
        let mut stage = GpuFirmwareStage::default();
        for (segment, size) in STAGE_SEGMENT_SIZES.into_iter().enumerate() {
            let mut frame = b"PVR1".to_vec();
            frame.extend_from_slice(&(segment as u32).to_le_bytes());
            frame.extend_from_slice(&(size as u32).to_le_bytes());
            frame.extend(core::iter::repeat_n(0x5a, size));
            stage.stage_frame(&frame).unwrap();
        }

        let mmu = stage.mmu.as_ref().unwrap();
        let sysinit = super::FW_SYSINIT_VADDR;
        let fault = 0xe1c0_070000;
        let fault_phys = mmu.test_pte(fault).unwrap() & 0xff_ffff_f000;
        let encoded_phys = mmu.read_mapped_u32(sysinit).unwrap() as u64
            | ((mmu.read_mapped_u32(sysinit + 4).unwrap() as u64) << 32);
        assert_eq!(encoded_phys, fault_phys);
        assert_eq!(mmu.read_mapped_u32(fault), Ok(0xdead_beef));
        assert_eq!(mmu.read_mapped_u32(fault + PAGE_SIZE - 4), Ok(0xdead_beef));
        assert_eq!(mmu.test_pte(fault + PAGE_SIZE), Ok(0));

        let counter = 0xe1c0_072000;
        assert_eq!(mmu.read_mapped_u32(sysinit + 184), Ok(0x7007_2000));
        assert_eq!(mmu.read_mapped_u32(sysinit + 188), Ok(1024));
        assert_eq!(mmu.read_mapped_u32(counter), Ok(0));
        assert_eq!(mmu.test_pte(counter + PAGE_SIZE), Ok(0));

        let align = 0xe1c0_074000;
        assert_eq!(mmu.read_mapped_u32(sysinit + 192), Ok(0xf007_4000));
        assert_eq!(mmu.read_mapped_u32(align), Ok(32));
        assert_eq!(mmu.read_mapped_u32(align + 4), Ok(440));
        assert_eq!(mmu.read_mapped_u32(align + 4 + 31 * 4), Ok(8));
        assert_eq!(mmu.read_mapped_u32(align + 4 + 32 * 4), Ok(0));
        assert_eq!(mmu.test_pte(align + PAGE_SIZE), Ok(0));
    }

    #[ktest]
    fn gpu_dma_stage_initializes_vendor_sysinit_heap_bases() {
        let mut stage = GpuFirmwareStage::default();
        for (segment, size) in STAGE_SEGMENT_SIZES.into_iter().enumerate() {
            let mut frame = b"PVR1".to_vec();
            frame.extend_from_slice(&(segment as u32).to_le_bytes());
            frame.extend_from_slice(&(size as u32).to_le_bytes());
            frame.extend(core::iter::repeat_n(0x5a, size));
            stage.stage_frame(&frame).unwrap();
        }

        let mmu = stage.mmu.as_ref().unwrap();
        let sysinit = super::FW_SYSINIT_VADDR;
        // These are the selected Volcanic rgxheapconfig.h values, compiled by
        // both RISC-V and native FWIF ABI probes for the pinned RockOS tree.
        for (offset, expected) in [
            (8, 0xda00_000000u64),
            (16, 0xe000_000000),
            (24, 0xec00_000000),
            (32, 0xec40_000000),
            (40, 0xf000_000000),
            (48, 0xed00_000000),
        ] {
            let low = mmu.read_mapped_u32(sysinit + offset).unwrap() as u64;
            let high = mmu.read_mapped_u32(sysinit + offset + 4).unwrap() as u64;
            assert_eq!((high << 32) | low, expected);
        }
    }
}
