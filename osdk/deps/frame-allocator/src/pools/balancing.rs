// SPDX-License-Identifier: MPL-2.0

//! Controlling the balancing between CPU-local free pools and the global free pool.

use ostd::cpu::num_cpus;

use super::{BuddyOrder, BuddySet, MAX_LOCAL_BUDDY_ORDER, OnDemandGlobalLock, lesser_order_of};

use crate::chunk::{size_of_order, split_to_order};

/// Controls the expected size of cache for each CPU-local free pool.
///
/// The expected size will be the size of `GLOBAL_POOL` divided by the number
/// of the CPUs, and then divided by this constant.
const CACHE_EXPECTED_PORTION: usize = 2;

/// Prevents CPU-local pools from retaining a large fraction of physical RAM.
///
/// Freeing memory on a different CPU from the one that allocated it can split
/// adjacent buddies across local pools. Bounding each pool keeps most memory
/// in the global buddy set, where cross-CPU frees can coalesce.
const CACHE_EXPECTED_SIZE_LIMIT: usize = 8 * 1024 * 1024;

/// Returns the expected size of cache for each CPU-local free pool.
///
/// It depends on the size of the global free pool.
fn cache_expected_size(global_size: usize) -> usize {
    (global_size / num_cpus() / CACHE_EXPECTED_PORTION).min(CACHE_EXPECTED_SIZE_LIMIT)
}

/// Controls the minimal size of cache for each CPU-local free pool.
///
/// The minimal will be the expected size divided by this constant.
const CACHE_MINIMAL_PORTION: usize = 8;

/// Returns the minimal size of cache for each CPU-local free pool.
///
/// It depends on the size of the global free pool.
fn cache_minimal_size(global_size: usize) -> usize {
    cache_expected_size(global_size) / CACHE_MINIMAL_PORTION
}

/// Controls the maximal size of cache for each CPU-local free pool.
///
/// The maximal will be the expected size multiplied by this constant.
const CACHE_MAXIMAL_MULTIPLIER: usize = 2;

/// The most chunks one balancing pass moves.
///
/// Attempting to move the whole target amount at once is unbounded in work
/// when the source pool is fragmented: with only 32 KiB chunks available, an
/// 8 MiB target needs 256 chunk moves. The cap keeps each pass short; the
/// pool is topped up by subsequent allocations instead.
const MAX_CHUNKS_PER_BALANCE: usize = 4;

/// Returns the maximal size of cache for each CPU-local free pool.
///
/// It depends on the size of the global free pool.
fn cache_maximal_size(global_size: usize) -> usize {
    cache_expected_size(global_size) * CACHE_MAXIMAL_MULTIPLIER
}

/// Balances a local cache and the global free pool.
pub(super) fn balance(
    local: &mut BuddySet<MAX_LOCAL_BUDDY_ORDER>,
    global: &mut OnDemandGlobalLock,
) {
    let global_size = global.get_global_size();

    let minimal_local_size = cache_minimal_size(global_size);
    let expected_local_size = cache_expected_size(global_size);
    let maximal_local_size = cache_maximal_size(global_size);

    let local_size = local.total_size();

    if local_size >= maximal_local_size {
        // Move local frames to the global pool.
        if local_size == 0 {
            return;
        }

        let expected_removal = local_size - expected_local_size;
        let lesser_order = lesser_order_of(expected_removal);

        balance_to(local, &mut *global.get(), lesser_order);
    } else if local_size < minimal_local_size {
        // Move global frames to the local pool.
        if global_size == 0 {
            return;
        }

        let expected_allocation = expected_local_size - local_size;
        let lesser_order = lesser_order_of(expected_allocation);

        balance_to(&mut *global.get(), local, lesser_order);
    }
}

/// Balances from `a` to `b`.
///
/// Moves chunks of the largest available size first, until `order`'s worth of
/// memory has been moved or the source pool has nothing left to give.
///
/// The move is capped so that the time spent holding the source pool's lock
/// stays bounded. If the cap is reached, the target pool remains below its
/// target size and later allocations balance it again.
fn balance_to<const MAX_ORDER1: BuddyOrder, const MAX_ORDER2: BuddyOrder>(
    a: &mut BuddySet<MAX_ORDER1>,
    b: &mut BuddySet<MAX_ORDER2>,
    order: BuddyOrder,
) {
    let mut remaining = size_of_order(order);
    let mut moved = 0;

    for source_order in (0..=order).rev() {
        let chunk_size = size_of_order(source_order);

        while remaining >= chunk_size && moved < MAX_CHUNKS_PER_BALANCE {
            let Some(addr) = a.alloc_chunk(source_order) else {
                break;
            };

            if source_order >= MAX_ORDER2 {
                let inserted_order = MAX_ORDER2 - 1;
                split_to_order(addr, source_order, inserted_order).for_each(|addr| {
                    b.insert_chunk(addr, inserted_order);
                });
            } else {
                b.insert_chunk(addr, source_order);
            }

            remaining -= chunk_size;
            moved += 1;
        }

        if moved >= MAX_CHUNKS_PER_BALANCE {
            break;
        }
    }
}

#[cfg(ktest)]
mod test {
    use super::*;
    use crate::test::MockMemoryRegion;
    use ostd::prelude::ktest;

    #[ktest]
    fn cpu_local_cache_target_is_bounded() {
        let uncapped_target = CACHE_EXPECTED_SIZE_LIMIT / 2;
        let small_global = uncapped_target * num_cpus() * CACHE_EXPECTED_PORTION;
        assert_eq!(cache_expected_size(small_global), uncapped_target);

        let large_global = CACHE_EXPECTED_SIZE_LIMIT * num_cpus() * CACHE_EXPECTED_PORTION * 2;
        assert_eq!(cache_expected_size(large_global), CACHE_EXPECTED_SIZE_LIMIT);
    }

    #[ktest]
    fn balancing_moves_a_bounded_number_of_fragmented_chunks() {
        // Every other chunk, so adjacent chunks are not buddies and cannot merge.
        const CHUNK_ORDER: BuddyOrder = 3;
        const CHUNKS: usize = 2 * MAX_CHUNKS_PER_BALANCE;
        let chunk_size = size_of_order(CHUNK_ORDER);
        let region = MockMemoryRegion::alloc(2 * CHUNKS * chunk_size);

        let mut source = BuddySet::<8>::new_empty();
        let mut target = BuddySet::<MAX_LOCAL_BUDDY_ORDER>::new_empty();
        for index in 0..CHUNKS {
            source.insert_chunk(region.paddr() + 2 * index * chunk_size, CHUNK_ORDER);
        }
        assert_eq!(source.total_size(), CHUNKS * chunk_size);

        // The source has no chunk large enough for the requested order, so the
        // pass has to move the target amount in small chunks. It must stop at
        // the cap instead of draining everything in one lock-held burst.
        balance_to(&mut source, &mut target, 11);

        assert_eq!(target.total_size(), MAX_CHUNKS_PER_BALANCE * chunk_size);
        assert_eq!(
            source.total_size(),
            (CHUNKS - MAX_CHUNKS_PER_BALANCE) * chunk_size
        );

        // Empty both sets before the region is returned: a free chunk holds
        // frame metadata in the "unique" state, and dropping the region while
        // chunks are still linked in would hand those frames back in that
        // state.
        while source.total_size() > 0 {
            source.alloc_chunk(CHUNK_ORDER).unwrap();
        }
        while target.total_size() > 0 {
            target.alloc_chunk(CHUNK_ORDER).unwrap();
        }
    }
}
