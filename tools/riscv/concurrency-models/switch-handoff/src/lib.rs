// SPDX-License-Identifier: MPL-2.0

//! Loom model of the scheduler's task handoff around `switched_to_cpu`.
//!
//! Real code: `ostd/src/task/processor.rs`.
//!
//! * `before_switching_to(task)` spins until it can CAS the task's
//!   `switched_to_cpu` flag from `false` to `true`. A failed CAS is reported
//!   once with "Switching to a task already running in the foreground".
//! * `after_switching_to()` runs at the top of the task that was switched to
//!   and clears the *previous* task's flag, i.e. a task becomes switchable
//!   again only after the CPU that left it has completed its context switch.
//!
//! The property that matters is mutual exclusion: two CPUs must never
//! execute one task at the same time, even when a task is woken on CPU 1 while
//! CPU 0 is still switching away from it. The warning above is the handoff
//! wait; the model checks that the wait is what guarantees the exclusion.
//!
//! `buggy == true` drops the flag check, which is the negative control: loom
//! must then find the interleaving where both CPUs run the task.

#![cfg(test)]

use std::sync::atomic::{AtomicBool as StdAtomicBool, Ordering as StdOrdering};
use std::sync::Arc;

use loom::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use loom::thread;

const NONE: usize = usize::MAX;

struct Shared {
    /// The task's `switched_to_cpu` flag.
    switched_to_cpu: AtomicBool,
    /// Which CPU is executing the task, if any.
    running_on: AtomicUsize,
    /// How often `before_switching_to` had to wait (the logged handoff).
    handoffs: AtomicUsize,
    /// Two CPUs executed the task at once.
    violation: StdAtomicBool,
}

impl Shared {
    fn new() -> Self {
        Self {
            switched_to_cpu: AtomicBool::new(false),
            running_on: AtomicUsize::new(NONE),
            handoffs: AtomicUsize::new(0),
            violation: StdAtomicBool::new(false),
        }
    }

    /// `before_switching_to`: become the task's CPU.
    fn switch_to(&self, cpu: usize, buggy: bool) {
        if buggy {
            // Negative control: no handoff check at all.
            if self.running_on.swap(cpu, Ordering::AcqRel) != NONE {
                self.violation.store(true, StdOrdering::Release);
            }
            return;
        }

        let mut waited = false;
        while self
            .switched_to_cpu
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Relaxed)
            .is_err()
        {
            if !waited {
                self.handoffs.fetch_add(1, Ordering::Relaxed);
                waited = true;
            }
            thread::yield_now();
        }

        // The flag was ours; no other CPU can be executing the task.
        if self.running_on.swap(cpu, Ordering::AcqRel) != NONE {
            self.violation.store(true, StdOrdering::Release);
        }
    }

    /// The CPU stops executing the task; the next task's `after_switching_to`
    /// releases the flag, so the task becomes switchable again only now.
    fn switch_away(&self, cpu: usize) {
        self.running_on.compare_exchange(cpu, NONE, Ordering::AcqRel, Ordering::Relaxed).ok();
        self.switched_to_cpu.store(false, Ordering::Release);
    }
}

fn model(buggy: bool) -> (bool, usize) {
    let shared = Arc::new(Shared::new());

    let mk = |cpu: usize| {
        let shared = shared.clone();
        thread::spawn(move || {
            shared.switch_to(cpu, buggy);
            shared.switch_away(cpu);
        })
    };

    let cpu0 = mk(0);
    let cpu1 = mk(1);
    cpu0.join().unwrap();
    cpu1.join().unwrap();

    (
        shared.violation.load(StdOrdering::Acquire),
        shared.handoffs.load(Ordering::Relaxed),
    )
}

#[test]
fn the_handoff_keeps_two_cpus_from_running_one_task() {
    loom::model(|| {
        let (violation, _) = model(false);
        assert!(!violation, "two CPUs executed the task at the same time");
    });
}

#[test]
fn the_handoff_wait_is_observable() {
    // The warning exists because a CPU can be asked to switch to a task whose
    // flag is still held; the protocol must wait, not proceed.
    static FOUND: StdAtomicBool = StdAtomicBool::new(false);
    loom::model(|| {
        let (_, handoffs) = model(false);
        if handoffs > 0 {
            FOUND.store(true, StdOrdering::Relaxed);
        }
    });
    assert!(
        FOUND.load(StdOrdering::Relaxed),
        "loom never produced the handoff wait"
    );
}

#[test]
fn without_the_flag_two_cpus_can_run_one_task() {
    // Negative control: the model must catch the bug it exists for.
    static FOUND: StdAtomicBool = StdAtomicBool::new(false);
    loom::model(|| {
        let (violation, _) = model(true);
        if violation {
            FOUND.store(true, StdOrdering::Relaxed);
        }
    });
    assert!(
        FOUND.load(StdOrdering::Relaxed),
        "loom did not reach the double-execution interleaving"
    );
}
