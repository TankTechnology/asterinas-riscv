// SPDX-License-Identifier: MPL-2.0

//! Loom model of `Pollee::poll_with` / `Pollee::notify`
//! (`kernel/src/process/signal/poll.rs`).
//!
//! Two properties are modeled:
//!
//! 1. **Mask merge** (`register_poller`). `poll(2)` accepts duplicate entries
//!    for one descriptor and they may carry different masks (OpenSSH polls a
//!    connection with `POLLIN` and then with an empty mask that only asks for
//!    `ERR`/`HUP`). `SyncSubject` keeps one filter per observer, so a second
//!    registration used to *replace* the first one. Before `66d0290f7` that
//!    dropped the `IN` wakeup and OpenSSH slept forever on a pty that had data.
//!
//! 2. **Ordering protocol**. `poll_with` registers the poller, then reads the
//!    cached state, then claims the state with a `swap`, then checks the
//!    object. `notify` publishes the object's readiness, stores `INV_STATE`
//!    with `Release`, and only then takes the subject lock to wake observers.
//!    That order is what makes "either the events are returned or the poller
//!    is notified" hold when the two threads race: if `notify` took the
//!    subject lock after the registration, the wakeup is delivered; if it took
//!    it before, the registration's lock acquisition orders the poller's state
//!    read after the invalidation, so the poller re-checks and finds the data.
//!
//! `Task` in `PolleeInner::state` marks "someone is checking"; the model has a
//! single poller, so one constant stands in for the task pointer. Cached
//! events are modeled as their bit values, `INV_STATE` as `-1`.

#![cfg(test)]

use std::sync::atomic::{AtomicBool as StdAtomicBool, Ordering as StdOrdering};
use std::sync::Arc;

use loom::sync::atomic::{AtomicIsize, AtomicUsize, Ordering};
use loom::sync::Mutex;
use loom::thread;

const ERR: usize = 1 << 3;
const HUP: usize = 1 << 4;
const IN: usize = 1 << 6;
const ALWAYS_POLL: usize = ERR | HUP;
const NO_EVENTS: usize = 0;

const INV_STATE: isize = -1;
/// Stands in for `Task::current()`'s kernel pointer.
const TASK: isize = -2;

struct SubjectState {
    /// The single filter the subject keeps for this observer.
    filter: usize,
    /// `notify_observers` matched an event against the filter.
    woken: bool,
}

struct Pollee {
    /// `PolleeInner::state`: `INV_STATE`, `TASK`, or cached event bits.
    state: AtomicIsize,
    /// The object's own readiness, i.e. what `check()` reads.
    ready: AtomicUsize,
    /// `SyncSubject`'s lock.
    subject: Mutex<SubjectState>,
}

impl Pollee {
    fn new() -> Self {
        Self {
            state: AtomicIsize::new(INV_STATE),
            ready: AtomicUsize::new(0),
            subject: Mutex::new(SubjectState {
                filter: 0,
                woken: false,
            }),
        }
    }

    /// `register_poller`: the mask is merged per pollee when fixed; otherwise
    /// it is stored as the observer's only filter, as before `66d0290f7`.
    fn register_poller(&self, mask: usize, fixed: bool) {
        let mut subject = self.subject.lock().unwrap();
        if fixed {
            subject.filter |= mask;
        } else {
            subject.filter = mask;
        }
    }

    fn check(&self) -> usize {
        self.ready.load(Ordering::Acquire)
    }

    /// Returns the events this entry reports.
    fn poll_with(&self, mask: usize, fixed: bool) -> usize {
        let mask = mask | ALWAYS_POLL;

        self.register_poller(mask, fixed);

        // Return the cached events, if any.
        let state = self.state.load(Ordering::Acquire);
        if state >= 0 {
            return (state as usize) & mask;
        }
        // Someone else is checking; let them finish and check ourselves.
        if state != INV_STATE {
            return self.check() & mask;
        }

        // Claim the state, then check. The `Acquire` on the swap is what makes
        // a concurrent invalidation visible to `check()`.
        let _ = self.state.swap(TASK, Ordering::Acquire);
        let events = self.check();
        // Cache only if no invalidation happened in between.
        let _ = self.state.compare_exchange(
            TASK,
            events as isize,
            Ordering::Release,
            Ordering::Relaxed,
        );

        events & mask
    }

    /// `notify`: publish, invalidate, then wake the observers.
    fn notify(&self, events: usize) {
        self.ready.fetch_or(events, Ordering::AcqRel);
        // Release: a poller that sees this must see the readiness above.
        self.state.store(INV_STATE, Ordering::Release);
        let mut subject = self.subject.lock().unwrap();
        if subject.filter & events != 0 {
            subject.woken = true;
        }
    }
}

struct Outcome {
    /// The poller found nothing on any entry and went to sleep.
    slept: bool,
    /// Some `notify_observers` matched the observer's filter.
    woken: bool,
    /// The object ended up with `IN` ready.
    ready: bool,
    /// An interleaving that breaks "returned or notified".
    violation: bool,
}

fn model(fixed: bool) -> Outcome {
    let pollee = Arc::new(Pollee::new());
    // Duplicate poll entries for the same object, as OpenSSH issues for a pty.
    let masks = [IN, NO_EVENTS];

    let poller = {
        let pollee = pollee.clone();
        thread::spawn(move || {
            let mut any = 0;
            for &mask in &masks {
                any |= pollee.poll_with(mask, fixed);
            }
            any == 0
        })
    };
    let producer = {
        let pollee = pollee.clone();
        thread::spawn(move || pollee.notify(IN))
    };

    let slept = poller.join().unwrap();
    producer.join().unwrap();

    let woken = pollee.subject.lock().unwrap().woken;
    let ready = pollee.ready.load(Ordering::Acquire) & IN != 0;
    let violation = slept && ready && !woken;

    Outcome {
        slept,
        woken,
        ready,
        violation,
    }
}

#[test]
fn buggy_protocol_drops_a_duplicate_fd_wakeup() {
    static FOUND: StdAtomicBool = StdAtomicBool::new(false);
    loom::model(|| {
        if model(false).violation {
            FOUND.store(true, StdOrdering::Relaxed);
        }
    });
    assert!(
        FOUND.load(StdOrdering::Relaxed),
        "loom did not reach the lost-wakeup interleaving"
    );
}

#[test]
fn fixed_protocol_keeps_every_duplicate_fd_interest() {
    loom::model(|| {
        let outcome = model(true);
        assert!(
            !outcome.violation,
            "a duplicate-fd registration was dropped despite the mask union \
             (slept={}, ready={}, woken={})",
            outcome.slept,
            outcome.ready,
            outcome.woken
        );
    });
}

/// The same protocol with the producer running first: readiness published and
/// invalidated before the poller ever registers. The registration's lock
/// acquisition must order the poller's state read after the invalidation, so
/// its check finds the data even though no wakeup could be delivered.
#[test]
fn readiness_published_before_registration_is_seen_by_the_check() {
    loom::model(|| {
        let pollee = Arc::new(Pollee::new());
        let producer = {
            let pollee = pollee.clone();
            thread::spawn(move || pollee.notify(IN))
        };
        let poller = {
            let pollee = pollee.clone();
            thread::spawn(move || pollee.poll_with(IN, true))
        };
        producer.join().unwrap();
        let reported = poller.join().unwrap();
        let woken = pollee.subject.lock().unwrap().woken;
        assert!(
            reported & IN != 0 || woken,
            "readiness that predates registration was neither reported nor notified \
             (reported={:#x}, woken={})",
            reported,
            woken
        );
    });
}

/// `ALWAYS_POLL` is structural: `ERR`/`HUP` are reported even when the caller
/// asked for neither, which is why every filter includes them.
#[test]
fn always_poll_events_are_added_to_every_mask() {
    loom::model(|| {
        let pollee = Arc::new(Pollee::new());
        pollee.ready.store(ERR | HUP, Ordering::Release);
        let reported = pollee.poll_with(NO_EVENTS, true);
        assert_eq!(reported, ERR | HUP);
        assert_ne!(pollee.subject.lock().unwrap().filter & ALWAYS_POLL, 0);
    });
}
