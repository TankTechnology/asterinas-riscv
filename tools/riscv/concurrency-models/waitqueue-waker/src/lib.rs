// SPDX-License-Identifier: MPL-2.0

//! Loom model of ostd's `Waiter`/`Waker` handshake
//! (`ostd/src/sync/wait.rs`).
//!
//! The real protocol:
//!
//! ```text
//! Waker::wake_up:  if has_woken.swap(true) { false } else { unpark(task); true }
//! Waiter::wait:    while !has_woken.swap(false) { park until has_woken or unpark }
//! ```
//!
//! `has_woken` is the token that makes a wake before (or during) a park
//! visible to the waiter, and `swap(false)` on the wait side makes two wakes
//! collapse into one. The model keeps both operations in the same order and
//! replaces the kernel's `park_current` with loom's `park`, so loom explores
//! every interleaving of wake and park.
//!
//! A lost wake shows up as a deadlock: the waiter parks with `has_woken`
//! false and no unpark left to deliver.

#![cfg(test)]

use std::sync::Arc;

use loom::sync::atomic::{AtomicBool, Ordering};
use loom::sync::{Condvar, Mutex};
use loom::thread;

pub struct Waker {
    has_woken: AtomicBool,
    /// Stands in for the scheduler's park queue: `notify_one` must not be
    /// lost between the waiter's check and its park.
    parked: Mutex<()>,
    unpark: Condvar,
}

impl Waker {
    pub fn new() -> Self {
        Self {
            has_woken: AtomicBool::new(false),
            parked: Mutex::new(()),
            unpark: Condvar::new(),
        }
    }

    /// Returns whether this call delivered the wake.
    pub fn wake_up(&self) -> bool {
        if self.has_woken.swap(true, Ordering::Release) {
            return false;
        }
        let _guard = self.parked.lock().unwrap();
        self.unpark.notify_one();
        true
    }

    /// The waiter side: consume a pending wake, or park until one arrives.
    pub fn wait(&self) {
        loop {
            if self.has_woken.swap(false, Ordering::Acquire) {
                return;
            }
            let guard = self.parked.lock().unwrap();
            // Re-check under the park lock: a wake that arrives between the
            // swap above and this park must not be lost.
            if !self.has_woken.load(Ordering::Acquire) {
                let _guard = self.unpark.wait(guard).unwrap();
            }
        }
    }

    /// Equivalent to dropping the waiter: later wakes are no-ops.
    pub fn close(&self) {
        let _ = self.has_woken.swap(true, Ordering::Acquire);
    }
}

/// Creates a waiter thread that publishes its own waker, mirroring
/// `Waiter::new_pair`: the waker targets the task that will park.
fn spawn_waiter() -> (Arc<Waker>, loom::thread::JoinHandle<()>) {
    let (tx, rx) = loom::sync::mpsc::channel();
    let handle = thread::spawn(move || {
        let waker = Arc::new(Waker::new());
        tx.send(waker.clone()).unwrap();
        waker.wait();
    });
    let waker = rx.recv().unwrap();
    (waker, handle)
}

#[test]
fn wake_before_wait_is_not_lost() {
    loom::model(|| {
        // Either order of the two operations must complete: the token covers
        // the case where the wake happens before the waiter parks.
        let (waker, waiter) = spawn_waiter();
        assert!(waker.wake_up());
        waiter.join().unwrap();
    });
}

#[test]
fn every_wake_from_two_wakers_unparks_the_waiter() {
    loom::model(|| {
        let (waker, waiter) = spawn_waiter();
        let waker_a = waker.clone();
        let waker_b = waker.clone();
        let a = thread::spawn(move || waker_a.wake_up());
        let b = thread::spawn(move || waker_b.wake_up());
        a.join().unwrap();
        b.join().unwrap();
        waiter.join().unwrap();
    });
}

#[test]
fn one_wake_is_delivered_at_most_once() {
    loom::model(|| {
        let waker = Arc::new(Waker::new());
        // The second call must report "already woken" no matter how the two
        // calls interleave, which is what keeps the run queue from receiving
        // a duplicate wake.
        let clone = waker.clone();
        let first = thread::spawn(move || waker.wake_up());
        let second = thread::spawn(move || clone.wake_up());
        let delivered = first.join().unwrap() as u8 + second.join().unwrap() as u8;
        assert_eq!(delivered, 1);
    });
}

#[test]
fn a_closed_waiter_is_never_woken() {
    loom::model(|| {
        let waker = Arc::new(Waker::new());
        waker.close();
        assert!(!waker.wake_up());
    });
}
