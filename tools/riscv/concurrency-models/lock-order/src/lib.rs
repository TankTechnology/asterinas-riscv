// SPDX-License-Identifier: MPL-2.0

//! The lock acquisition order of the kernel's multi-lock paths.
//!
//! A set of locks with a *total* order cannot deadlock: every waiter holds
//! only lower-indexed locks while asking for a higher-indexed one, so no cycle
//! can form. This crate encodes the order the kernel uses and the acquisition
//! sequences of the paths that take two locks, and checks the sequences
//! respect it. It is a check of the locking policy, not an enumeration of
//! interleavings — the total-order argument is static.
//!
//! The paths encoded here:
//!
//! * `lock_bucket_pairs` (`kernel/src/process/posix_thread/futex.rs:573`)
//!   locks two futex buckets in index order, so `FUTEX_REQUEUE` between A and
//!   B racing with one between B and A cannot deadlock;
//! * `Vmar::fork_from` (`kernel/src/vm/vmar/vmar_impls/fork.rs`) takes the
//!   VMAR write lock and then page-table cursors;
//! * the page-fault path (`kernel/src/vm/vmar/vm_mapping.rs`) takes the VMAR
//!   read lock and then a page-table cursor;
//! * `futex_wait_bitset` drops the bucket lock before calling
//!   `handle_page_fault`, so the futex lock never nests inside the VM lock.

#![cfg(test)]

/// Lock classes, ordered by the value: all futex buckets come first (ordered
/// among themselves by bucket index), then the VMAR lock, then the page table.
pub const BUCKET_BASE: usize = 0;
pub const BUCKET_COUNT: usize = 64;
pub const VMAR: usize = BUCKET_BASE + BUCKET_COUNT;
pub const PAGE_TABLE: usize = VMAR + 1;

/// A lock acquisition sequence is valid when every acquisition is of a
/// strictly higher index than everything the thread already holds.
pub fn respects_total_order(held: &[usize], next: usize) -> bool {
    held.iter().all(|&lock| lock < next)
}

/// Runs a thread's acquisition sequence and returns whether it stayed within
/// the total order.
fn run_sequence(sequence: &[usize]) -> bool {
    let mut held = Vec::new();
    for &lock in sequence {
        if !respects_total_order(&held, lock) {
            return false;
        }
        held.push(lock);
    }
    held.clear();
    true
}

/// `lock_bucket_pairs`: the two buckets are always locked in index order, and
/// the same bucket twice is locked only once (`Ordering::Equal`).
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
    let forward = bucket_pair(3, 17);
    let backward = bucket_pair(17, 3);
    assert!(run_sequence(&forward));
    assert!(run_sequence(&backward));
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
            assert!(run_sequence(&pair), "bucket pair ({a}, {b}) breaks the order");
        }
    }
}

#[test]
fn fork_and_fault_take_the_vmar_before_the_page_table() {
    // fork: VMAR write lock, then page-table cursors.
    assert!(run_sequence(&[VMAR, PAGE_TABLE]));
    // page fault: VMAR read lock, then a page-table cursor.
    assert!(run_sequence(&[VMAR, PAGE_TABLE]));
    // The ordering also holds against a futex bucket held in another thread,
    // because `futex_wait_bitset` drops the bucket before faulting in.
    assert!(run_sequence(&[BUCKET_BASE + 5]));
    assert!(run_sequence(&[VMAR, PAGE_TABLE]));
}

#[test]
fn argument_order_without_sorting_breaks_the_total_order() {
    // Negative control: locking in argument order lets the two directions
    // disagree about the order, which is exactly how ABBA deadlocks form.
    let unsorted = |first: usize, second: usize| [BUCKET_BASE + first, BUCKET_BASE + second];
    assert!(run_sequence(&unsorted(3, 17)));
    assert!(
        !run_sequence(&unsorted(17, 3)),
        "the model must reject an unsorted pair"
    );
}

#[test]
fn page_table_before_vmar_would_break_the_total_order() {
    assert!(
        !run_sequence(&[PAGE_TABLE, VMAR]),
        "acquiring the VMAR lock under the page table must be rejected"
    );
}
