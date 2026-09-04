// SPDX-License-Identifier: MPL-2.0

use core::ops::Range;

use ostd::mm::{HasPaddr, Paddr, frame::linked_list::LinkedList};

use crate::chunk::{BuddyOrder, FreeChunk, FreeHeadMeta, size_of_order};

/// A set of free buddy chunks.
pub(crate) struct BuddySet<const MAX_ORDER: BuddyOrder> {
    /// The sum of the sizes of all free chunks.
    total_size: usize,
    /// The lists of free buddy chunks for each orders.
    lists: [LinkedList<FreeHeadMeta>; MAX_ORDER],
}

impl<const MAX_ORDER: BuddyOrder> BuddySet<MAX_ORDER> {
    /// Creates a new empty set of free lists.
    pub(crate) const fn new_empty() -> Self {
        Self {
            total_size: 0,
            lists: [const { LinkedList::new() }; MAX_ORDER],
        }
    }

    /// Gets the total size of free chunks.
    pub(crate) fn total_size(&self) -> usize {
        self.total_size
    }

    /// Inserts a free chunk into the set.
    pub(crate) fn insert_chunk(&mut self, addr: Paddr, order: BuddyOrder) {
        debug_assert!(order < MAX_ORDER);

        let inserted_size = size_of_order(order);
        let mut chunk = FreeChunk::from_unused(addr, order);

        let order = chunk.order();
        // Coalesce the chunk with its buddy whenever possible.
        for (i, list) in self.lists.iter_mut().enumerate().skip(order) {
            if i + 1 >= MAX_ORDER {
                // The chunk is already the largest one.
                break;
            }
            let buddy_addr = chunk.buddy();
            let Some(mut cursor) = list.cursor_mut_at(buddy_addr) else {
                // The buddy is not in this free list, so we can't coalesce.
                break;
            };
            let taken = cursor.take_current().unwrap();
            debug_assert_eq!(buddy_addr, taken.paddr());
            chunk = chunk.merge_free(FreeChunk::from_free_head(taken));
        }
        // Insert the coalesced chunk into the free lists.
        let order = chunk.order();
        self.lists[order].push_front(chunk.into_unique_head());

        self.total_size += inserted_size;
    }

    /// Allocates a chunk from the set.
    ///
    /// The function will choose and remove a buddy chunk of the given order
    /// from the set. The address of the chunk will be returned.
    pub(crate) fn alloc_chunk(&mut self, order: BuddyOrder) -> Option<Paddr> {
        // Find the first non-empty size class larger than the requested order.
        let mut non_empty = None;
        for (i, list) in self.lists.iter_mut().enumerate().skip(order) {
            if !list.is_empty() {
                non_empty = Some(i);
                break;
            }
        }
        let non_empty = non_empty?;
        let mut chunk = {
            let head = self.lists[non_empty].pop_front().unwrap();
            debug_assert_eq!(head.meta().order(), non_empty as BuddyOrder);

            Some(FreeChunk::from_free_head(head))
        };

        // Split the chunk.
        for i in (order + 1..=non_empty).rev() {
            let (left_sub, right_sub) = chunk.take().unwrap().split_free();
            // Push the right sub-chunk back to the free lists.
            let right_sub = right_sub.into_unique_head();
            debug_assert_eq!(right_sub.meta().order(), (i - 1) as BuddyOrder);
            self.lists[i - 1].push_front(right_sub);
            // Pass the left sub-chunk to the next iteration.
            chunk = Some(left_sub);
        }

        let allocated_size = size_of_order(order);

        self.total_size -= allocated_size;

        // The remaining chunk is the one we want.
        let head_frame = chunk.take().unwrap().into_unique_head();
        let paddr = head_frame.paddr();
        head_frame.reset_as_unused(); // It will "drop" the frame without up-calling us.
        Some(paddr)
    }

    /// Allocates one chunk that is wholly contained in `range`.
    pub(crate) fn alloc_chunk_in(
        &mut self,
        order: BuddyOrder,
        range: Range<Paddr>,
    ) -> Option<Paddr> {
        if order >= MAX_ORDER || range.start >= range.end {
            return None;
        }

        let requested_size = size_of_order(order);
        let mut selected = None;
        for source_order in order..MAX_ORDER {
            let source_size = size_of_order(source_order);
            let mut cursor = self.lists[source_order].cursor_front_mut();
            while let Some(chunk_start) = cursor.current_paddr() {
                let chunk_end = chunk_start.checked_add(source_size)?;
                let candidate_start = chunk_start.max(range.start);
                let candidate_start =
                    candidate_start.checked_add(requested_size - 1)? & !(requested_size - 1);
                let candidate_end = candidate_start.checked_add(requested_size)?;
                if candidate_end <= chunk_end && candidate_end <= range.end {
                    let chunk = FreeChunk::from_free_head(cursor.take_current().unwrap());
                    selected = Some((chunk, source_order, candidate_start));
                    break;
                }
                cursor.move_next();
            }
            if selected.is_some() {
                break;
            }
        }

        let (mut chunk, mut chunk_order, candidate_start) = selected?;
        while chunk_order > order {
            let (left, right) = chunk.split_free();
            chunk_order -= 1;
            let (selected_child, unused_child) = if candidate_start < right.addr() {
                (left, right)
            } else {
                (right, left)
            };
            self.lists[chunk_order].push_front(unused_child.into_unique_head());
            chunk = selected_child;
        }

        debug_assert_eq!(chunk.addr(), candidate_start);
        self.total_size -= requested_size;
        let head_frame = chunk.into_unique_head();
        let paddr = head_frame.paddr();
        head_frame.reset_as_unused();
        Some(paddr)
    }

    /// Moves every free chunk into a buddy set with at least as many orders.
    ///
    /// Inserting the chunks again is intentional: chunks that were separated
    /// across CPU-local sets can then coalesce in the destination set.
    pub(crate) fn drain_into<const DEST_MAX_ORDER: BuddyOrder>(
        &mut self,
        dest: &mut BuddySet<DEST_MAX_ORDER>,
    ) {
        assert!(MAX_ORDER <= DEST_MAX_ORDER);

        for (order, list) in self.lists.iter_mut().enumerate() {
            while let Some(head) = list.pop_front() {
                let addr = head.paddr();
                head.reset_as_unused();
                self.total_size -= size_of_order(order);
                dest.insert_chunk(addr, order);
            }
        }

        debug_assert_eq!(self.total_size, 0);
    }
}

#[cfg(ktest)]
mod test {
    use super::*;
    use crate::test::MockMemoryRegion;
    use ostd::prelude::ktest;

    #[ktest]
    fn buddy_set_insert_alloc() {
        let region_order = 4;
        let region_size = size_of_order(region_order);
        let region = MockMemoryRegion::alloc(region_size);
        let region_start = region.paddr();

        let mut set = BuddySet::<5>::new_empty();
        set.insert_chunk(region_start, region_order);
        assert!(set.total_size() == region_size);

        // Allocating chunks of orders of 0, 0, 1, 2, 3 should be okay.
        let chunk1 = set.alloc_chunk(0).unwrap();
        assert!(set.total_size() == region_size - size_of_order(0));
        let chunk2 = set.alloc_chunk(0).unwrap();
        assert!(set.total_size() == region_size - size_of_order(1));
        let chunk3 = set.alloc_chunk(1).unwrap();
        assert!(set.total_size() == region_size - size_of_order(2));
        let chunk4 = set.alloc_chunk(2).unwrap();
        assert!(set.total_size() == region_size - size_of_order(3));
        let chunk5 = set.alloc_chunk(3).unwrap();
        assert!(set.total_size() == 0);

        // Putting them back should enable us to allocate the original region.
        set.insert_chunk(chunk3, 1);
        assert!(set.total_size() == size_of_order(1));
        set.insert_chunk(chunk1, 0);
        assert!(set.total_size() == size_of_order(0) + size_of_order(1));
        set.insert_chunk(chunk5, 3);
        assert!(set.total_size() == size_of_order(0) + size_of_order(1) + size_of_order(3));
        set.insert_chunk(chunk2, 0);
        assert!(set.total_size() == size_of_order(2) + size_of_order(3));
        set.insert_chunk(chunk4, 2);
        assert!(set.total_size() == size_of_order(4));

        let chunk = set.alloc_chunk(region_order).unwrap();
        assert!(chunk == region_start);
        assert!(set.total_size() == 0);
    }

    #[ktest]
    fn megrez_sdma_buddy_set_allocates_only_inside_the_requested_range() {
        let region_order = 4;
        let region_size = size_of_order(region_order);
        let region = MockMemoryRegion::alloc(region_size);
        let region_start = region.paddr();
        let target_start = region_start + size_of_order(3);

        let mut set = BuddySet::<5>::new_empty();
        set.insert_chunk(region_start, region_order);

        let selected = set
            .alloc_chunk_in(1, target_start..target_start + size_of_order(2))
            .unwrap();
        assert_eq!(selected, target_start);
        assert_eq!(set.total_size(), region_size - size_of_order(1));

        assert_eq!(set.alloc_chunk(3), Some(region_start));
        assert_eq!(set.alloc_chunk(2), Some(target_start + size_of_order(2)));
        assert_eq!(set.alloc_chunk(1), Some(target_start + size_of_order(1)));
        assert_eq!(set.total_size(), 0);
    }

    #[ktest]
    fn megrez_sdma_buddy_set_rejects_an_out_of_range_request_without_mutation() {
        let region_order = 3;
        let region_size = size_of_order(region_order);
        let region = MockMemoryRegion::alloc(region_size);
        let region_start = region.paddr();

        let mut set = BuddySet::<4>::new_empty();
        set.insert_chunk(region_start, region_order);

        assert_eq!(
            set.alloc_chunk_in(
                1,
                region_start + region_size..region_start + region_size * 2
            ),
            None
        );
        assert_eq!(set.total_size(), region_size);
        assert_eq!(set.alloc_chunk(region_order), Some(region_start));
        assert_eq!(set.total_size(), 0);
    }

    #[ktest]
    fn draining_sets_coalesces_chunks_in_destination() {
        let region_order = 4;
        let region_size = size_of_order(region_order);
        let region = MockMemoryRegion::alloc(region_size);
        let region_start = region.paddr();

        let mut source = BuddySet::<5>::new_empty();
        source.insert_chunk(region_start, region_order);
        let left = source.alloc_chunk(region_order - 1).unwrap();
        let right = source.alloc_chunk(region_order - 1).unwrap();

        let mut local1 = BuddySet::<5>::new_empty();
        let mut local2 = BuddySet::<5>::new_empty();
        let mut global = BuddySet::<6>::new_empty();
        local1.insert_chunk(left, region_order - 1);
        local2.insert_chunk(right, region_order - 1);

        assert_eq!(global.alloc_chunk(region_order), None);
        local1.drain_into(&mut global);
        assert_eq!(global.alloc_chunk(region_order), None);
        local2.drain_into(&mut global);

        assert_eq!(local1.total_size(), 0);
        assert_eq!(local2.total_size(), 0);
        assert_eq!(global.alloc_chunk(region_order), Some(region_start));
        assert_eq!(global.total_size(), 0);
    }
}
