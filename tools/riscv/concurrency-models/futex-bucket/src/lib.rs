// SPDX-License-Identifier: MPL-2.0

//! Loom model of the futex bucket protocol: wait, wake, and requeue.
//!
//! Real code: `kernel/src/process/posix_thread/futex.rs`.
//!
//! * `futex_wait_bitset` takes the bucket lock, reads the futex word, appends
//!   its item to the bucket, releases the lock, and sleeps on a `Waiter`.
//! * `futex_wake_bitset` takes the bucket lock, drains matching items, releases
//!   the lock, and wakes each item's waker.
//! * `futex_requeue` / `futex_cmp_requeue` take the source bucket, move items
//!   into the target bucket (and may wake some), so a waiter that was queued on
//!   the source is woken by a wake on the *target*.
//!
//! The property that matters: a waiter that appended its item before a wake
//! that matches it is never left asleep — including after a requeue moved the
//! item to another bucket. A missed wakeup here is a hang (this module already
//! produced one real crash: `futex_cmp_requeue01` on four CPUs).
//!
//! The model uses the same wake token as `waitqueue-waker`: a woken waiter
//! either consumes the token or is unparked, and a wake that arrives between
//! the check and the park is not lost.

#![cfg(test)]

use std::sync::atomic::{AtomicBool as StdAtomicBool, Ordering as StdOrdering};
use std::sync::Arc;

use loom::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use loom::sync::{Condvar, Mutex};
use loom::thread;

/// One queued waiter. `futex_wait_bitset` owns such an item while it sleeps.
struct Item {
    /// The wake token: a wake sets it, a waiter consumes it.
    woken: AtomicBool,
    /// Stands in for the scheduler's park queue: `notify_one` must not be lost
    /// between the waiter's check and its park.
    parked: Mutex<()>,
    unpark: Condvar,
}

impl Item {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            woken: AtomicBool::new(false),
            parked: Mutex::new(()),
            unpark: Condvar::new(),
        })
    }

    /// `Waker::wake_up`.
    fn wake(&self) {
        if self.woken.swap(true, Ordering::Release) {
            return;
        }
        let _guard = self.parked.lock().unwrap();
        self.unpark.notify_one();
    }

    /// `Waiter::pause`.
    fn wait(&self) {
        loop {
            if self.woken.swap(false, Ordering::Acquire) {
                return;
            }
            let guard = self.parked.lock().unwrap();
            if !self.woken.load(Ordering::Acquire) {
                let _guard = self.unpark.wait(guard).unwrap();
            }
        }
    }
}

/// A futex bucket: the queue of items waiting on one futex key.
struct Bucket {
    items: Mutex<Vec<Arc<Item>>>,
}

impl Bucket {
    fn new() -> Self {
        Self {
            items: Mutex::new(Vec::new()),
        }
    }

    /// `futex_wait_bitset`'s enqueue half. Returns the item to sleep on.
    fn enqueue(&self) -> Arc<Item> {
        let item = Item::new();
        self.items.lock().unwrap().push(item.clone());
        item
    }

    /// `futex_wake_bitset` with a wake count.
    fn wake(&self, count: usize) -> usize {
        let woken: Vec<_> = {
            let mut items = self.items.lock().unwrap();
            let n = count.min(items.len());
            items.drain(..n).collect()
        };
        let n = woken.len();
        for item in woken {
            item.wake();
        }
        n
    }

    /// `futex_requeue`: move up to `count` items to `target`.
    fn requeue(&self, target: &Bucket, count: usize) -> usize {
        let moved: Vec<_> = {
            let mut items = self.items.lock().unwrap();
            let n = count.min(items.len());
            items.drain(..n).collect()
        };
        let n = moved.len();
        target.items.lock().unwrap().extend(moved);
        n
    }
}

/// A waiter that never reports being woken would make `join` hang, which loom
/// reports as a deadlock — the model's way of failing.
///
/// The wake is ordered after the enqueue: FUTEX_WAKE only wakes waiters that
/// are already queued, so a wake before the enqueue is a legitimate no-op.
#[test]
fn a_wake_reaches_a_waiter_that_already_enqueued() {
    loom::model(|| {
        let bucket = Arc::new(Bucket::new());
        let (enqueued_tx, enqueued_rx) = loom::sync::mpsc::channel();

        let waiter = {
            let bucket = bucket.clone();
            thread::spawn(move || {
                let item = bucket.enqueue();
                enqueued_tx.send(()).unwrap();
                item.wait();
            })
        };
        let waker = {
            let bucket = bucket.clone();
            thread::spawn(move || {
                enqueued_rx.recv().unwrap();
                bucket.wake(1);
            })
        };

        waiter.join().unwrap();
        waker.join().unwrap();
    });
}

/// A waiter requeued from the source bucket must be woken by a wake on the
/// target bucket, even when the wake races with the waiter parking.
#[test]
fn a_requeued_waiter_is_woken_on_the_target_bucket() {
    loom::model(|| {
        let source = Arc::new(Bucket::new());
        let target = Arc::new(Bucket::new());
        let (enqueued_tx, enqueued_rx) = loom::sync::mpsc::channel();
        let (requeued_tx, requeued_rx) = loom::sync::mpsc::channel();

        let waiter = {
            let source = source.clone();
            thread::spawn(move || {
                let item = source.enqueue();
                enqueued_tx.send(()).unwrap();
                item.wait();
            })
        };
        let requeuer = {
            let source = source.clone();
            let target = target.clone();
            thread::spawn(move || {
                enqueued_rx.recv().unwrap();
                source.requeue(&target, 1);
                requeued_tx.send(()).unwrap();
            })
        };
        let waker = {
            let target = target.clone();
            thread::spawn(move || {
                requeued_rx.recv().unwrap();
                target.wake(1);
            })
        };

        waiter.join().unwrap();
        requeuer.join().unwrap();
        waker.join().unwrap();
    });
}

/// A wake on the source bucket after a requeue must not consume the waiter's
/// token; the target bucket's later wake still delivers.
#[test]
fn a_wake_on_the_source_bucket_leaves_the_requeued_waiter_to_the_target() {
    loom::model(|| {
        let source = Arc::new(Bucket::new());
        let target = Arc::new(Bucket::new());
        let (enqueued_tx, enqueued_rx) = loom::sync::mpsc::channel();

        let waiter = {
            let source = source.clone();
            thread::spawn(move || {
                let item = source.enqueue();
                enqueued_tx.send(()).unwrap();
                item.wait();
            })
        };
        let driver = {
            let source = source.clone();
            let target = target.clone();
            thread::spawn(move || {
                enqueued_rx.recv().unwrap();
                source.requeue(&target, 1);
                // The source bucket is empty now; this wake must find nothing.
                assert_eq!(source.wake(1), 0);
                assert_eq!(target.wake(1), 1);
            })
        };

        waiter.join().unwrap();
        driver.join().unwrap();
    });
}

/// Two waiters and two wakes: both must observe a wake (otherwise the join
/// deadlocks), whichever order the wakes run in.
#[test]
fn two_waiters_are_both_woken_by_two_wakes() {
    loom::model(|| {
        let bucket = Arc::new(Bucket::new());
        let (enqueued_tx, enqueued_rx) = loom::sync::mpsc::channel();

        let first = {
            let bucket = bucket.clone();
            let enqueued_tx = enqueued_tx.clone();
            thread::spawn(move || {
                let item = bucket.enqueue();
                enqueued_tx.send(()).unwrap();
                item.wait();
            })
        };
        let second = {
            let bucket = bucket.clone();
            thread::spawn(move || {
                let item = bucket.enqueue();
                enqueued_tx.send(()).unwrap();
                item.wait();
            })
        };
        let waker = {
            let bucket = bucket.clone();
            thread::spawn(move || {
                enqueued_rx.recv().unwrap();
                enqueued_rx.recv().unwrap();
                bucket.wake(1);
                bucket.wake(1);
            })
        };

        first.join().unwrap();
        second.join().unwrap();
        waker.join().unwrap();
    });
}
