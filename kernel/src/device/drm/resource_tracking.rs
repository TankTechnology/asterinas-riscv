// SPDX-License-Identifier: MPL-2.0

//! Device-wide DRM resource-lifetime diagnostics.

use core::sync::atomic::{AtomicU64, Ordering};

use super::retry_pending_ids;
use crate::prelude::*;

/// Authoritative software view of host-created virgl contexts.
///
/// Device I/O is completed before these sets are updated. Failed destruction
/// leaves the context in `contexts` and records its ID for a later retry, so
/// diagnostics distinguish confirmed release from uncertain host lifetime.
pub(super) struct VirglContextTracker {
    contexts: SpinLock<BTreeMap<u32, BTreeSet<u32>>>,
    pending_cleanup: SpinLock<BTreeSet<u32>>,
    attachment_count: AtomicU64,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(super) struct VirglContextCounts {
    pub(super) contexts: usize,
    pub(super) attachments: u64,
    pub(super) pending_cleanup: usize,
}

/// Device-wide DRM and virtio-gpu resource counts exposed through fdinfo.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(super) struct DrmResourceSnapshot {
    pub(super) dumb_pool_used_bytes: usize,
    pub(super) dumb_pool_capacity_bytes: usize,
    pub(super) gem_objects: usize,
    pub(super) gem_references: u64,
    pub(super) flink_names: usize,
    pub(super) live_host_resources: usize,
    pub(super) cleanup_only_host_resources: usize,
    pub(super) pending_resource_cleanup: usize,
    pub(super) virgl_contexts: usize,
    pub(super) context_attachments: u64,
    pub(super) pending_context_cleanup: usize,
    pub(super) tracked_fences: usize,
    pub(super) fence_associations: u64,
    pub(super) backend_backing_owners: usize,
    pub(super) backend_pending_cleanup: usize,
    pub(super) scanout_resources: usize,
    pub(super) cursor_resources: usize,
}

impl VirglContextTracker {
    pub(super) fn new() -> Self {
        Self {
            contexts: SpinLock::new(BTreeMap::new()),
            pending_cleanup: SpinLock::new(BTreeSet::new()),
            attachment_count: AtomicU64::new(0),
        }
    }

    pub(super) fn record_created(&self, context_id: u32) {
        let previous = self.contexts.lock().insert(context_id, BTreeSet::new());
        debug_assert!(previous.is_none());
    }

    pub(super) fn has_attachment(&self, context_id: u32, resource_id: u32) -> bool {
        self.contexts
            .lock()
            .get(&context_id)
            .is_some_and(|resources| resources.contains(&resource_id))
    }

    pub(super) fn record_attachment(&self, context_id: u32, resource_id: u32) {
        let inserted = self
            .contexts
            .lock()
            .get_mut(&context_id)
            .expect("host-created virgl context must be tracked")
            .insert(resource_id);
        debug_assert!(inserted);
        if inserted {
            self.attachment_count.fetch_add(1, Ordering::Relaxed);
        }
    }

    pub(super) fn record_detachment(&self, context_id: u32, resource_id: u32) {
        let removed = self
            .contexts
            .lock()
            .get_mut(&context_id)
            .expect("host-created virgl context must be tracked")
            .remove(&resource_id);
        debug_assert!(removed);
        if removed {
            self.attachment_count.fetch_sub(1, Ordering::Relaxed);
        }
    }

    pub(super) fn record_destroyed(&self, context_id: u32) {
        self.pending_cleanup.lock().remove(&context_id);
        let removed = self.contexts.lock().remove(&context_id);
        debug_assert!(removed.is_some());
        if let Some(resources) = removed {
            self.attachment_count
                .fetch_sub(resources.len() as u64, Ordering::Relaxed);
        }
    }

    pub(super) fn defer_destroy(&self, context_id: u32) {
        debug_assert!(self.contexts.lock().contains_key(&context_id));
        self.pending_cleanup.lock().insert(context_id);
    }

    pub(super) fn counts(&self) -> VirglContextCounts {
        VirglContextCounts {
            contexts: self.contexts.lock().len(),
            attachments: self.attachment_count.load(Ordering::Relaxed),
            pending_cleanup: self.pending_cleanup.lock().len(),
        }
    }

    pub(super) fn retry_pending(&self, mut try_destroy: impl FnMut(u32) -> bool) {
        retry_pending_ids(&self.pending_cleanup, |context_id| {
            if !try_destroy(context_id) {
                return false;
            }
            let removed = self.contexts.lock().remove(&context_id);
            debug_assert!(removed.is_some());
            if let Some(resources) = removed {
                self.attachment_count
                    .fetch_sub(resources.len() as u64, Ordering::Relaxed);
            }
            true
        });
    }
}
