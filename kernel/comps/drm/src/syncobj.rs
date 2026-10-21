// SPDX-License-Identifier: MPL-2.0

//! DRM sync objects.
//!
//! A sync object is a handle to a point that userspace can wait on and signal.
//! This implementation is the minimal surface a compositor needs: binary
//! signal/wait/reset and a timeline value that `SYNCOBJ_TIMELINE_SIGNAL`
//! advances and `SYNCOBJ_TIMELINE_WAIT` waits for. There are no fence imports
//! and no explicit-fencing integration yet, so a sync object only changes state
//! through these ioctls.

use alloc::{collections::BTreeMap, sync::Arc, vec::Vec};

use aster_core::prelude::*;
use ostd::sync::{Mutex, Waker};
use sparse_id_alloc::SparseIdAlloc;

/// The state of one sync object.
#[derive(Debug, Default)]
struct DrmSyncObjState {
    /// Whether a binary signal has been observed since the last reset.
    signaled: bool,
    /// The timeline point that has been signaled.
    timeline_value: u64,
}

/// A sync object, shared by every handle that refers to it.
pub(crate) struct DrmSyncObj {
    state: Mutex<DrmSyncObjState>,
    /// Tasks blocked in a wait ioctl on this object.
    waiters: Mutex<Vec<Arc<Waker>>>,
}

impl DrmSyncObj {
    /// Creates a sync object, optionally already signaled.
    pub(crate) fn new(signaled: bool) -> Self {
        Self {
            state: Mutex::new(DrmSyncObjState {
                signaled,
                timeline_value: u64::from(signaled),
            }),
            waiters: Mutex::new(Vec::new()),
        }
    }

    /// Returns whether the object has been signaled.
    pub(crate) fn is_signaled(&self) -> bool {
        self.state.lock().signaled
    }

    /// Returns the current timeline point.
    pub(crate) fn timeline_value(&self) -> u64 {
        self.state.lock().timeline_value
    }

    /// Returns whether a wait on `point` is satisfied.
    ///
    /// `None` is a binary wait; `Some(point)` is a timeline wait. Point zero is
    /// always considered signaled, matching Linux's timeline semantics.
    pub(crate) fn is_satisfied(&self, point: Option<u64>) -> bool {
        let state = self.state.lock();
        match point {
            None => state.signaled,
            Some(point) => point == 0 || point <= state.timeline_value,
        }
    }

    /// Signals the object once (binary signal).
    pub(crate) fn signal(&self) {
        {
            let mut state = self.state.lock();
            state.signaled = true;
            state.timeline_value = state.timeline_value.max(1);
        }
        self.wake_waiters();
    }

    /// Advances the timeline to `point`.
    pub(crate) fn signal_timeline(&self, point: u64) {
        {
            let mut state = self.state.lock();
            if point > state.timeline_value {
                state.timeline_value = point;
            }
            if point > 0 {
                state.signaled = true;
            }
        }
        self.wake_waiters();
    }

    /// Clears both the binary signal and the timeline point.
    pub(crate) fn reset(&self) {
        let mut state = self.state.lock();
        state.signaled = false;
        state.timeline_value = 0;
    }

    /// Registers a waker that is woken on the next state change.
    pub(crate) fn register_waker(&self, waker: Arc<Waker>) {
        self.waiters.lock().push(waker);
    }

    fn wake_waiters(&self) {
        let waiters = core::mem::take(&mut *self.waiters.lock());
        for waker in waiters {
            waker.wake_up();
        }
    }
}

/// The per-file table of sync-object handles.
///
/// Handles are scoped to one DRM file, like GEM handles.
pub(crate) struct DrmSyncObjTable {
    allocator: SparseIdAlloc,
    objects: BTreeMap<u32, Arc<DrmSyncObj>>,
}

impl core::fmt::Debug for DrmSyncObjTable {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("DrmSyncObjTable")
            .field("count", &self.objects.len())
            .finish_non_exhaustive()
    }
}

impl DrmSyncObjTable {
    pub(crate) fn new() -> Self {
        Self {
            // Sync-object handle zero is not special, but starting at one keeps
            // it consistent with the GEM handle table.
            allocator: SparseIdAlloc::new(1, u32::MAX),
            objects: BTreeMap::new(),
        }
    }

    pub(crate) fn insert(&mut self, object: Arc<DrmSyncObj>) -> Result<u32> {
        let Some(handle) = self.allocator.alloc() else {
            return_errno_with_message!(Errno::ENOMEM, "no sync-object handles are available");
        };
        let previous = self.objects.insert(handle, object);
        debug_assert!(previous.is_none(), "an allocated handle must be unused");
        Ok(handle)
    }

    pub(crate) fn lookup(&self, handle: u32) -> Result<Arc<DrmSyncObj>> {
        self.objects.get(&handle).cloned().ok_or_else(|| {
            Error::with_message(Errno::ENOENT, "the sync-object handle is invalid")
        })
    }

    pub(crate) fn remove(&mut self, handle: u32) -> Result<()> {
        if self.objects.remove(&handle).is_none() {
            return_errno_with_message!(Errno::ENOENT, "the sync-object handle is invalid");
        }
        self.allocator.free(handle);
        Ok(())
    }

    /// Drops every handle and resets the allocator.
    pub(crate) fn clear(&mut self) {
        self.allocator = SparseIdAlloc::new(1, u32::MAX);
        self.objects.clear();
    }
}
