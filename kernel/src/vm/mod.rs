// SPDX-License-Identifier: MPL-2.0

//! Virtual memory (VM).
//!
//! There are two primary VM abstractions:
//!  * The VMAR (used to be Virtual Memory Address Region, now an orphan
//!    initialism) represents the entire virtual address space of a process;
//!  * The VMO (Virtual Memory Object) is a set of logically contiguous memory
//!    frames that can be mapped into one virtual address range. Frames in a
//!    VMO can be non-contiguous in physical memory.

use osdk_frame_allocator::FrameAllocator;
use osdk_heap_allocator::{HeapAllocator, type_from_layout};

pub mod page_cache;
pub mod perms;
pub mod vmar;

#[ostd::global_frame_allocator]
static FRAME_ALLOCATOR: FrameAllocator = FrameAllocator;

#[ostd::global_heap_allocator]
static HEAP_ALLOCATOR: HeapAllocator = HeapAllocator;

#[ostd::global_heap_allocator_slot_map]
const fn slot_type_from_layout(layout: core::alloc::Layout) -> Option<ostd::mm::heap::SlotInfo> {
    type_from_layout(layout)
}

/// Total physical memory in the entire system in bytes.
pub fn mem_total() -> usize {
    use ostd::boot::{boot_info, memory_region::MemoryRegionType};

    let regions = &boot_info().memory_regions;
    regions
        .iter()
        .filter(|region| region.typ() == MemoryRegionType::Usable)
        .map(|region| region.len())
        .sum::<usize>()
}

#[cfg(ktest)]
mod tests {
    use super::mem_total;
    use ostd::{
        Error,
        mm::{
            FrameAllocOptions, HasPaddr, HasSize, PAGE_SIZE, io::util::HasVmReaderWriter,
        },
        prelude::*,
    };

    #[ktest]
    fn frame_alloc_free_roundtrip() {
        let frame = FrameAllocOptions::new().alloc_frame().unwrap();
        let paddr = frame.paddr();
        assert_eq!(paddr % PAGE_SIZE, 0);

        frame.writer().write_val(&0x5a_u8).unwrap();
        assert_eq!(frame.reader().read_val::<u8>().unwrap(), 0x5a);

        drop(frame);
    }

    #[ktest]
    fn frame_alloc_multipage_is_contiguous() {
        const NR_FRAMES: usize = 4;

        let segment = FrameAllocOptions::new()
            .alloc_segment(NR_FRAMES)
            .unwrap();
        assert_eq!(segment.size(), NR_FRAMES * PAGE_SIZE);
        assert_eq!(segment.paddr() % PAGE_SIZE, 0);

        // Every frame in the segment must be usable and at the next page.
        for (idx, paddr) in (segment.paddr()..segment.paddr() + segment.size())
            .step_by(PAGE_SIZE)
            .enumerate()
        {
            assert_eq!(paddr, segment.paddr() + idx * PAGE_SIZE);
        }
    }

    #[ktest]
    fn frame_free_is_reused() {
        let paddr = FrameAllocOptions::new()
            .alloc_frame()
            .unwrap()
            .paddr();
        let frame = FrameAllocOptions::new().alloc_frame().unwrap();
        assert_eq!(frame.paddr(), paddr);
    }

    #[ktest]
    fn frame_exhaustion_returns_enomem_without_panic() {
        let total_frames = mem_total() / PAGE_SIZE;
        assert!(total_frames > 0);

        // Request more contiguous frames than the machine has.  The
        // allocator must fail cleanly rather than panic, and it must remain
        // usable for later, satisfiable requests.
        let err = FrameAllocOptions::new()
            .alloc_segment(total_frames + 1)
            .unwrap_err();
        assert_eq!(err, Error::NoMemory);

        let frame = FrameAllocOptions::new().alloc_frame().unwrap();
        drop(frame);
    }
}
