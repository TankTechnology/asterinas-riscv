// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::Ordering;

use crate::{
    arch::mm::tlb_flush_addr_range,
    cpu::{AtomicCpuSet, CpuSet, PinCurrentCpu},
    impl_frame_meta_for,
    irq::DisabledLocalIrqGuard,
    mm::{
        FrameAllocOptions, PAGE_SIZE, Segment,
        kspace::kvirt_area::KVirtArea,
        page_prop::{CachePolicy, PageFlags, PageProperty, PrivilegedPageFlags},
    },
    prelude::*,
};

/// The kernel stack size of a task, specified in pages.
///
/// By default, we choose a rather large stack size.
/// OSTD users can choose a smaller size by specifying
/// the `OSTD_TASK_STACK_SIZE_IN_PAGES` environment variable
/// at build time.
const STACK_SIZE_IN_PAGES: u32 =
    if let Some(size_str) = option_env!("OSTD_TASK_STACK_SIZE_IN_PAGES") {
        match u32::from_str_radix(size_str, 10) {
            Ok(size) => size,
            Err(_) => panic!(
                "The environment variable `OSTD_TASK_STACK_SIZE_IN_PAGES` \
                    specifies an invalid value"
            ),
        }
    } else {
        DEFAULT_STACK_SIZE_IN_PAGES
    };

/// The default kernel stack size of a task, specified in pages.
const DEFAULT_STACK_SIZE_IN_PAGES: u32 = 128;

const KERNEL_STACK_SIZE: usize = STACK_SIZE_IN_PAGES as usize * PAGE_SIZE;

/// The number of pages in each physically contiguous stack segment.
///
/// The stack used to be allocated as one 128-page segment, which needs an
/// order-7 buddy block and fails first when physical memory is fragmented.
/// Splitting it into order-4 (16-page) segments keeps the virtual layout
/// unchanged while making the physical allocation much easier to satisfy.
const STACK_SEGMENT_SIZE_IN_PAGES: usize = 16;

const STACK_SIZE_IN_PAGES_USIZE: usize = KERNEL_STACK_SIZE / PAGE_SIZE;

const STACK_SEGMENT_COUNT: usize =
    (STACK_SIZE_IN_PAGES_USIZE + STACK_SEGMENT_SIZE_IN_PAGES - 1) / STACK_SEGMENT_SIZE_IN_PAGES;

#[derive(Debug)]
pub(super) struct KernelStack {
    kvirt_area: KVirtArea,
    tlb_coherent: AtomicCpuSet,
    end_vaddr: Vaddr,
}

#[derive(Debug, Default)]
struct KernelStackMeta;

impl_frame_meta_for!(KernelStackMeta);

impl KernelStack {
    /// Generates a kernel stack with guard pages.
    ///
    /// 4 additional virtual pages are reserved as guard pages (two below and
    /// two above the stack); they are left unmapped and consume no frames.
    //
    // TODO: We map kernel stacks in the kernel virtual areas, which incurs
    // non-negligible TLB and mapping overhead on task creation. This could
    // be improved by caching/reusing kernel stacks with a pool.
    pub(super) fn new_with_guard_page() -> Result<Self> {
        // Allocate the stack in several physically contiguous segments and
        // map them into one contiguous virtual range.  Only the virtual
        // layout is part of the contract; the frames may be scattered.
        let mut segments: [Option<Segment<KernelStackMeta>>; STACK_SEGMENT_COUNT] =
            [const { None }; STACK_SEGMENT_COUNT];
        let mut remaining = STACK_SIZE_IN_PAGES_USIZE;
        for slot in &mut segments {
            let pages = remaining.min(STACK_SEGMENT_SIZE_IN_PAGES);
            debug_assert!(pages > 0);
            // If any segment cannot be allocated, `segments` is dropped on
            // this early return, which releases every segment allocated so
            // far.  Do not replace this with a manual cleanup path.
            *slot = Some(
                FrameAllocOptions::new()
                    .zeroed(false)
                    .alloc_segment_with(pages, |_| KernelStackMeta)?,
            );
            remaining -= pages;
        }
        debug_assert_eq!(remaining, 0);

        let frames = segments
            .iter_mut()
            .filter_map(|segment| segment.take())
            .flat_map(|segment| segment.into_iter());
        let prop = PageProperty {
            flags: PageFlags::RW,
            cache: CachePolicy::Writeback,
            priv_flags: PrivilegedPageFlags::empty(),
        };
        let new_kvirt_area = KVirtArea::map_frames(
            KERNEL_STACK_SIZE + 4 * PAGE_SIZE,
            2 * PAGE_SIZE,
            frames,
            prop,
        );
        let mapped_start = new_kvirt_area.range().start + 2 * PAGE_SIZE;
        let mapped_end = mapped_start + KERNEL_STACK_SIZE;
        Ok(Self {
            kvirt_area: new_kvirt_area,
            tlb_coherent: AtomicCpuSet::new(CpuSet::new_empty()),
            end_vaddr: mapped_end,
        })
    }

    /// Flushes the TLB for the current CPU if necessary.
    pub(super) fn flush_tlb(&self, irq_guard: &DisabledLocalIrqGuard) {
        let cur_cpu = irq_guard.current_cpu();
        if !self.tlb_coherent.contains(cur_cpu, Ordering::Relaxed) {
            tlb_flush_addr_range(&self.kvirt_area.range());
            self.tlb_coherent.add(cur_cpu, Ordering::Relaxed);
        }
    }

    pub(super) fn end_vaddr(&self) -> Vaddr {
        self.end_vaddr
    }
}
