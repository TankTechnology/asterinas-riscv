// SPDX-License-Identifier: MPL-2.0

//! Per-file queues for atomic hardware work and DRM completion events.

use ostd::sync::WaitQueue;

use super::{
    CRTC_ID, DRM_EVENT_FLIP_COMPLETE, DrmEventVblank, MAX_DRM_EVENTS, MAX_PENDING_ATOMIC_COMMITS,
    vblank::VblankSnapshot,
};
use crate::{
    events::IoEvents,
    prelude::*,
    process::signal::{PollHandle, Pollee},
    thread::kernel_thread::ThreadOptions,
};

type AtomicCommitWork = Box<dyn FnOnce() + Send>;
type VblankCompletionWork = Box<dyn FnOnce() + Send>;

struct AtomicCommitQueueState {
    work_items: VecDeque<AtomicCommitWork>,
    is_worker_running: bool,
    reserved_slots: usize,
}

/// Serializes nonblocking atomic hardware updates in submission order.
pub(super) struct AtomicCommitQueue {
    state: SpinLock<AtomicCommitQueueState>,
    waiters: WaitQueue,
}

impl AtomicCommitQueue {
    pub(super) fn new() -> Self {
        Self {
            state: SpinLock::new(AtomicCommitQueueState {
                work_items: VecDeque::new(),
                is_worker_running: false,
                reserved_slots: 0,
            }),
            waiters: WaitQueue::new(),
        }
    }

    pub(super) fn ensure_idle(&self) -> Result<()> {
        let state = self.state.lock();
        if state.is_worker_running || state.reserved_slots != 0 {
            return_errno_with_message!(Errno::EBUSY, "an atomic commit is still pending");
        }
        Ok(())
    }

    pub(super) fn reserve(self: &Arc<Self>) -> Result<AtomicCommitReservation> {
        let mut state = self.state.lock();
        let pending_commits =
            state.work_items.len() + state.reserved_slots + usize::from(state.is_worker_running);
        if pending_commits >= MAX_PENDING_ATOMIC_COMMITS {
            return_errno_with_message!(Errno::EBUSY, "too many atomic commits are pending");
        }
        state.reserved_slots += 1;
        drop(state);
        Ok(AtomicCommitReservation {
            queue: self.clone(),
            is_submitted: false,
        })
    }

    fn submit_reserved(self: &Arc<Self>, work: AtomicCommitWork) {
        let should_spawn_worker = {
            let mut state = self.state.lock();
            debug_assert!(state.reserved_slots > 0);
            state.reserved_slots -= 1;
            state.work_items.push_back(work);
            if state.is_worker_running {
                false
            } else {
                state.is_worker_running = true;
                true
            }
        };
        if should_spawn_worker {
            let queue = self.clone();
            ThreadOptions::new(move || queue.run()).spawn();
        }
    }

    fn run(self: Arc<Self>) {
        let mut worker = AtomicCommitWorker {
            queue: self.clone(),
            has_finished: false,
        };
        loop {
            let work = {
                let mut state = self.state.lock();
                let Some(work) = state.work_items.pop_front() else {
                    state.is_worker_running = false;
                    worker.has_finished = true;
                    drop(state);
                    self.waiters.wake_all();
                    return;
                };
                work
            };
            work();
        }
    }

    pub(super) fn wait_until_idle(&self) {
        self.waiters.wait_until(|| {
            let state = self.state.lock();
            (!state.is_worker_running && state.reserved_slots == 0).then_some(())
        });
    }
}

/// Restores queue liveness if an atomic commit worker exits unexpectedly.
struct AtomicCommitWorker {
    queue: Arc<AtomicCommitQueue>,
    has_finished: bool,
}

impl Drop for AtomicCommitWorker {
    fn drop(&mut self) {
        if self.has_finished {
            return;
        }
        error!("DRM atomic commit worker exited unexpectedly");
        let mut state = self.queue.state.lock();
        state.work_items.clear();
        state.is_worker_running = false;
        let is_idle = state.reserved_slots == 0;
        drop(state);
        if is_idle {
            self.queue.waiters.wake_all();
        }
    }
}

/// Reserves queue capacity before software KMS state is published.
pub(super) struct AtomicCommitReservation {
    queue: Arc<AtomicCommitQueue>,
    is_submitted: bool,
}

impl AtomicCommitReservation {
    pub(super) fn submit(mut self, work: AtomicCommitWork) {
        self.queue.submit_reserved(work);
        self.is_submitted = true;
    }
}

impl Drop for AtomicCommitReservation {
    fn drop(&mut self) {
        if self.is_submitted {
            return;
        }
        let mut state = self.queue.state.lock();
        debug_assert!(state.reserved_slots > 0);
        state.reserved_slots -= 1;
        let is_idle = !state.is_worker_running && state.reserved_slots == 0;
        drop(state);
        if is_idle {
            self.queue.waiters.wake_all();
        }
    }
}

struct VblankCompletionQueueState {
    work_items: VecDeque<VblankCompletionWork>,
    is_worker_running: bool,
}

/// Runs delayed legacy KMS completions without allocating one thread per flip.
pub(super) struct VblankCompletionQueue {
    state: SpinLock<VblankCompletionQueueState>,
    waiters: WaitQueue,
}

impl VblankCompletionQueue {
    pub(super) fn new() -> Self {
        Self {
            state: SpinLock::new(VblankCompletionQueueState {
                work_items: VecDeque::new(),
                is_worker_running: false,
            }),
            waiters: WaitQueue::new(),
        }
    }

    pub(super) fn submit(self: &Arc<Self>, work: VblankCompletionWork) {
        let should_spawn_worker = {
            let mut state = self.state.lock();
            state.work_items.push_back(work);
            if state.is_worker_running {
                false
            } else {
                state.is_worker_running = true;
                true
            }
        };
        if should_spawn_worker {
            let queue = self.clone();
            ThreadOptions::new(move || queue.run()).spawn();
        }
    }

    fn run(self: Arc<Self>) {
        let mut worker = VblankCompletionWorker {
            queue: self.clone(),
            has_finished: false,
        };
        loop {
            let work = {
                let mut state = self.state.lock();
                let Some(work) = state.work_items.pop_front() else {
                    state.is_worker_running = false;
                    worker.has_finished = true;
                    drop(state);
                    self.waiters.wake_all();
                    return;
                };
                work
            };
            work();
        }
    }

    pub(super) fn wait_until_idle(&self) {
        self.waiters.wait_until(|| {
            let state = self.state.lock();
            (!state.is_worker_running).then_some(())
        });
    }
}

struct VblankCompletionWorker {
    queue: Arc<VblankCompletionQueue>,
    has_finished: bool,
}

impl Drop for VblankCompletionWorker {
    fn drop(&mut self) {
        if self.has_finished {
            return;
        }
        error!("DRM vblank completion worker exited unexpectedly");
        let mut state = self.queue.state.lock();
        state.work_items.clear();
        state.is_worker_running = false;
        drop(state);
        self.queue.waiters.wake_all();
    }
}

struct DrmEventQueueState {
    events: VecDeque<DrmEventVblank>,
    reserved_slots: usize,
}

/// Stores events and reservations shared with asynchronous commit workers.
pub(super) struct DrmEventQueue {
    state: SpinLock<DrmEventQueueState>,
    pollee: Pollee,
}

impl DrmEventQueue {
    pub(super) fn new() -> Self {
        Self {
            state: SpinLock::new(DrmEventQueueState {
                events: VecDeque::new(),
                reserved_slots: 0,
            }),
            pollee: Pollee::new(),
        }
    }

    pub(super) fn queue_flip_event(&self, vblank: VblankSnapshot, user_data: u64) -> Result<()> {
        let event = new_flip_event(vblank, user_data);
        let mut state = self.state.lock();
        if state.events.len() + state.reserved_slots >= MAX_DRM_EVENTS {
            return_errno_with_message!(Errno::EBUSY, "DRM event queue is full");
        }
        state.events.push_back(event);
        drop(state);
        self.pollee.notify(IoEvents::IN);
        Ok(())
    }

    pub(super) fn ensure_capacity(&self) -> Result<()> {
        let state = self.state.lock();
        if state.events.len() + state.reserved_slots >= MAX_DRM_EVENTS {
            return_errno_with_message!(Errno::EBUSY, "DRM event queue is full");
        }
        Ok(())
    }

    /// Reserves room before publishing a nonblocking atomic commit.
    pub(super) fn reserve(self: &Arc<Self>) -> Result<DrmEventReservation> {
        let mut state = self.state.lock();
        if state.events.len() + state.reserved_slots >= MAX_DRM_EVENTS {
            return_errno_with_message!(Errno::EBUSY, "DRM event queue is full");
        }
        state.reserved_slots += 1;
        drop(state);
        Ok(DrmEventReservation {
            queue: self.clone(),
            is_consumed: false,
        })
    }

    pub(super) fn pop(&self) -> Option<DrmEventVblank> {
        let mut state = self.state.lock();
        let event = state.events.pop_front()?;
        if state.events.is_empty() {
            self.pollee.invalidate();
        }
        Some(event)
    }

    pub(super) fn requeue_front(&self, event: DrmEventVblank) {
        self.state.lock().events.push_front(event);
        self.pollee.notify(IoEvents::IN);
    }

    pub(super) fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        self.pollee.poll_with(mask, poller, || {
            let mut events = IoEvents::OUT;
            if !self.state.lock().events.is_empty() {
                events |= IoEvents::IN;
            }
            events
        })
    }
}

fn new_flip_event(vblank: VblankSnapshot, user_data: u64) -> DrmEventVblank {
    DrmEventVblank {
        type_: DRM_EVENT_FLIP_COMPLETE,
        length: size_of::<DrmEventVblank>() as u32,
        user_data,
        tv_sec: vblank.timestamp.as_secs() as u32,
        tv_usec: vblank.timestamp.subsec_micros(),
        sequence: vblank.sequence as u32,
        crtc_id: CRTC_ID,
    }
}

/// Capacity reserved for one asynchronous page-flip completion event.
pub(super) struct DrmEventReservation {
    queue: Arc<DrmEventQueue>,
    is_consumed: bool,
}

impl DrmEventReservation {
    pub(super) fn queue(mut self, vblank: VblankSnapshot, user_data: u64) {
        let event = new_flip_event(vblank, user_data);
        let mut state = self.queue.state.lock();
        debug_assert!(state.reserved_slots > 0);
        state.reserved_slots -= 1;
        state.events.push_back(event);
        self.is_consumed = true;
        drop(state);
        self.queue.pollee.notify(IoEvents::IN);
    }
}

impl Drop for DrmEventReservation {
    fn drop(&mut self) {
        if self.is_consumed {
            return;
        }
        let mut state = self.queue.state.lock();
        debug_assert!(state.reserved_slots > 0);
        state.reserved_slots -= 1;
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn atomic_commit_queue_preserves_submission_order() {
        let queue = Arc::new(AtomicCommitQueue::new());
        let completed: Arc<SpinLock<Vec<u32>>> = Arc::new(SpinLock::new(Vec::new()));
        for sequence in 0..3 {
            let completed = completed.clone();
            queue
                .reserve()
                .unwrap()
                .submit(Box::new(move || completed.lock().push(sequence)));
        }

        queue.wait_until_idle();
        assert_eq!(*completed.lock(), vec![0, 1, 2]);
        assert!(queue.ensure_idle().is_ok());
    }

    #[ktest]
    fn reserved_atomic_commit_keeps_queue_busy() {
        let queue = Arc::new(AtomicCommitQueue::new());
        let reservation = queue.reserve().unwrap();

        assert!(queue.ensure_idle().is_err());
        drop(reservation);
        assert!(queue.ensure_idle().is_ok());
    }

    #[ktest]
    fn vblank_completion_queue_preserves_submission_order() {
        let queue = Arc::new(VblankCompletionQueue::new());
        let completed: Arc<SpinLock<Vec<u32>>> = Arc::new(SpinLock::new(Vec::new()));
        for sequence in 0..3 {
            let completed = completed.clone();
            queue.submit(Box::new(move || completed.lock().push(sequence)));
        }

        queue.wait_until_idle();
        assert_eq!(*completed.lock(), vec![0, 1, 2]);
    }

    #[ktest]
    fn flip_event_uses_supplied_vblank_snapshot() {
        let clock = super::super::vblank::VblankClock::new();
        let snapshot = clock.snapshot();
        let event = new_flip_event(snapshot, 42);

        assert_eq!(event.user_data, 42);
        assert_eq!(event.sequence, snapshot.sequence as u32);
        assert_eq!(event.tv_sec, snapshot.timestamp.as_secs() as u32);
        assert_eq!(event.tv_usec, snapshot.timestamp.subsec_micros());
    }

    #[ktest]
    fn dropped_event_reservation_releases_capacity() {
        let queue = Arc::new(DrmEventQueue::new());
        queue.state.lock().reserved_slots = MAX_DRM_EVENTS - 1;

        let last_slot = queue.reserve().unwrap();
        assert!(queue.reserve().is_err());
        drop(last_slot);
        assert!(queue.reserve().is_ok());
    }
}
