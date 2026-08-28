// SPDX-License-Identifier: MPL-2.0

//! DRM synchronization objects and timeline points.
//!
//! A syncobj is a per-file handle to a shareable fence container. Binary
//! operations replace that container's current fence. Timeline operations add
//! a point whose fence is chained after the previous payload, preserving GPU
//! submission order even when the newly supplied fence signals first.

use core::{
    fmt::{Debug, Display},
    mem,
    sync::atomic::{AtomicUsize, Ordering},
    time::Duration,
};

use ostd::{
    mm::VmIo,
    sync::{LocalIrqDisabled, Waiter, Waker},
};

use super::{
    DriHandle,
    fence::{Fence, FenceCallbackRegistration, FenceChainSlot},
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
pub(super) const DRM_SYNCOBJ_FD_TIMELINE: u32 = 1 << 1;
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
    pub point: u64,
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
    fence_callbacks: SpinLock<Vec<FenceCallbackRegistration>, LocalIrqDisabled>,
    event_watchers: SpinLock<EventWatcherList, LocalIrqDisabled>,
}

impl Debug for SyncObject {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("SyncObject").finish_non_exhaustive()
    }
}

impl SyncObject {
    pub(super) fn new() -> Arc<Self> {
        Self::with_initial_signal(false)
    }

    pub(super) fn new_signaled() -> Arc<Self> {
        Self::with_initial_signal(true)
    }

    fn with_initial_signal(signaled: bool) -> Arc<Self> {
        let signaled_fence = Fence::new_signaled();
        let payload = signaled.then(|| SyncPayload::Binary(signaled_fence.clone()));
        Arc::new(Self {
            state: SpinLock::new(SyncObjectState {
                payload,
                ..Default::default()
            }),
            signaled_fence,
            watchers: SpinLock::new(Vec::new()),
            fence_callbacks: SpinLock::new(Vec::new()),
            event_watchers: SpinLock::new(EventWatcherList::default()),
        })
    }

    pub(super) fn replace_fence(self: &Arc<Self>, fence: Option<Arc<Fence>>) {
        let old_callbacks = mem::take(&mut *self.fence_callbacks.lock());
        drop(old_callbacks);
        if let Some(fence) = fence.as_ref() {
            self.arm_fence(fence);
        }
        let mut state = self.state.lock();
        state.payload = fence.map(SyncPayload::Binary);
        state.points.clear();
        drop(state);
        self.notify_watchers(true);
    }

    pub(super) fn signal_binary(self: &Arc<Self>) {
        self.replace_fence(Some(self.signaled_fence.clone()));
    }

    /// Adds a fence to a timeline. Point zero has binary replacement semantics.
    pub(super) fn add_point(self: &Arc<Self>, point: u64, fence: Arc<Fence>) -> Result<()> {
        if point == 0 {
            self.replace_fence(Some(fence));
            return Ok(());
        }

        let chain_slot = Fence::reserve_chain_slot()?;
        self.poll_timeline_completion();
        let mut state = self.state.lock();
        refresh_timeline(&mut state);
        reserve_point_storage(&mut state, 1)?;
        let previous = current_fence(&state);
        let chained = Fence::chain(previous, fence, chain_slot);
        append_timeline_point(&mut state, point, chained.clone());
        drop(state);
        self.arm_fence(&chained);
        self.notify_watchers(true);
        Ok(())
    }

    /// Prepares one binary or timeline publication before a GPU submission.
    pub(super) fn reserve_publication(self: &Arc<Self>, point: u64) -> Result<SyncobjPublication> {
        if point == 0 {
            return Ok(SyncobjPublication {
                syncobj: self.clone(),
                kind: SyncobjPublicationKind::Binary,
            });
        }

        let chain_slot = Fence::reserve_chain_slot()?;
        self.poll_timeline_completion();
        let mut state = self.state.lock();
        refresh_timeline(&mut state);
        reserve_point_storage(&mut state, 1)?;
        state.reserved_point_count += 1;
        Ok(SyncobjPublication {
            syncobj: self.clone(),
            kind: SyncobjPublicationKind::Timeline {
                point,
                chain_slot: Some(chain_slot),
                active: true,
            },
        })
    }

    fn publish_reserved_point(
        self: &Arc<Self>,
        point: u64,
        fence: Arc<Fence>,
        chain_slot: FenceChainSlot,
    ) {
        self.poll_timeline_completion();
        let mut state = self.state.lock();
        refresh_timeline(&mut state);
        debug_assert!(state.reserved_point_count > 0);
        state.reserved_point_count -= 1;
        let previous = current_fence(&state);
        let chained = Fence::chain(previous, fence, chain_slot);
        append_timeline_point(&mut state, point, chained.clone());
        drop(state);
        self.arm_fence(&chained);
        self.notify_watchers(true);
    }

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

    pub(super) fn query_point(&self, last_submitted: bool) -> u64 {
        self.poll_timeline_completion();
        let mut state = self.state.lock();
        refresh_timeline(&mut state);
        match state.payload.as_ref() {
            Some(SyncPayload::Timeline {
                signaled_point,
                last_submitted: submitted,
                ..
            }) => {
                if last_submitted {
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
        wait_for_submit: bool,
    ) -> Result<Arc<Fence>> {
        if let Some(fence) = self.find_fence(point) {
            return Ok(fence);
        }
        if !wait_for_submit {
            return_errno_with_message!(Errno::EINVAL, "syncobj fence has not been submitted");
        }

        let (waiter, _) = Waiter::new_pair();
        self.register_waker(&waiter.waker());
        waiter.pause_until_or_timeout(|| self.find_fence(point), Some(&WAIT_FOR_SUBMIT_TIMEOUT))
    }

    fn arm_fence(self: &Arc<Self>, fence: &Arc<Fence>) {
        let weak = Arc::downgrade(self);
        let registration = fence.on_signal(move || {
            if let Some(syncobj) = weak.upgrade() {
                syncobj.notify_watchers(false);
            }
        });
        if let Some(registration) = registration {
            let mut callbacks = self.fence_callbacks.lock();
            callbacks.retain(FenceCallbackRegistration::is_active);
            callbacks.push(registration);
        }
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

    fn register_waker(&self, waker: &Arc<Waker>) {
        let mut watchers = self.watchers.lock();
        watchers.retain(|weak| weak.strong_count() != 0);
        if watchers
            .iter()
            .filter_map(Weak::upgrade)
            .any(|registered| Arc::ptr_eq(&registered, waker))
        {
            return;
        }
        watchers.push(Arc::downgrade(waker));
    }

    fn notify_watchers(&self, poll_device: bool) {
        let mut watchers = self.watchers.lock();
        watchers.retain(|weak| {
            let Some(watcher) = weak.upgrade() else {
                return false;
            };
            watcher.wake_up();
            true
        });
        drop(watchers);
        self.notify_event_watchers(poll_device);
    }

    fn register_event_watcher(
        &self,
        point: u64,
        available_only: bool,
        event_file: Arc<dyn FileLike>,
    ) -> Result<()> {
        let quota = EventWatcherQuota::reserve()?;
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
        let watcher = Arc::new(SyncobjEventWatcher {
            generation,
            point,
            available_only,
            event_file,
            _quota: quota,
        });
        watchers.entries.push(watcher.clone());
        drop(watchers);
        // Only the newly registered watcher needs a readiness check. Scanning
        // every older watcher here would make N registrations O(N^2).
        let readiness = self.event_readiness(true);
        self.notify_event_watcher(&watcher, readiness);
        Ok(())
    }

    fn notify_event_watchers(&self, poll_device: bool) {
        // Watchers registered after this cutoff perform their own readiness
        // check and must not consume this potentially stale snapshot.
        let generation_cutoff = self.event_watchers.lock().next_generation;
        let readiness = self.event_readiness(poll_device);
        self.event_watchers.lock().entries.retain(|watcher| {
            if watcher.generation < generation_cutoff
                && readiness.is_ready(watcher.point, watcher.available_only)
            {
                watcher
                    .event_file
                    .downcast_ref::<crate::syscall::EventFile>()
                    .expect("syncobj event watcher lost its eventfd type")
                    .signal();
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
                watcher
                    .event_file
                    .downcast_ref::<crate::syscall::EventFile>()
                    .expect("syncobj event watcher lost its eventfd type")
                    .signal();
            }
        }
    }

    fn event_readiness(&self, poll_device: bool) -> EventReadiness {
        let current = {
            let state = self.state.lock();
            current_fence(&state)
        };
        if poll_device {
            if let Some(fence) = current.as_ref() {
                fence.poll_and_is_signaled();
            }
        }

        let mut state = self.state.lock();
        refresh_timeline(&mut state);
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
    event_file: Arc<dyn FileLike>,
    _quota: EventWatcherQuota,
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
    Binary,
    Timeline {
        point: u64,
        chain_slot: Option<FenceChainSlot>,
        active: bool,
    },
}

impl SyncobjPublication {
    pub(super) fn publish(mut self, fence: Arc<Fence>) {
        match &mut self.kind {
            SyncobjPublicationKind::Binary => self.syncobj.replace_fence(Some(fence)),
            SyncobjPublicationKind::Timeline {
                point,
                chain_slot,
                active,
            } => {
                self.syncobj.publish_reserved_point(
                    *point,
                    fence,
                    chain_slot
                        .take()
                        .expect("syncobj publication lost its fence slot"),
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
    if pointer == 0 {
        return Ok(alloc::vec![0; validate_count(count)?]);
    }
    read_array::<8, u64>(pointer, count, u64::from_le_bytes)
}

fn read_array<const N: usize, T>(
    pointer: u64,
    count: u32,
    decode: impl Fn([u8; N]) -> T,
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
    let mut bytes = alloc::vec![0; byte_len];
    current_userspace!().read_bytes(pointer as usize, &mut bytes)?;
    Ok(bytes
        .as_chunks::<N>()
        .0
        .iter()
        .map(|bytes| decode(*bytes))
        .collect())
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
    handles
        .iter()
        .map(|id| {
            inner
                .syncobjs
                .get(id)
                .cloned()
                .ok_or_else(|| Error::with_message(Errno::ENOENT, "unknown syncobj handle"))
        })
        .collect()
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
        SyncObject::new_signaled()
    } else {
        SyncObject::new()
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
    let points = alloc::vec![0; syncobjs.len()];
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
        syncobj.replace_fence(None);
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
    for (syncobj, point) in syncobjs.iter().zip(points) {
        syncobj.add_signaled_point(point)?;
    }
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
    let last_submitted = req.flags & DRM_SYNCOBJ_QUERY_LAST_SUBMITTED != 0;
    let mut output = Vec::new();
    output
        .try_reserve_exact(syncobjs.len() * size_of::<u64>())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate syncobj query output"))?;
    for syncobj in syncobjs {
        output.extend_from_slice(&syncobj.query_point(last_submitted).to_le_bytes());
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
    let source =
        objects[0].wait_for_fence(req.src_point, req.flags & DRM_SYNCOBJ_WAIT_FOR_SUBMIT != 0)?;
    if req.dst_point == 0 {
        objects[1].replace_fence(Some(source));
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
        let valid = DRM_SYNCOBJ_FD_EXPORT_SYNC_FILE | DRM_SYNCOBJ_FD_TIMELINE;
        if req.pad != 0 || req.flags & !valid != 0 {
            return_errno_with_message!(Errno::EINVAL, "invalid syncobj export request");
        }
        let syncobj = lookup_syncobjs(handle, &[req.handle])?.remove(0);
        let file: Arc<dyn FileLike> = if req.flags & DRM_SYNCOBJ_FD_EXPORT_SYNC_FILE != 0 {
            let point = if req.flags & DRM_SYNCOBJ_FD_TIMELINE != 0 {
                req.point
            } else {
                0
            };
            Arc::new(super::fence::FenceFile::new(
                syncobj.wait_for_fence(point, false)?,
            ))
        } else {
            if req.point != 0 {
                return_errno_with_message!(Errno::EINVAL, "syncobj fd export has a timeline point");
            }
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
        let valid = DRM_SYNCOBJ_FD_IMPORT_SYNC_FILE | DRM_SYNCOBJ_FD_TIMELINE;
        if req.pad != 0 || req.flags & !valid != 0 {
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
            let point = if req.flags & DRM_SYNCOBJ_FD_TIMELINE != 0 {
                req.point
            } else {
                0
            };
            if point == 0 {
                syncobj.replace_fence(Some(fence));
            } else {
                syncobj.add_point(point, fence)?;
            }
            cmd.write(&req)?;
            return Ok(0);
        }

        if req.point != 0 {
            return_errno_with_message!(Errno::EINVAL, "syncobj fd import has a timeline point");
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
        if file.downcast_ref::<crate::syscall::EventFile>().is_none() {
            return_errno_with_message!(Errno::EINVAL, "syncobj event target is not an eventfd");
        }
        syncobj.register_event_watcher(
            req.point,
            req.flags & DRM_SYNCOBJ_WAIT_AVAILABLE != 0,
            file,
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
    if !may_wait_for_submit
        && syncobjs
            .iter()
            .zip(points)
            .any(|(syncobj, point)| syncobj.find_fence(*point).is_none())
    {
        return_errno_with_message!(Errno::EINVAL, "syncobj fence has not been submitted");
    }

    let wait_all = flags & DRM_SYNCOBJ_WAIT_ALL != 0;
    let available_only = flags & DRM_SYNCOBJ_WAIT_AVAILABLE != 0;
    let (waiter, _) = Waiter::new_pair();
    let waker = waiter.waker();
    for syncobj in syncobjs {
        syncobj.register_waker(&waker);
    }

    let condition = || {
        let mut first = None;
        let mut ready_count = 0;
        for (index, (syncobj, point)) in syncobjs.iter().zip(points).enumerate() {
            let ready = syncobj
                .find_fence(*point)
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
    waiter.pause_until_or_timeout(condition, timeout)
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

    #[ktest]
    fn binary_reset_and_signal() {
        let syncobj = SyncObject::new();
        assert!(syncobj.find_fence(0).is_none());
        syncobj.signal_binary();
        assert!(syncobj.find_fence(0).unwrap().is_signaled());
        syncobj.replace_fence(None);
        assert!(syncobj.find_fence(0).is_none());
    }

    #[ktest]
    fn timeline_preserves_previous_dependency() {
        let first = Arc::new(Fence::new());
        let syncobj = SyncObject::new();
        syncobj.add_point(4, first.clone()).unwrap();
        syncobj.add_signaled_point(9).unwrap();
        assert_eq!(syncobj.query_point(false), 0);
        assert!(!syncobj.find_fence(9).unwrap().is_signaled());
        first.signal_success();
        assert!(syncobj.find_fence(9).unwrap().is_signaled());
        assert_eq!(syncobj.query_point(false), 9);
        assert_eq!(syncobj.query_point(true), 9);
    }

    #[ktest]
    fn timeline_reservation_publishes_without_growing_storage() {
        let syncobj = SyncObject::new();
        let reservation = syncobj.reserve_publication(9).unwrap();
        {
            let state = syncobj.state.lock();
            assert_eq!(state.reserved_point_count, 1);
            assert!(state.points.capacity() >= state.points.len() + 1);
        }

        // A normal producer may append while the GPU submission owns its
        // reservation; it must preserve enough capacity for both points.
        let first = Arc::new(Fence::new());
        syncobj.add_point(4, first.clone()).unwrap();
        {
            let state = syncobj.state.lock();
            assert_eq!(state.reserved_point_count, 1);
            assert!(state.points.capacity() >= state.points.len() + 1);
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
        let syncobj = SyncObject::new();
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
    fn wait_for_submit_wakes_on_future_fence() {
        let syncobj = SyncObject::new();
        let finished = Arc::new(AtomicBool::new(false));
        let waiter = {
            let syncobj = syncobj.clone();
            let finished = finished.clone();
            ThreadOptions::new(move || {
                syncobj.wait_for_fence(0, true).unwrap();
                finished.store(true, Ordering::Release);
            })
            .spawn()
        };
        while syncobj.watchers.lock().is_empty() {
            ostd::task::Task::yield_now();
        }
        syncobj.signal_binary();
        waiter.join();
        assert!(finished.load(Ordering::Acquire));
    }

    #[ktest]
    fn in_flight_wait_retains_syncobj_after_original_owner_drops() {
        let original = SyncObject::new();
        let shared = original.clone();
        let weak = Arc::downgrade(&original);
        let finished = Arc::new(AtomicBool::new(false));
        let waiter = {
            let syncobj = original.clone();
            let finished = finished.clone();
            ThreadOptions::new(move || {
                syncobj.wait_for_fence(0, true).unwrap();
                finished.store(true, Ordering::Release);
            })
            .spawn()
        };
        while original.watchers.lock().is_empty() {
            ostd::task::Task::yield_now();
        }

        drop(original);
        assert!(weak.upgrade().is_some());
        shared.signal_binary();
        waiter.join();
        assert!(finished.load(Ordering::Acquire));
    }
}
