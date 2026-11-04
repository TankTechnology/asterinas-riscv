// SPDX-License-Identifier: MPL-2.0

//! Loom model of ostd's per-CPU run queue around task exit and wake.
//!
//! The real code lives in:
//!   * `ostd/src/task/scheduler/fifo_scheduler.rs` (`enqueue`,
//!     `dequeue_current`, `try_pick_next`)
//!   * `ostd/src/task/scheduler/mod.rs` (`exit_current`, `unpark_target`)
//!   * `ostd/src/task/processor.rs` (`switch_to_task`)
//!
//! The model keeps the same protocol, with loom atomics standing in for
//! `AtomicCpuId` and a loom mutex standing in for the run-queue spin lock.
//! Two CPUs take part: CPU 0 runs the task that is exiting, CPU 1 wakes it.
//!
//! The invariant is simple: once a task starts exiting, it must never be put
//! back into a run queue. A task that is put back is later picked and
//! switched to from its saved (already exited) context, which is exactly the
//! `unreachable!()` panic `exit_current` hit when LTP's
//! `futex_cmp_requeue01` ran on four CPUs.

#![cfg(test)]

use std::collections::VecDeque;
use std::sync::Arc;

use loom::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::atomic::AtomicBool as StdAtomicBool;
use loom::sync::Mutex;
use loom::thread;

const NONE: usize = usize::MAX;
const CPU0: usize = 0;
const CPU1: usize = 1;

#[derive(Default)]
struct RunQueue {
    queue: VecDeque<usize>,
    current: usize,
}

struct Shared {
    /// `AtomicCpuId`: which CPU owns the task (queued or running), or NONE.
    task_cpu: AtomicUsize,
    /// `Task::exit_pending`: set before the exiting task leaves its queue.
    task_exit_pending: AtomicBool,
    /// The task has switched away for the last time.
    task_dead: AtomicBool,
    rq0: Mutex<RunQueue>,
    rq1: Mutex<RunQueue>,
    /// Set when an interleaving breaks the invariant.
    violation: AtomicBool,
}

impl Shared {
    fn new() -> Self {
        Self {
            task_cpu: AtomicUsize::new(CPU0),
            task_exit_pending: AtomicBool::new(false),
            task_dead: AtomicBool::new(false),
            rq0: Mutex::new(RunQueue::default()),
            rq1: Mutex::new(RunQueue::default()),
            violation: AtomicBool::new(false),
        }
    }

    fn rq(&self, cpu: usize) -> &Mutex<RunQueue> {
        if cpu == CPU0 { &self.rq0 } else { &self.rq1 }
    }

    /// `processor::exit_current` + `dequeue_current`: the task stops running.
    fn exit(&self, _task: usize, cpu: usize, fixed: bool) {
        if fixed {
            // Release pairs with the Acquire check under the run-queue lock.
            self.task_exit_pending.store(true, Ordering::Release);
        }
        let mut rq = self.rq(cpu).lock().unwrap();
        rq.current = NONE;
        // `dequeue_current`: the task no longer owns a CPU.
        self.task_cpu.store(NONE, Ordering::Release);
        drop(rq);
        // The context switch away is the last thing the task does.
        self.task_dead.store(true, Ordering::Release);
    }

    /// `unpark_target` -> `FifoScheduler::enqueue(_, Wake)`.
    fn wake(&self, task: usize, selected_cpu: usize, fixed: bool) {
        // `set_if_is_none(selected_cpu)`: fails when the task already has a CPU.
        let (still_in_rq, target_cpu) =
            match self
                .task_cpu
                .compare_exchange(NONE, selected_cpu, Ordering::AcqRel, Ordering::Acquire)
            {
                Ok(_) => (false, selected_cpu),
                Err(existing) => (true, existing),
            };

        let mut rq = self.rq(target_cpu).lock().unwrap();

        // The fix under test: an exiting task must not go back into a queue.
        if fixed && self.task_exit_pending.load(Ordering::Acquire) {
            return;
        }
        if still_in_rq
            && self
                .task_cpu
                .compare_exchange(NONE, target_cpu, Ordering::AcqRel, Ordering::Acquire)
                .is_err()
        {
            return;
        }
        rq.queue.push_back(task);
        if self.task_dead.load(Ordering::Acquire) {
            self.violation.store(true, Ordering::Release);
        }
    }

    /// `try_pick_next`: another CPU takes the next runnable task.
    fn pick(&self, cpu: usize) {
        let mut rq = self.rq(cpu).lock().unwrap();
        if let Some(next) = rq.queue.pop_front() {
            rq.current = next;
            if self.task_dead.load(Ordering::Acquire) {
                self.violation.store(true, Ordering::Release);
            }
        }
    }
}

fn model(fixed: bool) -> bool {
    let shared = Arc::new(Shared::new());
    let exiter = {
        let shared = shared.clone();
        thread::spawn(move || shared.exit(0, CPU0, fixed))
    };
    let waker = {
        let shared = shared.clone();
        thread::spawn(move || shared.wake(0, CPU1, fixed))
    };
    let picker = {
        let shared = shared.clone();
        thread::spawn(move || {
            shared.pick(CPU0);
            shared.pick(CPU1);
        })
    };
    exiter.join().unwrap();
    waker.join().unwrap();
    picker.join().unwrap();
    shared.violation.load(Ordering::Acquire)
}

#[test]
fn buggy_protocol_re_enqueues_an_exiting_task() {
    // Loom must find the interleaving: the wake fails the first assignment
    // check, the exiting CPU dequeues and clears the assignment, then the wake
    // pushes the dead task into the queue.
    static FOUND: StdAtomicBool = StdAtomicBool::new(false);
    loom::model(|| {
        if model(false) {
            FOUND.store(true, std::sync::atomic::Ordering::Relaxed);
        }
    });
    assert!(
        FOUND.load(std::sync::atomic::Ordering::Relaxed),
        "loom did not reach the re-enqueue interleaving"
    );
}

#[test]
fn fixed_protocol_never_re_enqueues_an_exiting_task() {
    loom::model(|| {
        assert!(
            !model(true),
            "an exiting task was re-enqueued despite the exit_pending check"
        );
    });
}
