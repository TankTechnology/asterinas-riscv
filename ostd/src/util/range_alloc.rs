// SPDX-License-Identifier: MPL-2.0

use alloc::collections::btree_map::BTreeMap;
use core::ops::Range;

use crate::sync::{PreemptDisabled, SpinLock, SpinLockGuard};

pub(crate) struct RangeAllocator {
    fullrange: Range<usize>,
    freelist: SpinLock<Option<BTreeMap<usize, FreeRange>>>,
    /// Highest address ever handed out, plus one.
    ///
    /// A block that starts at or above the watermark when it is allocated has
    /// never been allocated before, so nothing has ever been mapped there and
    /// no CPU can hold a translation for it. Virtual-area users rely on this
    /// to decide whether a mapping needs an inter-processor flush at all.
    high_water: core::sync::atomic::AtomicUsize,
}

/// An error returned when allocating from a [`RangeAllocator`].
#[derive(Debug)]
pub(crate) struct RangeAllocError;

impl RangeAllocator {
    pub(crate) const fn new(fullrange: Range<usize>) -> Self {
        Self {
            high_water: core::sync::atomic::AtomicUsize::new(fullrange.start),
            fullrange,
            freelist: SpinLock::new(None),
        }
    }

    pub(crate) const fn fullrange(&self) -> &Range<usize> {
        &self.fullrange
    }

    /// Allocates a specific kernel virtual area.
    pub(crate) fn alloc_specific(
        &self,
        allocate_range: &Range<usize>,
    ) -> Result<(), RangeAllocError> {
        if allocate_range.is_empty() {
            return Err(RangeAllocError);
        }

        let mut lock_guard = self.get_freelist_guard();
        let freelist = lock_guard.as_mut().unwrap();
        let mut target_node = None;
        let mut left_length = 0;
        let mut right_length = 0;

        for (key, value) in freelist.iter() {
            if value.block.end >= allocate_range.end && value.block.start <= allocate_range.start {
                target_node = Some(*key);
                left_length = allocate_range.start - value.block.start;
                right_length = value.block.end - allocate_range.end;
                break;
            }
        }

        if let Some(key) = target_node {
            if left_length == 0 {
                freelist.remove(&key);
            } else if let Some(freenode) = freelist.get_mut(&key) {
                freenode.block.end = allocate_range.start;
            }

            if right_length != 0 {
                freelist.insert(
                    allocate_range.end,
                    FreeRange::new(allocate_range.end..(allocate_range.end + right_length)),
                );
            }
        }

        if target_node.is_some() {
            // Keep the freshness watermark honest: a specific allocation also
            // makes its range "used" for every later allocation.
            self.high_water
                .fetch_max(allocate_range.end, core::sync::atomic::Ordering::Relaxed);
            Ok(())
        } else {
            Err(RangeAllocError)
        }
    }

    /// Allocates a range specific by the `size`.
    ///
    /// This is currently implemented with a simple FIRST-FIT algorithm.
    pub(crate) fn alloc(&self, size: usize) -> Result<Range<usize>, RangeAllocError> {
        self.alloc_with_freshness(size).map(|(range, _)| range)
    }

    /// Allocates `size` and reports whether the block was never used before.
    ///
    /// "Fresh" means the returned range starts at or above every address this
    /// allocator has ever handed out, so no page table has ever mapped it and
    /// no CPU can hold a translation or a cached invalid entry for it. The
    /// check runs under the free-list lock, so nothing can be allocated
    /// between the comparison and the watermark update.
    pub(crate) fn alloc_with_freshness(
        &self,
        size: usize,
    ) -> Result<(Range<usize>, bool), RangeAllocError> {
        let mut lock_guard = self.get_freelist_guard();
        let freelist = lock_guard.as_mut().unwrap();
        let mut allocate_range = None;
        let mut to_remove = None;

        for (key, value) in freelist.iter() {
            if value.block.end - value.block.start >= size {
                allocate_range = Some((value.block.end - size)..value.block.end);
                to_remove = Some(*key);
                break;
            }
        }

        if let Some(key) = to_remove
            && let Some(freenode) = freelist.get_mut(&key)
        {
            if freenode.block.end - size == freenode.block.start {
                freelist.remove(&key);
            } else {
                freenode.block.end -= size;
            }
        }

        if let Some(range) = allocate_range {
            let fresh = range.start >= self.high_water.load(core::sync::atomic::Ordering::Relaxed);
            self.high_water
                .fetch_max(range.end, core::sync::atomic::Ordering::Relaxed);
            Ok((range, fresh))
        } else {
            Err(RangeAllocError)
        }
    }

    /// Frees a `range`.
    pub(crate) fn free(&self, range: Range<usize>) {
        let mut lock_guard = self.freelist.lock();
        let freelist = lock_guard.as_mut().unwrap_or_else(|| {
            panic!("Free a 'KVirtArea' when 'VirtAddrAllocator' has not been initialized.")
        });
        // 1. get the previous free block, check if we can merge this block with the free one
        //     - if contiguous, merge this area with the free block.
        //     - if not contiguous, create a new free block, insert it into the list.
        let mut free_range = range.clone();

        if let Some((prev_va, prev_node)) = freelist
            .upper_bound_mut(core::ops::Bound::Excluded(&free_range.start))
            .peek_prev()
            && prev_node.block.end == free_range.start
        {
            let prev_va = *prev_va;
            free_range.start = prev_node.block.start;
            freelist.remove(&prev_va);
        }

        freelist.insert(free_range.start, FreeRange::new(free_range.clone()));

        // 2. check if we can merge the current block with the next block, if we can, do so.
        if let Some((next_va, next_node)) = freelist
            .lower_bound_mut(core::ops::Bound::Excluded(&free_range.start))
            .peek_next()
            && free_range.end == next_node.block.start
        {
            let next_va = *next_va;
            free_range.end = next_node.block.end;
            freelist.remove(&next_va);
            freelist.get_mut(&free_range.start).unwrap().block.end = free_range.end;
        }
    }

    fn get_freelist_guard(
        &self,
    ) -> SpinLockGuard<'_, Option<BTreeMap<usize, FreeRange>>, PreemptDisabled> {
        let mut lock_guard = self.freelist.lock();
        if lock_guard.is_none() {
            let mut freelist: BTreeMap<usize, FreeRange> = BTreeMap::new();
            freelist.insert(self.fullrange.start, FreeRange::new(self.fullrange.clone()));
            *lock_guard = Some(freelist);
        }
        lock_guard
    }
}

struct FreeRange {
    block: Range<usize>,
}

impl FreeRange {
    const fn new(range: Range<usize>) -> Self {
        Self { block: range }
    }
}
