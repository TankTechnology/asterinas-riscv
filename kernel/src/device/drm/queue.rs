// SPDX-License-Identifier: MPL-2.0

//! Per-file queues for atomic hardware work and DRM completion events.

use ostd::sync::WaitQueue;

use super::{
    CRTC_ID, DRM_EVENT_CRTC_SEQUENCE, DRM_EVENT_FLIP_COMPLETE, DRM_EVENT_VBLANK,
    DrmEventCrtcSequence, DrmEventVblank, GpuManager, MAX_DRM_EVENTS, MAX_PENDING_ATOMIC_COMMITS,
    vblank::VblankSnapshot,
};
use crate::{
    events::IoEvents,
    prelude::*,
    process::signal::{PollHandle, Pollee},
    thread::kernel_thread::ThreadOptions,
};

type AtomicCommitWork = Box<dyn FnOnce() + Send>;
type VblankCompletionWork = Box<dyn FnOnce(VblankSnapshot) + Send>;

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
    work_items: BTreeMap<(u64, u64), VblankCompletionWork>,
    is_worker_running: bool,
    next_submission_order: u64,
    schedule_generation: u64,
}

impl VblankCompletionQueueState {
    fn new() -> Self {
        Self {
            work_items: BTreeMap::new(),
            is_worker_running: false,
            next_submission_order: 0,
            schedule_generation: 0,
        }
    }

    fn schedule(&mut self, target_sequence: u64, work: VblankCompletionWork) {
        let submission_order = self.next_submission_order;
        self.next_submission_order = self.next_submission_order.wrapping_add(1);
        self.schedule_generation = self.schedule_generation.wrapping_add(1);
        let replaced = self
            .work_items
            .insert((target_sequence, submission_order), work);
        debug_assert!(replaced.is_none());
    }

    fn take_ready(&mut self, snapshot: VblankSnapshot) -> Vec<VblankCompletionWork> {
        let mut works = Vec::new();
        while let Some((&(target_sequence, _), _)) = self.work_items.first_key_value() {
            if snapshot.is_active() && target_sequence > snapshot.sequence {
                break;
            }
            let Some((_, work)) = self.work_items.pop_first() else {
                break;
            };
            works.push(work);
        }
        works
    }

    fn next_target(&self) -> Option<(u64, u64)> {
        self.work_items
            .first_key_value()
            .map(|(&(target_sequence, _), _)| (target_sequence, self.schedule_generation))
    }

    fn cancel_all(&mut self) {
        self.work_items.clear();
        self.schedule_generation = self.schedule_generation.wrapping_add(1);
    }
}

/// A per-file queue for delayed KMS and sequence completions.
pub(super) struct VblankCompletionQueue {
    gpu_manager: Arc<GpuManager>,
    state: SpinLock<VblankCompletionQueueState>,
    waiters: WaitQueue,
}

impl VblankCompletionQueue {
    pub(super) fn new(gpu_manager: Arc<GpuManager>) -> Self {
        Self {
            gpu_manager,
            state: SpinLock::new(VblankCompletionQueueState::new()),
            waiters: WaitQueue::new(),
        }
    }

    /// Schedules work by target sequence, preserving submission order on ties.
    pub(super) fn submit_at(self: &Arc<Self>, target_sequence: u64, work: VblankCompletionWork) {
        let should_spawn_worker = {
            let mut state = self.state.lock();
            state.schedule(target_sequence, work);
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
        self.gpu_manager.vblank_clock.notify_waiters();
    }

    fn run(self: Arc<Self>) {
        let mut worker = VblankCompletionWorker {
            queue: self.clone(),
            has_finished: false,
        };
        loop {
            let snapshot = self.gpu_manager.vblank_clock.snapshot();
            let (works, next_target) = {
                let mut state = self.state.lock();
                if state.work_items.is_empty() {
                    state.is_worker_running = false;
                    worker.has_finished = true;
                    drop(state);
                    self.waiters.wake_all();
                    return;
                }

                let works = state.take_ready(snapshot);
                let next_target = if works.is_empty() {
                    state.next_target()
                } else {
                    None
                };
                (works, next_target)
            };
            if let Some((target_sequence, schedule_generation)) = next_target {
                self.gpu_manager
                    .vblank_clock
                    .wait_until_or(target_sequence, || {
                        self.state.lock().schedule_generation != schedule_generation
                    });
                continue;
            }
            for work in works {
                work(snapshot);
            }
        }
    }

    pub(super) fn wait_until_idle(&self) {
        self.waiters.wait_until(|| {
            let state = self.state.lock();
            (!state.is_worker_running).then_some(())
        });
    }

    /// Drops file-owned pending events and joins the worker during close.
    pub(super) fn cancel_pending_and_wait(&self) {
        self.state.lock().cancel_all();
        self.gpu_manager.vblank_clock.notify_waiters();
        self.wait_until_idle();
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
    events: VecDeque<DrmEvent>,
    reserved_slots: usize,
}

#[derive(Clone, Copy, Debug)]
pub(super) enum DrmEvent {
    Vblank(DrmEventVblank),
    CrtcSequence(DrmEventCrtcSequence),
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

    pub(super) fn pop(&self) -> Option<DrmEvent> {
        let mut state = self.state.lock();
        let event = state.events.pop_front()?;
        if state.events.is_empty() {
            self.pollee.invalidate();
        }
        Some(event)
    }

    pub(super) fn requeue_front(&self, event: DrmEvent) {
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

fn new_vblank_event(event_type: u32, vblank: VblankSnapshot, user_data: u64) -> DrmEvent {
    DrmEvent::Vblank(DrmEventVblank {
        type_: event_type,
        length: size_of::<DrmEventVblank>() as u32,
        user_data,
        tv_sec: vblank.timestamp.as_secs() as u32,
        tv_usec: vblank.timestamp.subsec_micros(),
        sequence: vblank.sequence as u32,
        crtc_id: CRTC_ID,
    })
}

fn new_crtc_sequence_event(vblank: VblankSnapshot, user_data: u64) -> DrmEvent {
    let time_ns = i64::try_from(vblank.timestamp.as_nanos()).unwrap_or(i64::MAX);
    DrmEvent::CrtcSequence(DrmEventCrtcSequence {
        type_: DRM_EVENT_CRTC_SEQUENCE,
        length: size_of::<DrmEventCrtcSequence>() as u32,
        user_data,
        time_ns,
        sequence: vblank.sequence,
    })
}

/// Capacity reserved for one asynchronous page-flip completion event.
pub(super) struct DrmEventReservation {
    queue: Arc<DrmEventQueue>,
    is_consumed: bool,
}

impl DrmEventReservation {
    pub(super) fn queue_flip(self, vblank: VblankSnapshot, user_data: u64) {
        self.queue_event(new_vblank_event(DRM_EVENT_FLIP_COMPLETE, vblank, user_data));
    }

    pub(super) fn queue_vblank(self, vblank: VblankSnapshot, user_data: u64) {
        self.queue_event(new_vblank_event(DRM_EVENT_VBLANK, vblank, user_data));
    }

    pub(super) fn queue_crtc_sequence(self, vblank: VblankSnapshot, user_data: u64) {
        self.queue_event(new_crtc_sequence_event(vblank, user_data));
    }

    fn queue_event(mut self, event: DrmEvent) {
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
    fn vblank_completion_queue_orders_targets_and_ties() {
        let mut state = VblankCompletionQueueState::new();
        let completed: Arc<SpinLock<Vec<u32>>> = Arc::new(SpinLock::new(Vec::new()));
        for (target, id) in [(2, 0), (1, 1), (2, 2)] {
            let completed = completed.clone();
            state.schedule(target, Box::new(move |_| completed.lock().push(id)));
        }

        let snapshot = super::super::vblank::VblankClock::new().snapshot();
        for work in state.take_ready(snapshot) {
            work(snapshot);
        }
        assert_eq!(*completed.lock(), vec![1, 0, 2]);
    }

    #[ktest]
    fn vblank_completion_schedule_tracks_earliest_target_and_changes() {
        let mut state = VblankCompletionQueueState::new();
        state.schedule(12, Box::new(|_| {}));
        let (_, first_generation) = state.next_target().unwrap();
        assert_eq!(state.next_target().unwrap().0, 12);

        state.schedule(4, Box::new(|_| {}));
        let (target, second_generation) = state.next_target().unwrap();
        assert_eq!(target, 4);
        assert_ne!(first_generation, second_generation);

        state.cancel_all();
        assert!(state.next_target().is_none());
        assert_ne!(state.schedule_generation, second_generation);
    }

    #[ktest]
    fn flip_event_uses_supplied_vblank_snapshot() {
        let clock = super::super::vblank::VblankClock::new();
        let snapshot = clock.snapshot();
        let DrmEvent::Vblank(event) = new_vblank_event(DRM_EVENT_FLIP_COMPLETE, snapshot, 42)
        else {
            panic!("expected a vblank event");
        };

        assert_eq!(event.user_data, 42);
        assert_eq!(event.sequence, snapshot.sequence as u32);
        assert_eq!(event.tv_sec, snapshot.timestamp.as_secs() as u32);
        assert_eq!(event.tv_usec, snapshot.timestamp.subsec_micros());
    }

    #[ktest]
    fn crtc_sequence_event_uses_64_bit_clock_values() {
        let snapshot = super::super::vblank::VblankClock::new().snapshot();
        let DrmEvent::CrtcSequence(event) = new_crtc_sequence_event(snapshot, 43) else {
            panic!("expected a CRTC sequence event");
        };

        assert_eq!(event.user_data, 43);
        assert_eq!(event.sequence, snapshot.sequence);
        assert_eq!(event.time_ns, snapshot.timestamp.as_nanos() as i64);
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
