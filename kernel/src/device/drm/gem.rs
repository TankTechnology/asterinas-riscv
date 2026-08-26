// SPDX-License-Identifier: MPL-2.0

//! GEM (Graphics Execution Manager) object operations.
//!
//! Each GEM object wraps a dumb-buffer allocation. Objects live in the
//! global [`super::GpuManager`] table and are referenced by per-file
//! handles. GEM_FLINK uses the object's own id as its global name.

use core::sync::atomic::Ordering;

use crate::prelude::*;

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
pub(super) fn gem_open(handle: &super::DriHandle, name: u32) -> Result<(u32, u64)> {
    let names = handle.gpu_manager.gem_names.lock();
    let object_id = names
        .get(&name)
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "unknown GEM name"))?;
    let object_id = *object_id;
    drop(names);

    let size = handle.gpu_manager.retain_gem_object(object_id)?.size as u64;

    let mut inner = handle.inner.lock();
    let new_handle = inner.next_handle;
    inner.next_handle += 1;
    inner.handles.insert(new_handle, object_id);

    Ok((new_handle, size))
}

/// Removes a newly-created handle whose ioctl response could not be published.
pub(super) fn rollback_handle(handle: &super::DriHandle, gem_handle: u32) {
    let object_id = handle.inner.lock().handles.remove(&gem_handle);
    if let Some(object_id) = object_id
        && let Err(error) = handle.gpu_manager.release_gem_object(object_id)
    {
        ostd::warn!("cannot roll back GEM handle {}: {:?}", gem_handle, error);
    }
}
