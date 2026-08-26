// SPDX-License-Identifier: MPL-2.0

//! GEM (Graphics Execution Manager) object operations.
//!
//! Each GEM object wraps a dumb-buffer allocation. Objects live in the
//! global [`super::GpuManager`] table and are referenced by per-file
//! handles. GEM_FLINK uses the object's own id as its global name.

use core::sync::atomic::Ordering;

use crate::prelude::*;

/// A GEM handle whose number is reserved but not visible to other ioctl calls.
///
/// The owned object reference is transferred to the per-file handle table by
/// [`Self::publish`]. Dropping an unpublished handle releases that reference,
/// which makes userspace copyout failure transactional.
pub(super) struct PendingGemHandle<'a> {
    owner: &'a super::DriHandle,
    gem_handle: u32,
    object_id: u32,
    allocation: Option<(usize, usize)>,
    published: bool,
}

impl<'a> PendingGemHandle<'a> {
    /// Reserves a handle number for an object reference already owned by the caller.
    pub(super) fn new_owned(owner: &'a super::DriHandle, object_id: u32) -> Result<Self> {
        Self::new_owned_inner(owner, object_id, None)
    }

    /// Reserves a handle and owns a new bump allocation until publication.
    pub(super) fn new_allocated(
        owner: &'a super::DriHandle,
        object_id: u32,
        offset: usize,
        next_offset: usize,
    ) -> Result<Self> {
        Self::new_owned_inner(owner, object_id, Some((offset, next_offset)))
    }

    fn new_owned_inner(
        owner: &'a super::DriHandle,
        object_id: u32,
        allocation: Option<(usize, usize)>,
    ) -> Result<Self> {
        let gem_handle = {
            let mut inner = owner.inner.lock();
            let gem_handle = inner.next_handle;
            let Some(next_handle) = gem_handle.checked_add(1) else {
                drop(inner);
                Self::rollback_allocation(owner, allocation);
                if let Err(error) = owner.gpu_manager.release_gem_object(object_id) {
                    ostd::warn!(
                        "cannot release GEM object {} after handle exhaustion: {:?}",
                        object_id,
                        error
                    );
                }
                return_errno_with_message!(Errno::ENOSPC, "GEM handle space is exhausted");
            };
            inner.next_handle = next_handle;
            gem_handle
        };
        Ok(Self {
            owner,
            gem_handle,
            object_id,
            allocation,
            published: false,
        })
    }

    fn rollback_allocation(owner: &super::DriHandle, allocation: Option<(usize, usize)>) {
        if let Some((offset, next_offset)) = allocation {
            let mut allocation_cursor = owner.gpu_manager.next_offset.lock();
            if *allocation_cursor == next_offset {
                *allocation_cursor = offset;
            }
        }
    }

    pub(super) fn id(&self) -> u32 {
        self.gem_handle
    }

    /// Makes the reserved handle visible and transfers its object reference.
    pub(super) fn publish(mut self) {
        let previous = self
            .owner
            .inner
            .lock()
            .handles
            .insert(self.gem_handle, self.object_id);
        debug_assert!(previous.is_none());
        self.published = true;
    }
}

impl Drop for PendingGemHandle<'_> {
    fn drop(&mut self) {
        if self.published {
            return;
        }
        Self::rollback_allocation(self.owner, self.allocation);
        if let Err(error) = self.owner.gpu_manager.release_gem_object(self.object_id) {
            ostd::warn!(
                "cannot release unpublished GEM handle {}: {:?}",
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

    handle.gpu_manager.release_gem_object(object_id)
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

    let size = handle.gpu_manager.retain_gem_object(object_id)?.size as u64;
    let pending = PendingGemHandle::new_owned(handle, object_id)?;
    Ok((pending, size))
}
