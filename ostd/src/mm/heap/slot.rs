// SPDX-License-Identifier: MPL-2.0

//! Heap slots for allocations.

use core::{
    alloc::AllocError,
    ptr::NonNull,
    sync::atomic::{AtomicBool, Ordering},
};

use crate::{
    impl_frame_meta_for,
    mm::{
        FrameAllocOptions, HasPaddr, PAGE_SIZE, Paddr, Segment, SegmentedSegment, Vaddr,
        kspace::LINEAR_MAPPING_BASE_VADDR, paddr_to_vaddr,
    },
    sync::{LocalIrqDisabled, SpinLock},
};

/// Number of pages reserved for large heap slots (8 MiB).
///
/// Large slots are 2 KiB..N pages; reserving a contiguous run at the first
/// large allocation keeps them off the fragmented buddy allocator and avoids
/// mapping/unmapping a kernel virtual area for every slot.
const LARGE_POOL_PAGES: usize = 2048;

/// A reserved contiguous run of frames serving large heap slots.
struct LargeSlotPool {
    /// Kept alive for the lifetime of the kernel; its frames back the pool.
    _segment: Segment<()>,
    base: Vaddr,
    /// One bit per page: set means allocated.
    bits: [u64; LARGE_POOL_PAGES / 64],
}

impl LargeSlotPool {
    fn new(segment: Segment<()>) -> Self {
        Self {
            base: paddr_to_vaddr(segment.paddr()),
            _segment: segment,
            bits: [0; LARGE_POOL_PAGES / 64],
        }
    }

    fn is_used(&self, page: usize) -> bool {
        self.bits[page / 64] & (1 << (page % 64)) != 0
    }

    fn set_used(&mut self, page: usize, used: bool) {
        let word = &mut self.bits[page / 64];
        let mask = 1 << (page % 64);
        if used {
            *word |= mask;
        } else {
            *word &= !mask;
        }
    }

    /// First-fit allocation of `pages` consecutive pages.
    fn alloc(&mut self, pages: usize) -> Option<Vaddr> {
        if pages == 0 || pages > LARGE_POOL_PAGES {
            return None;
        }
        let mut run = 0;
        for page in 0..LARGE_POOL_PAGES {
            if self.is_used(page) {
                run = 0;
                continue;
            }
            run += 1;
            if run == pages {
                let start = page + 1 - pages;
                for page in start..=page {
                    self.set_used(page, true);
                }
                return Some(self.base + start * PAGE_SIZE);
            }
        }
        None
    }

    /// Returns `true` if `vaddr` belongs to the pool and was released.
    fn free(&mut self, vaddr: Vaddr, pages: usize) -> bool {
        let size = LARGE_POOL_PAGES * PAGE_SIZE;
        if vaddr < self.base || vaddr >= self.base + size {
            return false;
        }
        let start = (vaddr - self.base) / PAGE_SIZE;
        if start + pages > LARGE_POOL_PAGES {
            return false;
        }
        for page in start..start + pages {
            self.set_used(page, false);
        }
        true
    }
}

static LARGE_POOL: SpinLock<Option<LargeSlotPool>, LocalIrqDisabled> = SpinLock::new(None);

/// Whether the one-shot pool initialization has been attempted.
static LARGE_POOL_INIT_ATTEMPTED: AtomicBool = AtomicBool::new(false);

fn pool_alloc(pages: usize) -> Option<Vaddr> {
    let mut pool = LARGE_POOL.lock();
    if pool.is_none() && !LARGE_POOL_INIT_ATTEMPTED.swap(true, Ordering::Relaxed) {
        // Reserve while the buddy allocator can still find a large run.
        if let Ok(segment) = FrameAllocOptions::new()
            .zeroed(false)
            .alloc_segment(LARGE_POOL_PAGES)
        {
            *pool = Some(LargeSlotPool::new(segment));
        }
    }
    pool.as_mut().and_then(|pool| pool.alloc(pages))
}

fn pool_free(vaddr: Vaddr, pages: usize) -> bool {
    let mut pool = LARGE_POOL.lock();
    pool.as_mut().is_some_and(|pool| pool.free(vaddr, pages))
}

/// A slot that will become or has been turned from a heap allocation.
///
/// Heap slots can come from [`Slab`] or directly from a typed [`Segment`].
///
/// Heap slots can be used to fulfill heap allocations requested by the allocator.
/// Upon deallocation, the deallocated memory also becomes a heap slot.
///
/// The size of the heap slot must match the slot size of the [`Slab`] or the
/// size of the [`Segment`].
///
/// [`Slab`]: super::Slab
pub struct HeapSlot {
    /// The address of the slot.
    addr: NonNull<u8>,
    /// The type and size of the slot.
    info: SlotInfo,
}

/// The type and size of the heap slot that should be used for the allocation.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SlotInfo {
    /// The slot is from a [`super::Slab`].
    ///
    /// The size of the slot and the corresponding slab are provided.
    /// Both values are identical.
    SlabSlot(usize),
    /// The slot is from a [`Segment`].
    ///
    /// The size of the slot and the corresponding segment are provided.
    /// Both values are identical.
    LargeSlot(usize),
}

impl SlotInfo {
    /// Gets the size of the slot.
    pub fn size(&self) -> usize {
        match self {
            Self::SlabSlot(size) => *size,
            Self::LargeSlot(size) => *size,
        }
    }
}

impl HeapSlot {
    /// Creates a new pointer to a heap slot.
    ///
    /// # Safety
    ///
    /// The pointer to the slot must either:
    ///  - be a free slot in a [`super::Slab`], or
    ///  - be a free slot in a [`Segment`].
    ///
    /// If the pointer is from a [`super::Slab`] or [`Segment`], the slot must
    /// have a size that matches the slot size of the slab or segment respectively.
    pub(super) unsafe fn new(addr: NonNull<u8>, info: SlotInfo) -> Self {
        Self { addr, info }
    }

    /// Allocates a large slot.
    ///
    /// This function allocates in units of [`PAGE_SIZE`] bytes.
    ///
    /// This function returns an error if the frame allocation fails.
    ///
    /// # Panics
    ///
    /// This function panics if the size is not a multiple of [`PAGE_SIZE`].
    pub fn alloc_large(size: usize) -> Result<Self, AllocError> {
        assert_eq!(size % PAGE_SIZE, 0);
        let nframes = size / PAGE_SIZE;

        // Large slots normally come from the reserved contiguous pool. This
        // avoids both the fragmented buddy allocator and the page-table work
        // of mapping a segmented allocation on every slot.
        if let Some(vaddr) = pool_alloc(nframes) {
            return Ok(Self {
                addr: NonNull::new(vaddr as *mut u8).unwrap(),
                info: SlotInfo::LargeSlot(size),
            });
        }

        let segment = FrameAllocOptions::new()
            .zeroed(false)
            .alloc_segmented_with(nframes, |_| LargeAllocFrameMeta)
            .map_err(|_| {
                crate::error!("Failed to allocate a large slot");
                AllocError
            })?;

        // Hand the virtual address to the caller and keep the allocation
        // alive. Deallocation reconstructs the backing from this address.
        let vaddr = segment.leak_vaddr();

        Ok(Self {
            addr: NonNull::new(vaddr as *mut u8).unwrap(),
            info: SlotInfo::LargeSlot(size),
        })
    }

    /// Deallocates a large slot.
    ///
    /// # Panics
    ///
    /// This function aborts if the slot was not allocated with
    /// [`HeapSlot::alloc_large`], as it requires specific memory management
    /// operations that only apply to large slots.
    pub fn dealloc_large(self) {
        let SlotInfo::LargeSlot(size) = self.info else {
            crate::error!(
                "Deallocating a large slot that was not allocated with `HeapSlot::alloc_large`"
            );
            crate::panic::abort();
        };

        debug_assert_eq!(size % PAGE_SIZE, 0);
        let vaddr = self.addr.as_ptr() as Vaddr;
        debug_assert_eq!(vaddr % PAGE_SIZE, 0);

        if pool_free(vaddr, size / PAGE_SIZE) {
            return;
        }

        // SAFETY: The allocation was leaked in `alloc_large` and has not been
        // reclaimed since.
        unsafe {
            SegmentedSegment::<LargeAllocFrameMeta>::reclaim_from_vaddr(vaddr, size);
        }
    }

    /// Gets the physical address of the slot.
    pub fn paddr(&self) -> Paddr {
        self.addr.as_ptr() as Vaddr - LINEAR_MAPPING_BASE_VADDR
    }

    /// Gets the size of the slot.
    pub fn size(&self) -> usize {
        match self.info {
            SlotInfo::SlabSlot(size) => size,
            SlotInfo::LargeSlot(size) => size,
        }
    }

    /// Gets the type and size of the slot.
    pub fn info(&self) -> SlotInfo {
        self.info
    }

    /// Gets the pointer to the slot.
    pub fn as_ptr(&self) -> *mut u8 {
        self.addr.as_ptr()
    }
}

/// The frames allocated for a large allocation.
#[derive(Debug)]
pub(crate) struct LargeAllocFrameMeta;

impl_frame_meta_for!(LargeAllocFrameMeta);
