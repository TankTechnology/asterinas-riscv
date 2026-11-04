// SPDX-License-Identifier: MPL-2.0

//! The kernel's multi-lock acquisition order.
//!
//! A set of locks with a *total* order cannot deadlock: every waiter holds
//! only lower-indexed locks while asking for a higher-indexed one, so no cycle
//! can form. This crate records the order for the paths that nest locks —
//! futex buckets, the VMAR lock, the inode inner lock, the VMO page table
//! (XArray), page locks, and the ext2 block-group layers — and checks the
//! acquisition sequences against it.
//!
//! It checks the locking policy, not interleavings: the total-order argument is
//! static. The negative controls show what the model rejects.

#![cfg(test)]

/// Futex bucket locks, ordered among themselves by bucket index. They are
/// never nested with anything else: `futex_wait_bitset` drops the bucket before
/// it can fault in the futex word.
pub const BUCKET_BASE: usize = 0;
pub const BUCKET_COUNT: usize = 64;

/// `Vmar::inner` — write lock in `fork_from`/`mmap`/`munmap`, read lock while
/// resolving a page fault.
pub const VMAR: usize = BUCKET_BASE + BUCKET_COUNT;
/// `ext2::Inode::inner` — the inode's own `RwMutex`, held by file I/O.
pub const INODE_INNER: usize = VMAR + 1;
/// The VMO page table (`vmo.pages`, an `XArray`). Taken briefly to look up or
/// install a page and released before the page lock.
pub const XARRAY: usize = INODE_INNER + 1;
/// A page lock (`LockedCachePage`). Taken one page at a time.
pub const PAGE: usize = XARRAY + 1;
/// `BlockPtrTree` — the ext2 inode's block mapping, taken by the page-cache
/// backend while the page is locked.
pub const BLOCK_PTR_TREE: usize = PAGE + 1;
/// `IndirectBlockManager`, nested under `BlockPtrTree`.
pub const INDIRECT: usize = BLOCK_PTR_TREE + 1;
/// `Ext2::super_block`.
pub const SUPER_BLOCK: usize = INDIRECT + 1;
/// `BlockGroup::metadata`.
pub const METADATA: usize = SUPER_BLOCK + 1;

/// One step of a thread's locking sequence.
#[derive(Clone, Copy, Debug)]
pub enum Step {
    Acquire(usize),
    Release(usize),
}

/// Runs a sequence and returns whether every acquisition was of a strictly
/// higher index than everything the thread still holds.
pub fn respects_total_order(sequence: &[Step]) -> bool {
    let mut held: Vec<usize> = Vec::new();
    for step in sequence {
        match *step {
            Step::Acquire(lock) => {
                if held.iter().any(|&h| h >= lock) {
                    return false;
                }
                held.push(lock);
            }
            Step::Release(lock) => {
                if let Some(pos) = held.iter().position(|&h| h == lock) {
                    held.remove(pos);
                } else {
                    return false;
                }
            }
        }
    }
    true
}

fn seq(locks: &[usize]) -> Vec<Step> {
    locks.iter().map(|&l| Step::Acquire(l)).collect()
}

/// `lock_bucket_pairs`: both buckets in index order; the same bucket once.
fn bucket_pair(key_1: usize, key_2: usize) -> Vec<usize> {
    if key_1 == key_2 {
        return vec![BUCKET_BASE + key_1];
    }
    let (low, high) = if key_1 < key_2 {
        (key_1, key_2)
    } else {
        (key_2, key_1)
    };
    vec![BUCKET_BASE + low, BUCKET_BASE + high]
}

#[test]
fn requeue_in_both_directions_keeps_the_total_order() {
    assert!(respects_total_order(&seq(&bucket_pair(3, 17))));
    assert!(respects_total_order(&seq(&bucket_pair(17, 3))));
}

#[test]
fn every_bucket_pair_is_ordered() {
    for a in 0..BUCKET_COUNT {
        for b in 0..BUCKET_COUNT {
            let pair = bucket_pair(a, b);
            if a == b {
                assert_eq!(pair.len(), 1, "a bucket must not lock itself twice");
            } else {
                assert!(pair[0] < pair[1], "bucket pair ({a}, {b}) is not ordered");
            }
            assert!(respects_total_order(&seq(&pair)));
        }
    }
}

#[test]
fn a_private_file_page_fault_follows_the_order() {
    // `handle_single_page_fault`: VMAR read lock, page-table cursor, then the
    // page cache: XArray lookup, page lock, backend block lookup.
    let fault = [
        Step::Acquire(VMAR),
        Step::Acquire(PAGE), // the page-table cursor lock is below the VMAR lock
        Step::Release(PAGE),
        Step::Acquire(XARRAY),
        Step::Release(XARRAY),
        Step::Acquire(PAGE),
        Step::Acquire(BLOCK_PTR_TREE),
    ];
    assert!(respects_total_order(&fault));
}

#[test]
fn buffered_io_and_truncate_follow_the_order() {
    // `Inode::write_at`: inner write lock, then the page cache, then the block
    // mapping and the filesystem metadata layers.
    let write = [
        Step::Acquire(INODE_INNER),
        Step::Acquire(PAGE),
        Step::Acquire(BLOCK_PTR_TREE),
        Step::Acquire(INDIRECT),
        Step::Acquire(SUPER_BLOCK),
        Step::Acquire(METADATA),
    ];
    assert!(respects_total_order(&write));

    // Truncate releases the page lock before taking the block manager.
    let truncate = [
        Step::Acquire(INODE_INNER),
        Step::Acquire(PAGE),
        Step::Release(PAGE),
        Step::Acquire(BLOCK_PTR_TREE),
        Step::Acquire(SUPER_BLOCK),
        Step::Acquire(METADATA),
    ];
    assert!(respects_total_order(&truncate));
}

#[test]
fn writeback_waits_for_the_page_before_taking_the_page_table() {
    // `wait_for_writeback_and_remove_pages`: page lock, release, then the
    // XArray lock to remove the page.
    let writeback = [
        Step::Acquire(PAGE),
        Step::Release(PAGE),
        Step::Acquire(XARRAY),
        Step::Release(XARRAY),
    ];
    assert!(respects_total_order(&writeback));
}

#[test]
fn a_futex_bucket_is_never_held_across_a_fault() {
    // `futex_wait_bitset` drops the bucket before `handle_page_fault`.
    let wait = [
        Step::Acquire(BUCKET_BASE + 7),
        Step::Release(BUCKET_BASE + 7),
        Step::Acquire(VMAR),
        Step::Acquire(PAGE),
    ];
    assert!(respects_total_order(&wait));
}

#[test]
fn the_model_rejects_inversions() {
    // Each of these is a deadlock-capable order the kernel must never have.
    let inversions: &[&[usize]] = &[
        &[PAGE, XARRAY],          // page lock held while taking the page table
        &[BLOCK_PTR_TREE, PAGE],  // block mapping held while locking a page
        &[PAGE, INODE_INNER],     // page lock held while taking the inode lock
        &[PAGE, VMAR],            // page lock held while taking the VMAR lock
        &[VMAR, BUCKET_BASE],     // VMAR lock held while taking a futex bucket
        &[SUPER_BLOCK, BLOCK_PTR_TREE],
        &[METADATA, SUPER_BLOCK],
    ];
    for inversion in inversions {
        assert!(
            !respects_total_order(&seq(inversion)),
            "the model must reject the inversion {inversion:?}"
        );
    }
}

#[test]
fn unsorted_bucket_acquisition_is_rejected() {
    assert!(!respects_total_order(&seq(&[BUCKET_BASE + 17, BUCKET_BASE + 3])));
}
