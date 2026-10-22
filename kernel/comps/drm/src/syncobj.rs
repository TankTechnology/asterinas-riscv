// SPDX-License-Identifier: MPL-2.0

//! DRM sync objects.
//!
//! A sync object is a handle to a fence that userspace can wait on and
//! signal. The fence itself is [`crate::fence::DrmFence`], which is also what
//! sync-file descriptors carry, so an object can be exported as a file
//! descriptor and imported back without copying state.

use alloc::{collections::BTreeMap, sync::Arc};

use aster_core::prelude::*;
use sparse_id_alloc::SparseIdAlloc;

use crate::fence::DrmFence;

/// A sync object, shared by every handle that refers to it.
///
/// `binary_point` turns the fence into the binary view an imported sync file
/// had: the object counts as signaled once the fence reaches that point. A
/// plain object uses point zero.
pub(crate) struct DrmSyncObj {
    fence: Arc<DrmFence>,
    /// Imported with the TIMELINE flag: waits are point-based.
    timeline: bool,
}

impl core::fmt::Debug for DrmSyncObj {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("DrmSyncObj")
            .field("fence", &self.fence)
            .field("timeline", &self.timeline)
            .finish_non_exhaustive()
    }
}

impl DrmSyncObj {
    /// Creates a sync object, optionally already signaled.
    pub(crate) fn new(signaled: bool) -> Self {
        Self {
            fence: if signaled {
                DrmFence::new_signaled()
            } else {
                DrmFence::new()
            },
            timeline: false,
        }
    }

    /// Creates a sync object backed by an existing fence (an imported one).
    pub(crate) fn from_fence(fence: Arc<DrmFence>, timeline: bool) -> Self {
        Self { fence, timeline }
    }

    pub(crate) fn fence(&self) -> Arc<DrmFence> {
        self.fence.clone()
    }

    /// Returns whether the object has been signaled.
    pub(crate) fn is_signaled(&self) -> bool {
        if self.timeline {
            self.fence.timeline_value() > 0
        } else {
            self.fence.is_signaled()
        }
    }

    /// Returns the current timeline point.
    pub(crate) fn timeline_value(&self) -> u64 {
        self.fence.timeline_value()
    }

    /// Returns whether a wait on `point` is satisfied.
    ///
    /// `None` is a binary wait; `Some(point)` is a timeline wait. Point zero is
    /// always considered signaled, matching Linux's timeline semantics.
    pub(crate) fn is_satisfied(&self, point: Option<u64>) -> bool {
        match point {
            None => self.is_signaled(),
            Some(point) => self.fence.is_signaled_at(point),
        }
    }

    /// Signals the object once (binary signal).
    pub(crate) fn signal(&self) {
        self.fence.signal();
    }

    /// Advances the timeline to `point`.
    pub(crate) fn signal_timeline(&self, point: u64) {
        self.fence.signal_timeline(point);
    }

    /// Clears both the binary signal and the timeline point.
    pub(crate) fn reset(&self) {
        self.fence.reset();
    }

    /// Registers a waker that is woken on the next state change.
    pub(crate) fn register_waker(&self, waker: Arc<ostd::sync::Waker>) {
        self.fence.register_waker(waker);
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
