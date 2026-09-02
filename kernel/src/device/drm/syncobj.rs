// SPDX-License-Identifier: MPL-2.0

//! DRM synchronization objects and timeline points.
//!
//! A syncobj is a per-file handle to a shareable fence container.
//! Binary operations replace that container's current fence.
//! Timeline operations add a point whose fence is chained after the previous payload,
//! preserving GPU submission order even when the newly supplied fence signals first.

use core::{
    fmt::{Debug, Display},
    sync::atomic::{AtomicUsize, Ordering},
    time::Duration,
};

use ostd::{
    mm::VmIo,
    sync::{LocalIrqDisabled, Waiter, Waker},
};

use super::{
    DriHandle,
    fence::{Fence, FenceCallbackRegistration, PreparedFenceCallback, PreparedFenceChain},
};
use crate::{
    context::current_userspace,
    events::IoEvents,
    fs::{
        file::{
            AccessMode, CreationFlags, FileCommon, FileLike, StatusFlags,
            file_table::{FdFlags, FileDesc, WithFileTable},
        },
        pseudofs::AnonInodeFs,
    },
    prelude::*,
    process::{
        posix_thread::FileTableRefMut,
        signal::{PollHandle, Pollable},
    },
    time::{clocks::MonotonicClock, timer::Timeout, wait::ManagedTimeout},
    util::ioctl::{InOutData, Ioctl},
};

pub(super) const DRM_SYNCOBJ_CREATE_SIGNALED: u32 = 1 << 0;
pub(super) const DRM_SYNCOBJ_WAIT_ALL: u32 = 1 << 0;
pub(super) const DRM_SYNCOBJ_WAIT_FOR_SUBMIT: u32 = 1 << 1;
pub(super) const DRM_SYNCOBJ_WAIT_AVAILABLE: u32 = 1 << 2;
pub(super) const DRM_SYNCOBJ_WAIT_DEADLINE: u32 = 1 << 3;
pub(super) const DRM_SYNCOBJ_QUERY_LAST_SUBMITTED: u32 = 1 << 0;

pub(super) const DRM_SYNCOBJ_FD_IMPORT_SYNC_FILE: u32 = 1 << 0;
pub(super) const DRM_SYNCOBJ_FD_EXPORT_SYNC_FILE: u32 = 1 << 0;

/// Prevents untrusted userspace from forcing unbounded ioctl allocations.
pub(super) const MAX_SYNCOBJ_ARRAY_ITEMS: usize = 4096;
/// Bounds one syncobj's retained timeline dependency graph.
const MAX_TIMELINE_POINTS: usize = 4096;
/// Bounds strong eventfd references retained by one unavailable syncobj.
const MAX_EVENT_WATCHERS: usize = 4096;
/// Bounds eventfd references retained by all DRM clients system-wide.
const MAX_SYSTEM_EVENT_WATCHERS: usize = 16384;
static SYSTEM_EVENT_WATCHER_COUNT: AtomicUsize = AtomicUsize::new(0);
/// Mirrors Linux's bounded wait for a fence to be submitted into a syncobj.
const WAIT_FOR_SUBMIT_TIMEOUT: Duration = Duration::from_secs(5);

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmSyncobjCreate {
    pub handle: u32,
    pub flags: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmSyncobjDestroy {
    pub handle: u32,
    pub pad: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmSyncobjHandle {
    pub handle: u32,
    pub flags: u32,
    pub fd: i32,
    pub pad: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmSyncobjTransfer {
    pub src_handle: u32,
    pub dst_handle: u32,
    pub src_point: u64,
    pub dst_point: u64,
    pub flags: u32,
    pub pad: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmSyncobjWait {
    pub handles: u64,
    pub timeout_nsec: i64,
    pub count_handles: u32,
    pub flags: u32,
    pub first_signaled: u32,
    pub pad: u32,
    pub deadline_nsec: u64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmSyncobjTimelineWait {
    pub handles: u64,
    pub points: u64,
    pub timeout_nsec: i64,
    pub count_handles: u32,
    pub flags: u32,
    pub first_signaled: u32,
    pub pad: u32,
    pub deadline_nsec: u64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmSyncobjArray {
    pub handles: u64,
    pub count_handles: u32,
    pub pad: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmSyncobjTimelineArray {
    pub handles: u64,
    pub points: u64,
    pub count_handles: u32,
    pub flags: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmSyncobjEventfd {
    pub handle: u32,
    pub flags: u32,
    pub point: u64,
    pub fd: i32,
    pub pad: u32,
}

struct TimelinePoint {
    sequence: u64,
    fence: Arc<Fence>,
}

enum SyncPayload {
    Binary(Arc<Fence>),
    Timeline {
        signaled_point: u64,
        last_submitted: u64,
        current: Arc<Fence>,
    },
}

#[derive(Default)]
struct SyncObjectState {
    payload: Option<SyncPayload>,
    points: VecDeque<TimelinePoint>,
    /// Capacity promised to unpublished timeline points.
    reserved_point_count: usize,
}

/// A shareable DRM syncobj. Handles and exported syncobj fds own `Arc`s to it.
pub(super) struct SyncObject {
    state: SpinLock<SyncObjectState, LocalIrqDisabled>,
    signaled_fence: Arc<Fence>,
    watchers: SpinLock<Vec<Weak<Waker>>, LocalIrqDisabled>,
    fence_callbacks: SpinLock<SyncobjFenceCallbacks, LocalIrqDisabled>,
    event_watchers: SpinLock<EventWatcherList, LocalIrqDisabled>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum SubmissionWait {
    Immediate,
    Wait,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TimelineQuery {
    Signaled,
    LastSubmitted,
}

impl Debug for SyncObject {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("SyncObject").finish_non_exhaustive()
    }
}

impl SyncObject {
    pub(super) fn new() -> Result<Arc<Self>> {
        Self::with_initial_signal(false)
    }

    pub(super) fn new_signaled() -> Result<Arc<Self>> {
        Self::with_initial_signal(true)
    }

    fn with_initial_signal(signaled: bool) -> Result<Arc<Self>> {
        let signaled_fence = Arc::try_new(Fence::new())
            .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate signaled fence"))?;
        signaled_fence.signal_success();
        let payload = signaled.then(|| SyncPayload::Binary(signaled_fence.clone()));
        Arc::try_new(Self {
            state: SpinLock::new(SyncObjectState {
                payload,
                ..Default::default()
            }),
            signaled_fence,
            watchers: SpinLock::new(Vec::new()),
            fence_callbacks: SpinLock::new(SyncobjFenceCallbacks::default()),
            event_watchers: SpinLock::new(EventWatcherList::default()),
        })
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate syncobj"))
    }

    pub(super) fn replace_fence(self: &Arc<Self>, fence: Option<Arc<Fence>>) -> Result<()> {
        let notification = fence
            .as_ref()
            .filter(|fence| !fence.is_signaled())
            .map(|_| self.reserve_notification())
            .transpose()?;
        self.install_binary_fence(fence, notification);
        Ok(())
    }

    pub(super) fn clear_fence(self: &Arc<Self>) {
        self.install_binary_fence(None, None);
    }

    fn install_binary_fence(
        self: &Arc<Self>,
        fence: Option<Arc<Fence>>,
        notification: Option<SyncobjNotification>,
    ) {
        // Keep the callback -> event watcher -> state lock order in sync with
        // `SyncobjNotification::publish` and event watcher registration.
        let mut callbacks = self.fence_callbacks.lock();
        callbacks.entries.clear();
        let event_watchers = self.event_watchers.lock();
        let mut state = self.state.lock();
        state.payload = fence.clone().map(SyncPayload::Binary);
        state.points.clear();
        let event_notification = EventNotification::new(&event_watchers, &mut state);
        drop(state);
        drop(event_watchers);
        drop(callbacks);
        if let (Some(fence), Some(notification)) = (fence, notification) {
            notification.publish(&fence);
        }
        self.notify_watchers_with(event_notification);
    }

    pub(super) fn signal_binary(self: &Arc<Self>) {
        self.install_binary_fence(Some(self.signaled_fence.clone()), None);
    }

    /// Adds a fence to a timeline. Point zero has binary replacement semantics.
    pub(super) fn add_point(self: &Arc<Self>, point: u64, fence: Arc<Fence>) -> Result<()> {
        if point == 0 {
            return self.replace_fence(Some(fence));
        }

        let prepared_chain = Fence::prepare_chain(Fence::reserve_chain_slot()?)?;
        let notification = self.reserve_notification()?;
        self.poll_timeline_completion();
        let event_watchers = self.event_watchers.lock();
        let mut state = self.state.lock();
        refresh_timeline(&mut state);
        reserve_point_storage(&mut state, 1)?;
        let previous = current_fence(&state);
        let chained = Fence::finish_chain(prepared_chain, previous, fence);
        append_timeline_point(&mut state, point, chained.clone());
        let event_notification = EventNotification::new(&event_watchers, &mut state);
        drop(state);
        drop(event_watchers);
        notification.publish(&chained);
        self.notify_watchers_with(event_notification);
        Ok(())
    }

    /// Prepares one binary or timeline publication before a GPU submission.
    pub(super) fn reserve_publication(self: &Arc<Self>, point: u64) -> Result<SyncobjPublication> {
        if point == 0 {
            return Ok(SyncobjPublication {
                syncobj: self.clone(),
                kind: SyncobjPublicationKind::Binary {
                    notification: Some(self.reserve_notification()?),
                },
            });
        }

        self.poll_timeline_completion();
        let prepared_chain = Fence::prepare_chain(Fence::reserve_chain_slot()?)?;
        let notification = self.reserve_notification()?;
        let mut state = self.state.lock();
        refresh_timeline(&mut state);
        reserve_point_storage(&mut state, 1)?;
        state.reserved_point_count += 1;
        Ok(SyncobjPublication {
            syncobj: self.clone(),
            kind: SyncobjPublicationKind::Timeline {
                point,
                prepared_chain: Some(prepared_chain),
                notification: Some(notification),
                active: true,
            },
        })
    }

    fn publish_reserved_point(
        self: &Arc<Self>,
        point: u64,
        fence: Arc<Fence>,
        prepared_chain: PreparedFenceChain,
        notification: SyncobjNotification,
    ) {
        let event_watchers = self.event_watchers.lock();
        let mut state = self.state.lock();
        refresh_timeline(&mut state);
        debug_assert!(state.reserved_point_count > 0);
        state.reserved_point_count -= 1;
        let previous = current_fence(&state);
        let chained = Fence::finish_chain(prepared_chain, previous, fence);
        append_timeline_point(&mut state, point, chained.clone());
        let event_notification = EventNotification::new(&event_watchers, &mut state);
        drop(state);
        drop(event_watchers);
        notification.publish(&chained);
        self.notify_watchers_with(event_notification);
    }

    #[cfg(ktest)]
    pub(super) fn add_signaled_point(self: &Arc<Self>, point: u64) -> Result<()> {
        self.add_point(point, self.signaled_fence.clone())
    }

    /// Finds the fence representing `point`, or `None` if it is not submitted.
    pub(super) fn find_fence(&self, point: u64) -> Option<Arc<Fence>> {
        let mut state = self.state.lock();
        refresh_timeline(&mut state);
        match state.payload.as_ref()? {
            SyncPayload::Binary(fence) => (point == 0).then(|| fence.clone()),
            SyncPayload::Timeline {
                signaled_point,
                current,
                ..
            } => {
                if point == 0 {
                    return Some(current.clone());
                }
                if point <= *signaled_point {
                    return Some(self.signaled_fence.clone());
                }
                state
                    .points
                    .iter()
                    .find(|entry| entry.sequence >= point)
                    .map(|entry| entry.fence.clone())
            }
        }
    }

    fn query_point(&self, query: TimelineQuery) -> u64 {
        self.poll_timeline_completion();
        let mut state = self.state.lock();
        refresh_timeline(&mut state);
        match state.payload.as_ref() {
            Some(SyncPayload::Timeline {
                signaled_point,
                last_submitted: submitted,
                ..
            }) => {
                if query == TimelineQuery::LastSubmitted {
                    *submitted
                } else {
                    *signaled_point
                }
            }
            _ => 0,
        }
    }

    pub(super) fn wait_for_fence(
        self: &Arc<Self>,
        point: u64,
        submission_wait: SubmissionWait,
    ) -> Result<Arc<Fence>> {
        if let Some(fence) = self.find_fence(point) {
            return Ok(fence);
        }
        if submission_wait == SubmissionWait::Immediate {
            return_errno_with_message!(Errno::EINVAL, "syncobj fence has not been submitted");
        }

        let (waiter, _) = Waiter::new_pair();
        self.register_waker(&waiter.waker())?;
        waiter.pause_until_or_timeout(|| self.find_fence(point), Some(&WAIT_FOR_SUBMIT_TIMEOUT))
    }

    fn reserve_notification(self: &Arc<Self>) -> Result<SyncobjNotification> {
        let weak = Arc::downgrade(self);
        let callback = Fence::prepare_callback(move || {
            if let Some(syncobj) = weak.upgrade() {
                syncobj.notify_watchers();
            }
        })?;
        let mut callbacks = self.fence_callbacks.lock();
        callbacks
            .entries
            .retain(FenceCallbackRegistration::is_active);
        let additional = callbacks.reserved_count.checked_add(1).ok_or_else(|| {
            Error::with_message(Errno::ENOSPC, "syncobj callback reservation overflows")
        })?;
        callbacks.entries.try_reserve(additional).map_err(|_| {
            Error::with_message(Errno::ENOMEM, "cannot reserve syncobj fence callback")
        })?;
        callbacks.reserved_count += 1;
        Ok(SyncobjNotification {
            syncobj: self.clone(),
            callback: Some(callback),
            active: true,
        })
    }

    fn poll_timeline_completion(&self) {
        let current = {
            let state = self.state.lock();
            match state.payload.as_ref() {
                Some(SyncPayload::Timeline { current, .. }) => Some(current.clone()),
                _ => None,
            }
        };
        if let Some(current) = current {
            current.poll_and_is_signaled();
        }
    }

    fn register_waker(&self, waker: &Arc<Waker>) -> Result<()> {
        let mut watchers = self.watchers.lock();
        watchers.retain(|weak| weak.strong_count() != 0);
        if watchers
            .iter()
            .filter_map(Weak::upgrade)
            .any(|registered| Arc::ptr_eq(&registered, waker))
        {
            return Ok(());
        }
        watchers
            .try_reserve(1)
            .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot register syncobj waiter"))?;
        watchers.push(Arc::downgrade(waker));
        Ok(())
    }

    fn notify_watchers(&self) {
        let event_notification = {
            let event_watchers = self.event_watchers.lock();
            let mut state = self.state.lock();
            EventNotification::new(&event_watchers, &mut state)
        };
        self.notify_watchers_with(event_notification);
    }

    fn notify_watchers_with(&self, event_notification: EventNotification) {
        self.fence_callbacks
            .lock()
            .entries
            .retain(FenceCallbackRegistration::is_active);
        let mut watchers = self.watchers.lock();
        watchers.retain(|weak| {
            let Some(watcher) = weak.upgrade() else {
                return false;
            };
            watcher.wake_up();
            true
        });
        drop(watchers);
        self.notify_event_watchers(event_notification);
    }

    fn register_event_watcher(
        &self,
        point: u64,
        available_only: bool,
        event_file: Arc<crate::syscall::EventFile>,
    ) -> Result<()> {
        let quota = EventWatcherQuota::reserve()?;
        // Poll before taking the event watcher lock because completion may run
        // callbacks. The lock then serializes registration with state changes.
        self.poll_timeline_completion();
        let mut watchers = self.event_watchers.lock();
        if watchers.entries.len() >= MAX_EVENT_WATCHERS {
            return_errno_with_message!(Errno::ENOSPC, "syncobj has too many eventfd watchers");
        }
        watchers.entries.try_reserve(1).map_err(|_| {
            Error::with_message(Errno::ENOMEM, "cannot register syncobj eventfd watcher")
        })?;
        let generation = watchers.next_generation;
        watchers.next_generation = generation.checked_add(1).ok_or_else(|| {
            Error::with_message(Errno::ENOSPC, "syncobj eventfd generation exhausted")
        })?;
        let watcher = Arc::try_new(SyncobjEventWatcher {
            generation,
            point,
            available_only,
            event_file,
            _quota: quota,
        })
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate eventfd watcher"))?;
        watchers.entries.push(watcher.clone());
        let readiness = {
            let mut state = self.state.lock();
            EventReadiness::from_state(&mut state)
        };
        drop(watchers);
        // Only the newly registered watcher needs a readiness check. Scanning
        // every older watcher here would make N registrations O(N^2).
        self.notify_event_watcher(&watcher, readiness);
        Ok(())
    }

    fn notify_event_watchers(&self, notification: EventNotification) {
        self.event_watchers.lock().entries.retain(|watcher| {
            if watcher.generation < notification.generation_cutoff
                && notification
                    .readiness
                    .is_ready(watcher.point, watcher.available_only)
            {
                watcher.event_file.signal();
                false
            } else {
                true
            }
        });
    }

    fn notify_event_watcher(&self, watcher: &Arc<SyncobjEventWatcher>, readiness: EventReadiness) {
        if readiness.is_ready(watcher.point, watcher.available_only) {
            let removed = {
                let mut registered = self.event_watchers.lock();
                registered
                    .entries
                    .iter()
                    .position(|candidate| Arc::ptr_eq(candidate, watcher))
                    .map(|index| registered.entries.swap_remove(index))
            };
            if let Some(watcher) = removed {
                watcher.event_file.signal();
            }
        }
    }
}

#[derive(Clone, Copy)]
struct EventNotification {
    generation_cutoff: u64,
    readiness: EventReadiness,
}

impl EventNotification {
    /// Captures a state transition while registration is excluded.
    fn new(watchers: &EventWatcherList, state: &mut SyncObjectState) -> Self {
        Self {
            generation_cutoff: watchers.next_generation,
            readiness: EventReadiness::from_state(state),
        }
    }
}

impl EventReadiness {
    fn from_state(state: &mut SyncObjectState) -> Self {
        refresh_timeline(state);
        match state.payload.as_ref() {
            None => EventReadiness::Empty,
            Some(SyncPayload::Binary(fence)) => EventReadiness::Binary {
                signaled: fence.is_signaled(),
            },
            Some(SyncPayload::Timeline {
                signaled_point,
                last_submitted,
                current,
            }) => EventReadiness::Timeline {
                signaled_point: *signaled_point,
                last_submitted: *last_submitted,
                current_signaled: current.is_signaled(),
            },
        }
    }
}

struct SyncobjEventWatcher {
    generation: u64,
    point: u64,
    available_only: bool,
    event_file: Arc<crate::syscall::EventFile>,
    _quota: EventWatcherQuota,
}

#[derive(Default)]
struct SyncobjFenceCallbacks {
    entries: Vec<FenceCallbackRegistration>,
    reserved_count: usize,
}

struct SyncobjNotification {
    syncobj: Arc<SyncObject>,
    callback: Option<PreparedFenceCallback>,
    active: bool,
}

impl SyncobjNotification {
    fn publish(mut self, fence: &Arc<Fence>) {
        let registration = fence.register_callback(
            self.callback
                .take()
                .expect("syncobj notification lost its prepared callback"),
        );
        let mut callbacks = self.syncobj.fence_callbacks.lock();
        let state = self.syncobj.state.lock();
        let registration = registration
            .filter(|registration| registration.is_active() && state_contains_fence(&state, fence));
        drop(state);
        debug_assert!(callbacks.reserved_count > 0);
        callbacks.reserved_count -= 1;
        if let Some(registration) = registration {
            debug_assert!(callbacks.entries.len() < callbacks.entries.capacity());
            callbacks.entries.push(registration);
        }
        self.active = false;
    }
}

impl Drop for SyncobjNotification {
    fn drop(&mut self) {
        if self.active {
            let mut callbacks = self.syncobj.fence_callbacks.lock();
            debug_assert!(callbacks.reserved_count > 0);
            callbacks.reserved_count -= 1;
        }
    }
}

#[derive(Default)]
struct EventWatcherList {
    entries: Vec<Arc<SyncobjEventWatcher>>,
    next_generation: u64,
}

#[derive(Clone, Copy)]
enum EventReadiness {
    Empty,
    Binary {
        signaled: bool,
    },
    Timeline {
        signaled_point: u64,
        last_submitted: u64,
        current_signaled: bool,
    },
}

impl EventReadiness {
    fn is_ready(self, point: u64, available_only: bool) -> bool {
        match self {
            Self::Empty => false,
            Self::Binary { signaled } => point == 0 && (available_only || signaled),
            Self::Timeline {
                signaled_point,
                last_submitted,
                current_signaled,
            } => {
                if point == 0 {
                    available_only || current_signaled
                } else if available_only {
                    point <= last_submitted
                } else {
                    point <= signaled_point
                }
            }
        }
    }
}

struct EventWatcherQuota;

impl EventWatcherQuota {
    fn reserve() -> Result<Self> {
        SYSTEM_EVENT_WATCHER_COUNT
            .try_update(Ordering::AcqRel, Ordering::Acquire, |count| {
                (count < MAX_SYSTEM_EVENT_WATCHERS).then_some(count + 1)
            })
            .map_err(|_| {
                Error::with_message(Errno::ENOSPC, "system syncobj eventfd limit reached")
            })?;
        Ok(Self)
    }
}

impl Drop for EventWatcherQuota {
    fn drop(&mut self) {
        let old_count = SYSTEM_EVENT_WATCHER_COUNT.fetch_sub(1, Ordering::AcqRel);
        debug_assert!(old_count > 0);
    }
}

fn current_fence(state: &SyncObjectState) -> Option<Arc<Fence>> {
    match state.payload.as_ref() {
        Some(SyncPayload::Binary(fence)) => Some(fence.clone()),
        Some(SyncPayload::Timeline { current, .. }) => Some(current.clone()),
        None => None,
    }
}

fn state_contains_fence(state: &SyncObjectState, fence: &Arc<Fence>) -> bool {
    let is_current = match state.payload.as_ref() {
        Some(SyncPayload::Binary(current)) => Arc::ptr_eq(current, fence),
        Some(SyncPayload::Timeline { current, .. }) => Arc::ptr_eq(current, fence),
        None => false,
    };
    is_current
        || state
            .points
            .iter()
            .any(|point| Arc::ptr_eq(&point.fence, fence))
}

fn reserve_point_storage(state: &mut SyncObjectState, extra: usize) -> Result<()> {
    let retained = state
        .points
        .len()
        .checked_add(state.reserved_point_count)
        .and_then(|count| count.checked_add(extra))
        .ok_or_else(|| Error::with_message(Errno::ENOSPC, "syncobj timeline size overflows"))?;
    if retained > MAX_TIMELINE_POINTS {
        return_errno_with_message!(
            Errno::ENOSPC,
            "syncobj timeline has too many pending points"
        );
    }

    let additional = state
        .reserved_point_count
        .checked_add(extra)
        .ok_or_else(|| Error::with_message(Errno::ENOSPC, "syncobj reservation overflows"))?;
    state
        .points
        .try_reserve(additional)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot grow syncobj timeline"))
}

fn append_timeline_point(state: &mut SyncObjectState, point: u64, fence: Arc<Fence>) {
    debug_assert!(state.points.len() + state.reserved_point_count < state.points.capacity());
    state.points.push_back(TimelinePoint {
        sequence: point,
        fence: fence.clone(),
    });
    match state.payload.as_mut() {
        Some(SyncPayload::Timeline {
            last_submitted,
            current,
            ..
        }) => {
            *last_submitted = (*last_submitted).max(point);
            *current = fence;
        }
        _ => {
            state.payload = Some(SyncPayload::Timeline {
                signaled_point: 0,
                last_submitted: point,
                current: fence,
            });
        }
    }
}

fn refresh_timeline(state: &mut SyncObjectState) {
    if !matches!(state.payload, Some(SyncPayload::Timeline { .. })) {
        return;
    }

    let mut newest = None::<u64>;
    while state
        .points
        .front()
        .is_some_and(|entry| entry.fence.is_signaled())
    {
        let entry = state.points.pop_front().unwrap();
        newest = Some(newest.unwrap_or(0).max(entry.sequence));
    }
    if let (Some(newest), Some(SyncPayload::Timeline { signaled_point, .. })) =
        (newest, state.payload.as_mut())
    {
        *signaled_point = (*signaled_point).max(newest);
    }
}

/// Publication token that reserves recoverable capacity before submission.
pub(super) struct SyncobjPublication {
    syncobj: Arc<SyncObject>,
    kind: SyncobjPublicationKind,
}

enum SyncobjPublicationKind {
    Binary {
        notification: Option<SyncobjNotification>,
    },
    Timeline {
        point: u64,
        prepared_chain: Option<PreparedFenceChain>,
        notification: Option<SyncobjNotification>,
        active: bool,
    },
}

impl SyncobjPublication {
    pub(super) fn publish(mut self, fence: Arc<Fence>) {
        match &mut self.kind {
            SyncobjPublicationKind::Binary { notification } => self.syncobj.install_binary_fence(
                Some(fence),
                Some(
                    notification
                        .take()
                        .expect("binary publication lost its notification"),
                ),
            ),
            SyncobjPublicationKind::Timeline {
                point,
                prepared_chain,
                notification,
                active,
            } => {
                self.syncobj.publish_reserved_point(
                    *point,
                    fence,
                    prepared_chain
                        .take()
                        .expect("syncobj publication lost its prepared fence"),
                    notification
                        .take()
                        .expect("syncobj publication lost its notification"),
                );
                *active = false;
            }
        }
    }
}

impl Drop for SyncobjPublication {
    fn drop(&mut self) {
        if !matches!(
            &self.kind,
            SyncobjPublicationKind::Timeline { active: true, .. }
        ) {
            return;
        }
        let mut state = self.syncobj.state.lock();
        debug_assert!(state.reserved_point_count > 0);
        state.reserved_point_count -= 1;
    }
}

pub(super) fn reserve_publication_batch(
    outputs: &[(Arc<SyncObject>, u64)],
) -> Result<Vec<SyncobjPublication>> {
    let mut publications = Vec::new();
    publications
        .try_reserve_exact(outputs.len())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot reserve output syncobjs"))?;
    for (syncobj, point) in outputs {
        publications.push(syncobj.reserve_publication(*point)?);
    }
    Ok(publications)
}

fn signal_publication_batch(outputs: &[(Arc<SyncObject>, u64)]) -> Result<()> {
    let publications = reserve_publication_batch(outputs)?;
    let signaled_fence = Arc::try_new(Fence::new())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate signaled fence"))?;
    signaled_fence.signal_success();
    for publication in publications {
        publication.publish(signaled_fence.clone());
    }
    Ok(())
}

/// Opaque fd used to share the syncobj itself between processes.
pub(super) struct SyncObjectFile {
    common: FileCommon,
    syncobj: Arc<SyncObject>,
}

impl SyncObjectFile {
    pub(super) fn new(syncobj: Arc<SyncObject>) -> Self {
        let path = AnonInodeFs::new_path(|_| "anon_inode:[syncobj_file]".to_string());
        Self {
            common: FileCommon::new(path, StatusFlags::empty()),
            syncobj,
        }
    }

    pub(super) fn syncobj(&self) -> Arc<SyncObject> {
        self.syncobj.clone()
    }
}

impl Pollable for SyncObjectFile {
    fn poll(&self, _mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        IoEvents::empty()
    }
}

impl FileLike for SyncObjectFile {
    /// The retained syncobj can hold eventfd watchers, and an eventfd retains
    /// nothing, so every ownership chain from this file terminates at a leaf.
    fn is_scm_rights_proven_leaf(&self) -> bool {
        true
    }

    fn access_mode(&self) -> AccessMode {
        AccessMode::O_RDWR
    }

    fn common(&self) -> &FileCommon {
        &self.common
    }

    fn dump_proc_fdinfo(self: Arc<Self>, fd_flags: FdFlags) -> Box<dyn Display> {
        struct FdInfo(u32);
        impl Display for FdInfo {
            fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
                writeln!(f, "pos:\t0")?;
                writeln!(f, "flags:\t0{:o}", self.0)
            }
        }
        let mut flags = self.common.status_flags().bits() | self.access_mode() as u32;
        if fd_flags.contains(FdFlags::CLOEXEC) {
            flags |= CreationFlags::O_CLOEXEC.bits();
        }
        Box::new(FdInfo(flags))
    }
}

pub(super) fn read_handles(pointer: u64, count: u32) -> Result<Vec<u32>> {
    read_array::<4, u32>(pointer, count, u32::from_le_bytes)
}

pub(super) fn read_points(pointer: u64, count: u32) -> Result<Vec<u64>> {
    read_array::<8, u64>(pointer, count, u64::from_le_bytes)
}

fn read_array<const N: usize, T>(
    pointer: u64,
    count: u32,
    decode_fn: impl Fn([u8; N]) -> T,
) -> Result<Vec<T>> {
    let count = validate_count(count)?;
    if count == 0 {
        return Ok(Vec::new());
    }
    if pointer == 0 {
        return_errno_with_message!(Errno::EFAULT, "null syncobj array pointer");
    }
    let byte_len = count
        .checked_mul(N)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "syncobj array size overflows"))?;
    let mut bytes = Vec::new();
    bytes
        .try_reserve_exact(byte_len)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate syncobj input array"))?;
    bytes.resize(byte_len, 0);
    current_userspace!().read_bytes(pointer as usize, &mut bytes)?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(count)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot decode syncobj input array"))?;
    for bytes in bytes.as_chunks::<N>().0 {
        output.push(decode_fn(*bytes));
    }
    Ok(output)
}

fn validate_count(count: u32) -> Result<usize> {
    let count = count as usize;
    if count > MAX_SYNCOBJ_ARRAY_ITEMS {
        return_errno_with_message!(Errno::EINVAL, "too many syncobj array items");
    }
    Ok(count)
}

pub(super) fn lookup_syncobjs(handle: &DriHandle, handles: &[u32]) -> Result<Vec<Arc<SyncObject>>> {
    let inner = handle.inner.lock();
    let mut syncobjs = Vec::new();
    syncobjs
        .try_reserve_exact(handles.len())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate syncobj lookup array"))?;
    for id in handles {
        let syncobj = inner
            .syncobjs
            .get(id)
            .cloned()
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown syncobj handle"))?;
        syncobjs.push(syncobj);
    }
    Ok(syncobjs)
}

fn insert_syncobj(handle: &DriHandle, syncobj: Arc<SyncObject>) -> Result<u32> {
    let mut inner = handle.inner.lock();
    if inner.syncobjs.len() >= MAX_SYNCOBJ_ARRAY_ITEMS {
        return_errno_with_message!(Errno::EMFILE, "too many syncobj handles on DRM file");
    }
    let id = inner.next_syncobj_handle;
    inner.next_syncobj_handle = id
        .checked_add(1)
        .ok_or_else(|| Error::with_message(Errno::ENOSPC, "syncobj handle space exhausted"))?;
    inner.syncobjs.insert(id, syncobj);
    Ok(id)
}

fn remove_syncobj(handle: &DriHandle, id: u32) -> Result<Arc<SyncObject>> {
    handle
        .inner
        .lock()
        .syncobjs
        .remove(&id)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown syncobj handle"))
}

pub(super) fn create(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0xbf, true, InOutData<DrmSyncobjCreate>>,
) -> Result<i32> {
    let mut req = cmd.read()?;
    if req.flags & !DRM_SYNCOBJ_CREATE_SIGNALED != 0 {
        return_errno_with_message!(Errno::EINVAL, "unknown syncobj create flags");
    }
    let syncobj = if req.flags & DRM_SYNCOBJ_CREATE_SIGNALED != 0 {
        SyncObject::new_signaled()?
    } else {
        SyncObject::new()?
    };
    let id = insert_syncobj(handle, syncobj)?;
    req.handle = id;
    if let Err(error) = cmd.write(&req) {
        let _ = remove_syncobj(handle, id);
        return Err(error);
    }
    Ok(0)
}

pub(super) fn destroy(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0xc0, true, InOutData<DrmSyncobjDestroy>>,
) -> Result<i32> {
    let req = cmd.read()?;
    if req.pad != 0 {
        return_errno_with_message!(Errno::EINVAL, "syncobj destroy padding is nonzero");
    }
    remove_syncobj(handle, req.handle)?;
    Ok(0)
}

pub(super) fn wait(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0xc3, true, InOutData<DrmSyncobjWait>>,
) -> Result<i32> {
    let mut req = cmd.read()?;
    let valid_flags =
        DRM_SYNCOBJ_WAIT_ALL | DRM_SYNCOBJ_WAIT_FOR_SUBMIT | DRM_SYNCOBJ_WAIT_DEADLINE;
    if req.flags & !valid_flags != 0 {
        return_errno_with_message!(Errno::EINVAL, "unknown binary syncobj wait flags");
    }
    if req.count_handles == 0 {
        return Ok(0);
    }
    let ids = read_handles(req.handles, req.count_handles)?;
    let syncobjs = lookup_syncobjs(handle, &ids)?;
    let mut points = Vec::new();
    points
        .try_reserve_exact(syncobjs.len())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate binary wait points"))?;
    points.resize(syncobjs.len(), 0);
    req.first_signaled = wait_many(&syncobjs, &points, req.flags, req.timeout_nsec)?;
    cmd.write(&req)?;
    Ok(0)
}

pub(super) fn timeline_wait(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0xca, true, InOutData<DrmSyncobjTimelineWait>>,
) -> Result<i32> {
    let mut req = cmd.read()?;
    let valid_flags = DRM_SYNCOBJ_WAIT_ALL
        | DRM_SYNCOBJ_WAIT_FOR_SUBMIT
        | DRM_SYNCOBJ_WAIT_AVAILABLE
        | DRM_SYNCOBJ_WAIT_DEADLINE;
    if req.flags & !valid_flags != 0 {
        return_errno_with_message!(Errno::EINVAL, "unknown timeline syncobj wait flags");
    }
    if req.count_handles == 0 {
        return Ok(0);
    }
    let ids = read_handles(req.handles, req.count_handles)?;
    let points = read_points(req.points, req.count_handles)?;
    let syncobjs = lookup_syncobjs(handle, &ids)?;
    req.first_signaled = wait_many(&syncobjs, &points, req.flags, req.timeout_nsec)?;
    cmd.write(&req)?;
    Ok(0)
}

fn array_objects(handle: &DriHandle, req: DrmSyncobjArray) -> Result<Vec<Arc<SyncObject>>> {
    if req.pad != 0 || req.count_handles == 0 {
        return_errno_with_message!(Errno::EINVAL, "invalid syncobj array request");
    }
    let ids = read_handles(req.handles, req.count_handles)?;
    lookup_syncobjs(handle, &ids)
}

pub(super) fn reset(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0xc4, true, InOutData<DrmSyncobjArray>>,
) -> Result<i32> {
    for syncobj in array_objects(handle, cmd.read()?)? {
        syncobj.clear_fence();
    }
    Ok(0)
}

pub(super) fn signal(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0xc5, true, InOutData<DrmSyncobjArray>>,
) -> Result<i32> {
    for syncobj in array_objects(handle, cmd.read()?)? {
        syncobj.signal_binary();
    }
    Ok(0)
}

pub(super) fn timeline_signal(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0xcd, true, InOutData<DrmSyncobjTimelineArray>>,
) -> Result<i32> {
    let req = cmd.read()?;
    if req.flags != 0 || req.count_handles == 0 {
        return_errno_with_message!(Errno::EINVAL, "invalid timeline signal request");
    }
    let ids = read_handles(req.handles, req.count_handles)?;
    let points = read_points(req.points, req.count_handles)?;
    let syncobjs = lookup_syncobjs(handle, &ids)?;
    let mut outputs = Vec::new();
    outputs
        .try_reserve_exact(syncobjs.len())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot reserve timeline signal batch"))?;
    for (syncobj, point) in syncobjs.into_iter().zip(points) {
        outputs.push((syncobj, point));
    }
    signal_publication_batch(&outputs)?;
    Ok(0)
}

pub(super) fn query(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0xcb, true, InOutData<DrmSyncobjTimelineArray>>,
) -> Result<i32> {
    let req = cmd.read()?;
    if req.flags & !DRM_SYNCOBJ_QUERY_LAST_SUBMITTED != 0 || req.count_handles == 0 {
        return_errno_with_message!(Errno::EINVAL, "invalid syncobj query request");
    }
    if req.points == 0 {
        return_errno_with_message!(Errno::EFAULT, "null syncobj query output pointer");
    }
    let ids = read_handles(req.handles, req.count_handles)?;
    let syncobjs = lookup_syncobjs(handle, &ids)?;
    let query = if req.flags & DRM_SYNCOBJ_QUERY_LAST_SUBMITTED != 0 {
        TimelineQuery::LastSubmitted
    } else {
        TimelineQuery::Signaled
    };
    let mut output = Vec::new();
    output
        .try_reserve_exact(syncobjs.len() * size_of::<u64>())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate syncobj query output"))?;
    for syncobj in syncobjs {
        output.extend_from_slice(&syncobj.query_point(query).to_le_bytes());
    }
    current_userspace!().write_bytes(req.points as usize, &output)?;
    Ok(0)
}

pub(super) fn transfer(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0xcc, true, InOutData<DrmSyncobjTransfer>>,
) -> Result<i32> {
    let req = cmd.read()?;
    if req.pad != 0 || req.flags & !DRM_SYNCOBJ_WAIT_FOR_SUBMIT != 0 {
        return_errno_with_message!(Errno::EINVAL, "invalid syncobj transfer request");
    }
    let objects = lookup_syncobjs(handle, &[req.src_handle, req.dst_handle])?;
    let submission_wait = if req.flags & DRM_SYNCOBJ_WAIT_FOR_SUBMIT != 0 {
        SubmissionWait::Wait
    } else {
        SubmissionWait::Immediate
    };
    let source = objects[0].wait_for_fence(req.src_point, submission_wait)?;
    if req.dst_point == 0 {
        objects[1].replace_fence(Some(source))?;
    } else {
        objects[1].add_point(req.dst_point, source)?;
    }
    Ok(0)
}

pub(super) fn handle_to_fd(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0xc1, true, InOutData<DrmSyncobjHandle>>,
    file_table: &mut FileTableRefMut,
) -> Option<Result<i32>> {
    Some((|| -> Result<i32> {
        let mut req = cmd.read()?;
        if req.pad != 0 || req.flags & !DRM_SYNCOBJ_FD_EXPORT_SYNC_FILE != 0 {
            return_errno_with_message!(Errno::EINVAL, "invalid syncobj export request");
        }
        let syncobj = lookup_syncobjs(handle, &[req.handle])?.remove(0);
        let file: Arc<dyn FileLike> = if req.flags & DRM_SYNCOBJ_FD_EXPORT_SYNC_FILE != 0 {
            Arc::new(super::fence::FenceFile::new(
                syncobj.wait_for_fence(0, SubmissionWait::Immediate)?,
            ))
        } else {
            Arc::new(SyncObjectFile::new(syncobj))
        };
        let fd = file_table
            .unwrap()
            .write()
            .insert(file.clone(), FdFlags::CLOEXEC);
        req.fd = u32::from(fd) as i32;
        if let Err(error) = cmd.write(&req) {
            let closed = file_table.unwrap().write().close_file_if_same(fd, &file);
            drop(closed);
            return Err(error);
        }
        Ok(0)
    })())
}

pub(super) fn fd_to_handle(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0xc2, true, InOutData<DrmSyncobjHandle>>,
    file_table: &mut FileTableRefMut,
) -> Option<Result<i32>> {
    Some((|| -> Result<i32> {
        let mut req = cmd.read()?;
        if req.pad != 0 || req.flags & !DRM_SYNCOBJ_FD_IMPORT_SYNC_FILE != 0 {
            return_errno_with_message!(Errno::EINVAL, "invalid syncobj import request");
        }
        let fd = FileDesc::try_from(req.fd).map_err(|_| Error::new(Errno::EBADF))?;
        let file = file_table
            .read_with(|table| table.get_file(fd).cloned())
            .map_err(|_| Error::new(Errno::EBADF))?;

        if req.flags & DRM_SYNCOBJ_FD_IMPORT_SYNC_FILE != 0 {
            let fence = file
                .downcast_ref::<super::fence::FenceFile>()
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "fd is not a sync file"))?
                .fence();
            let syncobj = lookup_syncobjs(handle, &[req.handle])?.remove(0);
            syncobj.replace_fence(Some(fence))?;
            cmd.write(&req)?;
            return Ok(0);
        }

        let syncobj = file
            .downcast_ref::<SyncObjectFile>()
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "fd is not a syncobj file"))?
            .syncobj();
        let id = insert_syncobj(handle, syncobj)?;
        req.handle = id;
        if let Err(error) = cmd.write(&req) {
            let _ = remove_syncobj(handle, id);
            return Err(error);
        }
        Ok(0)
    })())
}

pub(super) fn eventfd(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0xcf, true, InOutData<DrmSyncobjEventfd>>,
    file_table: &mut FileTableRefMut,
) -> Option<Result<i32>> {
    Some((|| -> Result<i32> {
        let req = cmd.read()?;
        if req.pad != 0 || req.flags & !DRM_SYNCOBJ_WAIT_AVAILABLE != 0 {
            return_errno_with_message!(Errno::EINVAL, "invalid syncobj eventfd request");
        }
        let syncobj = lookup_syncobjs(handle, &[req.handle])?.remove(0);
        let fd = FileDesc::try_from(req.fd).map_err(|_| Error::new(Errno::EBADF))?;
        let file = file_table
            .read_with(|table| table.get_file(fd).cloned())
            .map_err(|_| Error::new(Errno::EBADF))?;
        let any_file: Arc<dyn Any + Send + Sync> = file;
        let event_file = Arc::downcast::<crate::syscall::EventFile>(any_file).map_err(|_| {
            Error::with_message(Errno::EINVAL, "syncobj event target is not an eventfd")
        })?;
        syncobj.register_event_watcher(
            req.point,
            req.flags & DRM_SYNCOBJ_WAIT_AVAILABLE != 0,
            event_file,
        )?;
        Ok(0)
    })())
}

pub(super) fn wait_many(
    syncobjs: &[Arc<SyncObject>],
    points: &[u64],
    flags: u32,
    timeout_nsec: i64,
) -> Result<u32> {
    debug_assert_eq!(syncobjs.len(), points.len());
    if syncobjs.is_empty() {
        return Ok(u32::MAX);
    }

    let may_wait_for_submit =
        flags & (DRM_SYNCOBJ_WAIT_FOR_SUBMIT | DRM_SYNCOBJ_WAIT_AVAILABLE) != 0;
    let mut fences = Vec::new();
    fences
        .try_reserve_exact(syncobjs.len())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot retain syncobj wait fences"))?;
    for (syncobj, point) in syncobjs.iter().zip(points) {
        fences.push(syncobj.find_fence(*point));
    }
    if !may_wait_for_submit && fences.iter().any(Option::is_none) {
        return_errno_with_message!(Errno::EINVAL, "syncobj fence has not been submitted");
    }

    let wait_all = flags & DRM_SYNCOBJ_WAIT_ALL != 0;
    let available_only = flags & DRM_SYNCOBJ_WAIT_AVAILABLE != 0;
    let (waiter, _) = Waiter::new_pair();
    let waker = waiter.waker();
    let mut prepared_wakeups = Vec::new();
    prepared_wakeups
        .try_reserve_exact(syncobjs.len())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot reserve fence wakeups"))?;
    let mut fence_registrations = Vec::new();
    fence_registrations
        .try_reserve_exact(syncobjs.len())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot retain fence wakeups"))?;
    for _ in syncobjs {
        let callback = if available_only {
            None
        } else {
            let waker = waker.clone();
            Some(Fence::prepare_callback(move || {
                waker.wake_up();
            })?)
        };
        prepared_wakeups.push(callback);
        fence_registrations.push(None);
    }
    for syncobj in syncobjs {
        syncobj.register_waker(&waker)?;
    }

    let condition_fn = || {
        let mut first = None;
        let mut ready_count = 0;
        for (index, (syncobj, point)) in syncobjs.iter().zip(points).enumerate() {
            if fences[index].is_none() {
                fences[index] = syncobj.find_fence(*point);
            }
            if let (Some(fence), Some(callback)) =
                (fences[index].as_ref(), prepared_wakeups[index].take())
            {
                fence_registrations[index] = fence.register_callback(callback);
            }
            let ready = fences[index]
                .as_ref()
                .is_some_and(|fence| available_only || fence.poll_and_is_signaled());
            if ready {
                first.get_or_insert(index as u32);
                ready_count += 1;
            }
        }
        if (wait_all && ready_count == syncobjs.len()) || (!wait_all && first.is_some()) {
            first
        } else {
            None
        }
    };

    let timeout = absolute_timeout(timeout_nsec);
    waiter.pause_until_or_timeout(condition_fn, timeout)
}

fn absolute_timeout(timeout_nsec: i64) -> Option<ManagedTimeout<'static>> {
    if timeout_nsec == i64::MAX {
        return None;
    }
    let deadline = if timeout_nsec <= 0 {
        Duration::ZERO
    } else {
        Duration::from_nanos(timeout_nsec as u64)
    };
    Some(ManagedTimeout::new_with_manager(
        Timeout::When(deadline),
        MonotonicClock::timer_manager(),
    ))
}

#[cfg(ktest)]
mod tests {
    use core::sync::atomic::AtomicBool;

    use ostd::prelude::ktest;

    use super::*;
    use crate::thread::kernel_thread::ThreadOptions;

    const WATCHER_REGISTRATION_YIELDS: usize = 10_000;

    fn wait_for_registered_watcher(syncobj: &SyncObject) {
        for _ in 0..WATCHER_REGISTRATION_YIELDS {
            if !syncobj.watchers.lock().is_empty() {
                return;
            }
            ostd::task::Task::yield_now();
        }
        panic!("syncobj waiter did not register");
    }

    #[ktest]
    fn binary_reset_and_signal() {
        let syncobj = SyncObject::new().unwrap();
        assert!(syncobj.find_fence(0).is_none());
        syncobj.signal_binary();
        assert!(syncobj.find_fence(0).unwrap().is_signaled());
        syncobj.clear_fence();
        assert!(syncobj.find_fence(0).is_none());
    }

    #[ktest]
    fn timeline_points_reject_null_nonempty_array() {
        assert!(read_points(0, 0).unwrap().is_empty());
        assert_eq!(read_points(0, 1).unwrap_err().error(), Errno::EFAULT);
    }

    #[ktest]
    fn timeline_preserves_previous_dependency() {
        let first = Arc::new(Fence::new());
        let syncobj = SyncObject::new().unwrap();
        syncobj.add_point(4, first.clone()).unwrap();
        syncobj.add_signaled_point(9).unwrap();
        assert_eq!(syncobj.query_point(TimelineQuery::Signaled), 0);
        assert!(!syncobj.find_fence(9).unwrap().is_signaled());
        first.signal_success();
        assert!(syncobj.find_fence(9).unwrap().is_signaled());
        assert_eq!(syncobj.query_point(TimelineQuery::Signaled), 9);
        assert_eq!(syncobj.query_point(TimelineQuery::LastSubmitted), 9);
    }

    #[ktest]
    fn timeline_reservation_publishes_without_growing_storage() {
        let syncobj = SyncObject::new().unwrap();
        let reservation = syncobj.reserve_publication(9).unwrap();
        {
            let state = syncobj.state.lock();
            assert_eq!(state.reserved_point_count, 1);
            assert!(state.points.capacity() > state.points.len());
        }

        // A normal producer may append while the GPU submission owns its
        // reservation; it must preserve enough capacity for both points.
        let first = Arc::new(Fence::new());
        syncobj.add_point(4, first.clone()).unwrap();
        {
            let state = syncobj.state.lock();
            assert_eq!(state.reserved_point_count, 1);
            assert!(state.points.capacity() > state.points.len());
        }

        reservation.publish(Fence::new_signaled());
        {
            let state = syncobj.state.lock();
            assert_eq!(state.reserved_point_count, 0);
            assert_eq!(state.points.len(), 2);
        }
        assert!(!syncobj.find_fence(9).unwrap().is_signaled());
        first.signal_success();
        assert!(syncobj.find_fence(9).unwrap().is_signaled());
    }

    #[ktest]
    fn dropping_timeline_reservation_releases_capacity() {
        let syncobj = SyncObject::new().unwrap();
        let reservation = syncobj.reserve_publication(3).unwrap();
        assert_eq!(syncobj.state.lock().reserved_point_count, 1);
        drop(reservation);
        assert_eq!(syncobj.state.lock().reserved_point_count, 0);
        assert!(syncobj.find_fence(3).is_none());
    }

    #[ktest]
    fn timeline_reservations_count_toward_retention_limit() {
        let mut state = SyncObjectState::default();
        reserve_point_storage(&mut state, MAX_TIMELINE_POINTS).unwrap();
        state.reserved_point_count = MAX_TIMELINE_POINTS;
        assert_eq!(
            reserve_point_storage(&mut state, 1).unwrap_err().error(),
            Errno::ENOSPC
        );
    }

    #[ktest]
    fn timeline_signal_batch_rolls_back_before_partial_publish() {
        let first = SyncObject::new().unwrap();
        let full = SyncObject::new().unwrap();
        {
            let mut state = full.state.lock();
            reserve_point_storage(&mut state, MAX_TIMELINE_POINTS).unwrap();
            state.reserved_point_count = MAX_TIMELINE_POINTS;
        }

        let outputs = [(first.clone(), 1), (full.clone(), 2)];
        let Err(error) = signal_publication_batch(&outputs) else {
            panic!("full second syncobj unexpectedly accepted the batch");
        };
        assert_eq!(error.error(), Errno::ENOSPC);
        assert_eq!(first.state.lock().reserved_point_count, 0);
        assert_eq!(first.fence_callbacks.lock().reserved_count, 0);
        assert!(first.find_fence(1).is_none());
        assert_eq!(full.fence_callbacks.lock().reserved_count, 0);
    }

    #[ktest]
    fn wait_for_submit_wakes_on_future_fence() {
        crate::time::clocks::init_for_ktest();
        let syncobj = SyncObject::new().unwrap();
        let finished = Arc::new(AtomicBool::new(false));
        let waiter = {
            let syncobj = syncobj.clone();
            let finished = finished.clone();
            ThreadOptions::new(move || {
                syncobj.wait_for_fence(0, SubmissionWait::Wait).unwrap();
                finished.store(true, Ordering::Release);
            })
            .spawn()
        };
        wait_for_registered_watcher(&syncobj);
        syncobj.signal_binary();
        waiter.join();
        assert!(finished.load(Ordering::Acquire));
    }

    #[ktest]
    fn syncobj_regression() {
        let fence = Arc::new(Fence::new());
        let syncobj = SyncObject::new().unwrap();
        syncobj.replace_fence(Some(fence.clone())).unwrap();
        let finished = Arc::new(AtomicBool::new(false));
        let waiter = {
            let syncobj = syncobj.clone();
            let finished = finished.clone();
            ThreadOptions::new(move || {
                wait_many(&[syncobj], &[0], 0, i64::MAX).unwrap();
                finished.store(true, Ordering::Release);
            })
            .spawn()
        };
        wait_for_registered_watcher(&syncobj);

        syncobj.clear_fence();
        fence.signal_success();
        waiter.join();
        assert!(finished.load(Ordering::Acquire));

        let syncobj = SyncObject::new_signaled().unwrap();
        let notification = {
            let watchers = syncobj.event_watchers.lock();
            let mut state = syncobj.state.lock();
            EventNotification::new(&watchers, &mut state)
        };
        syncobj.clear_fence();

        assert!(notification.readiness.is_ready(0, false));
        assert!(!EventReadiness::Empty.is_ready(0, false));
    }

    #[ktest]
    fn in_flight_wait_retains_syncobj_after_original_owner_drops() {
        let original = SyncObject::new().unwrap();
        let shared = original.clone();
        let weak = Arc::downgrade(&original);
        let finished = Arc::new(AtomicBool::new(false));
        let waiter = {
            let syncobj = original.clone();
            let finished = finished.clone();
            ThreadOptions::new(move || {
                syncobj.wait_for_fence(0, SubmissionWait::Wait).unwrap();
                finished.store(true, Ordering::Release);
            })
            .spawn()
        };
        wait_for_registered_watcher(&original);

        drop(original);
        assert!(weak.upgrade().is_some());
        shared.signal_binary();
        waiter.join();
        assert!(finished.load(Ordering::Acquire));
    }
}
