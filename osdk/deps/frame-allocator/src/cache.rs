// SPDX-License-Identifier: MPL-2.0

//! A fixed-size local cache for frame allocation.

use core::{
    alloc::Layout,
    cell::RefCell,
    sync::atomic::{AtomicUsize, Ordering},
};

use ostd::{
    cpu_local,
    irq::DisabledLocalIrqGuard,
    mm::{PAGE_SIZE, Paddr},
};

/// Number of refill failures reported so far.
///
/// Reporting every failure would flood the console once order-3 blocks run
/// out, because each single-frame allocation retries the refill.
static REFILL_FAILURE_REPORTS: AtomicUsize = AtomicUsize::new(0);

/// How many refill failures are printed before the report is suppressed.
const MAX_REFILL_FAILURE_REPORTS: usize = 8;

cpu_local! {
    static CACHE: RefCell<CacheOfSizes> = RefCell::new(CacheOfSizes::new());
}

struct CacheOfSizes {
    cache1: CacheArray<1, 12>,
    cache2: CacheArray<2, 6>,
    cache3: CacheArray<3, 6>,
    cache4: CacheArray<4, 6>,
}

/// A fixed-size local cache for frame allocation.
///
/// Each cache array contains at most `COUNT` segments. Each segment contains
/// `NR_CONT_FRAMES` contiguous frames.
struct CacheArray<const NR_CONT_FRAMES: usize, const COUNT: usize> {
    inner: [Option<Paddr>; COUNT],
    size: usize,
}

impl<const NR_CONT_FRAMES: usize, const COUNT: usize> CacheArray<NR_CONT_FRAMES, COUNT> {
    const fn new() -> Self {
        Self {
            inner: [const { None }; COUNT],
            size: 0,
        }
    }

    /// The size of the segments that this cache manages.
    const fn segment_size() -> usize {
        NR_CONT_FRAMES * PAGE_SIZE
    }

    /// Allocates a segment of frames.
    ///
    /// It may allocate directly from this cache. If the cache is empty, it
    /// will fill the cache.
    fn alloc(&mut self, guard: &DisabledLocalIrqGuard) -> Option<Paddr> {
        if let Some(frame) = self.pop_front() {
            return Some(frame);
        }

        let nr_to_alloc = COUNT * 2 / 3;
        let refill_layout =
            Layout::from_size_align(nr_to_alloc * Self::segment_size(), PAGE_SIZE).unwrap();
        let Some(allocated) = super::pools::alloc(guard, refill_layout) else {
            // Diagnostics only: a request for one or a few frames can fail here
            // even when plenty of small free chunks exist, because the refill
            // asks for a larger contiguous block.  Record that block's order,
            // but only for the first few failures: once high orders run out,
            // every single-frame allocation would otherwise log.
            let reports = REFILL_FAILURE_REPORTS.fetch_add(1, Ordering::Relaxed);
            if reports < MAX_REFILL_FAILURE_REPORTS {
                ostd::warn!(
                    "cache refill failed: class_frames={} refill_bytes={} refill_order={}",
                    NR_CONT_FRAMES,
                    refill_layout.size(),
                    crate::chunk::greater_order_of(refill_layout.size()),
                );
            }
            return None;
        };

        for i in 1..nr_to_alloc {
            self.push_front(allocated + i * Self::segment_size());
        }

        Some(allocated)
    }

    /// Deallocates a segment of frames.
    ///
    /// It may deallocate directly to this cache. If the cache is full, it will
    /// deallocate to the global pool.
    fn dealloc(&mut self, guard: &DisabledLocalIrqGuard, addr: Paddr) {
        if self.push_front(addr).is_none() {
            let nr_to_dealloc = COUNT * 2 / 3 + 1;

            let segments = (0..nr_to_dealloc).map(|i| {
                if i == 0 {
                    (addr, Self::segment_size())
                } else {
                    (self.pop_front().unwrap(), Self::segment_size())
                }
            });

            super::pools::dealloc(guard, segments);
        };
    }

    fn push_front(&mut self, frame: Paddr) -> Option<()> {
        if self.size == COUNT {
            return None;
        }

        self.inner[self.size] = Some(frame);
        self.size += 1;
        Some(())
    }

    fn pop_front(&mut self) -> Option<Paddr> {
        if self.size == 0 {
            return None;
        }

        let frame = self.inner[self.size - 1].take().unwrap();
        self.size -= 1;
        Some(frame)
    }
}

impl CacheOfSizes {
    const fn new() -> Self {
        Self {
            cache1: CacheArray::new(),
            cache2: CacheArray::new(),
            cache3: CacheArray::new(),
            cache4: CacheArray::new(),
        }
    }
}

pub(super) fn alloc(guard: &DisabledLocalIrqGuard, layout: Layout) -> Option<Paddr> {
    let nr_frames = layout.size() / PAGE_SIZE;
    if layout.align() > layout.size() {
        return super::pools::alloc(guard, layout);
    }

    let cache_cell = CACHE.get_with(guard);
    let mut cache = cache_cell.borrow_mut();

    let cached = match nr_frames {
        1 => cache.cache1.alloc(guard),
        2 => cache.cache2.alloc(guard),
        3 => cache.cache3.alloc(guard),
        4 => cache.cache4.alloc(guard),
        _ => return super::pools::alloc(guard, layout),
    };
    drop(cache);

    // A class cache refill asks for a larger contiguous block (eight pages for
    // single frames), so it can fail while smaller free chunks are plentiful.
    // Fall back to the exact request, which needs at most the requested order.
    cached.or_else(|| super::pools::alloc(guard, layout))
}

pub(super) fn dealloc(guard: &DisabledLocalIrqGuard, addr: Paddr, size: usize) {
    let nr_frames = size / PAGE_SIZE;
    if nr_frames > 4 {
        super::pools::dealloc(guard, [(addr, size)].into_iter());
        return;
    }

    let cache_cell = CACHE.get_with(guard);
    let mut cache = cache_cell.borrow_mut();

    match nr_frames {
        1 => cache.cache1.dealloc(guard, addr),
        2 => cache.cache2.dealloc(guard, addr),
        3 => cache.cache3.dealloc(guard, addr),
        4 => cache.cache4.dealloc(guard, addr),
        _ => super::pools::dealloc(guard, [(addr, size)].into_iter()),
    }
}
