// SPDX-License-Identifier: MPL-2.0

//! GEM (Graphics Execution Manager) object operations.
//!
//! Each GEM object wraps a dumb-buffer allocation.
//! Objects live in the global [`super::GpuManager`] table and are referenced by per-file handles.
//! GEM_FLINK uses the object's own id as its global name.
//! [`PendingGemHandle`] stages a per-file handle until its creating ioctl copies the response.

use core::{
    mem,
    sync::atomic::{AtomicU32, Ordering},
};

use crate::prelude::*;

/// An owned reference to a global GEM object.
pub(super) struct GemObjectRef<'a> {
    manager: &'a super::GpuManager,
    object_id: u32,
    buffer: super::DumbBuffer,
}

impl<'a> GemObjectRef<'a> {
    /// Allocates and inserts a new object, taking its initial reference.
    pub(super) fn insert_new(
        manager: &'a super::GpuManager,
        buffer: super::DumbBuffer,
    ) -> Result<Self> {
        let object_id = {
            let mut objects = manager.gem_objects.lock();
            let object_id = manager
                .next_gem_id
                .try_update(Ordering::Relaxed, Ordering::Relaxed, |id| id.checked_add(1))
                .map_err(|_| Error::with_message(Errno::ENOSPC, "GEM object space is exhausted"))?;
            if objects.contains_key(&object_id) {
                return_errno_with_message!(Errno::ENOSPC, "GEM object id is already allocated");
            }
            objects.insert(
                object_id,
                Arc::new(super::GemObject {
                    name: AtomicU32::new(0),
                    ref_count: AtomicU32::new(1),
                    buffer,
                }),
            );
            manager.gem_references.fetch_add(1, Ordering::Relaxed);
            object_id
        };
        Ok(Self {
            manager,
            object_id,
            buffer,
        })
    }

    /// Retains an existing object and owns the new reference.
    pub(super) fn retain(manager: &'a super::GpuManager, object_id: u32) -> Result<Self> {
        let buffer = manager.retain_gem_object(object_id)?;
        Ok(Self {
            manager,
            object_id,
            buffer,
        })
    }

    pub(super) fn object_id(&self) -> u32 {
        self.object_id
    }

    pub(super) fn buffer(&self) -> super::DumbBuffer {
        self.buffer
    }

    /// Transfers the owned reference into another lifetime container.
    fn into_raw(self) -> u32 {
        let object_id = self.object_id;
        mem::forget(self);
        object_id
    }
}

impl Drop for GemObjectRef<'_> {
    fn drop(&mut self) {
        if let Err(error) = self.manager.release_gem_object(self.object_id) {
            ostd::warn!(
                "cannot release unpublished GEM object {}: {:?}",
                self.object_id,
                error
            );
        }
    }
}

/// A GEM handle whose number is reserved but not visible to other ioctl calls.
///
/// [`Self::publish`] transfers the owned object reference to the per-file table.
/// Dropping an unpublished handle releases its object reference.
/// This makes userspace copyout failure transactional.
pub(super) struct PendingGemHandle<'a> {
    owner: &'a super::DriHandle,
    gem_handle: u32,
    object: Option<GemObjectRef<'a>>,
}

impl<'a> PendingGemHandle<'a> {
    /// Reserves a handle number for an owned object reference.
    pub(super) fn new(owner: &'a super::DriHandle, object: GemObjectRef<'a>) -> Result<Self> {
        let gem_handle = owner.reserve_gem_handle(object.object_id())?;
        Ok(Self {
            owner,
            gem_handle,
            object: Some(object),
        })
    }

    pub(super) fn id(&self) -> u32 {
        self.gem_handle
    }

    /// Makes the reserved handle visible and transfers its object reference.
    pub(super) fn publish(mut self) {
        let object = self.object.take().unwrap();
        let object_id = object.into_raw();
        let previous = self
            .owner
            .inner
            .lock()
            .handles
            .insert(self.gem_handle, object_id);
        debug_assert!(previous.is_none());
    }

    /// Discards the reserved handle and reports final host-resource cleanup.
    pub(super) fn discard(mut self) -> Result<super::HostCleanupStatus> {
        let object = self.object.take().unwrap();
        let object_id = object.into_raw();
        let detach_result = self.owner.release_gem_handle_reference(object_id);
        let cleanup_result = self
            .owner
            .gpu_manager
            .release_gem_object_and_report_cleanup(object_id);
        detach_result?;
        cleanup_result
    }
}

impl Drop for PendingGemHandle<'_> {
    fn drop(&mut self) {
        let Some(object) = self.object.as_ref() else {
            return;
        };
        if let Err(error) = self.owner.release_gem_handle_reference(object.object_id()) {
            ostd::warn!(
                "cannot release reserved GEM handle {}: {:?}",
                self.gem_handle,
                error
            );
        }
    }
}

/// GEM_CLOSE: drop a per-file handle, decrementing the object's ref count.
pub(super) fn gem_close(handle: &super::DriHandle, gem_handle: u32) -> Result<()> {
    let _cursor_operation = handle.cursor_operation.lock();
    let object_id = {
        let mut inner = handle.inner.lock();
        if inner.cursor.uses_handle(gem_handle) {
            return_errno_with_message!(Errno::EBUSY, "GEM object is active as the cursor");
        }
        inner
            .handles
            .remove(&gem_handle)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?
    };

    let detach_result = handle.release_gem_handle_reference(object_id);
    let release_result = handle.gpu_manager.release_gem_object(object_id);
    detach_result?;
    release_result
}

/// GEM_FLINK: return the object's id as a global 32-bit name.
pub(super) fn gem_flink(handle: &super::DriHandle, gem_handle: u32) -> Result<u32> {
    let inner = handle.inner.lock();
    let object_id = inner
        .handles
        .get(&gem_handle)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?;
    let object_id = *object_id;

    let guard = handle.gpu_manager.gem_objects.lock();
    let obj = guard
        .get(&object_id)
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;

    // Use the object's id as its global name.
    let name = obj.name.load(Ordering::Relaxed);
    if name != 0 {
        return Ok(name);
    }

    // Store object_id as the name (name == object_id simplifies lookup).
    obj.name.store(object_id, Ordering::Relaxed);
    let mut names = handle.gpu_manager.gem_names.lock();
    names.insert(object_id, object_id);
    Ok(object_id)
}

/// GEM_OPEN: look up a global name and create a per-file handle.
pub(super) fn gem_open<'a>(
    handle: &'a super::DriHandle,
    name: u32,
) -> Result<(PendingGemHandle<'a>, u64)> {
    let names = handle.gpu_manager.gem_names.lock();
    let object_id = names
        .get(&name)
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "unknown GEM name"))?;
    let object_id = *object_id;
    drop(names);

    let object = GemObjectRef::retain(&handle.gpu_manager, object_id)?;
    let size = object.buffer().size as u64;
    let pending = PendingGemHandle::new(handle, object)?;
    Ok((pending, size))
}
