// SPDX-License-Identifier: MPL-2.0

//! Dumb-buffer allocation, mapping, and destruction.
//!
//! Dumb buffers are carved from the shared pool in [`super::GpuManager`].
//! Each allocation is wrapped in a [`super::GemObject`] and assigned a
//! per-file handle.

use core::sync::atomic::{AtomicU32, Ordering};

use align_ext::AlignExt;

use super::{DUMB_POOL_SIZE, DrmModeCreateDumb, DrmModeDestroyDumb, DrmModeMapDumb, DumbBuffer};
use crate::prelude::*;

/// Creates a dumb buffer, allocating from the global pool and wrapping it
/// in a GEM object.
pub(super) fn create_dumb(
    handle: &super::DriHandle,
    req: &DrmModeCreateDumb,
) -> Result<DrmModeCreateDumb> {
    if req.flags != 0 {
        return_errno_with_message!(Errno::EINVAL, "unsupported dumb buffer flags");
    }
    let bytes_per_pixel = req.bpp.div_ceil(8);
    let pitch = req
        .width
        .checked_mul(bytes_per_pixel)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "dumb buffer width overflows"))?;
    let size = (pitch as usize)
        .checked_mul(req.height as usize)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "dumb buffer size overflows"))?;
    if size == 0 {
        return_errno_with_message!(Errno::EINVAL, "dumb buffer has zero size");
    }

    handle.gpu_manager.ensure_pool()?;

    let mut offset_guard = handle.gpu_manager.next_offset.lock();
    let offset = offset_guard.align_up(PAGE_SIZE);
    let end = offset
        .checked_add(size)
        .ok_or_else(|| Error::with_message(Errno::ENOMEM, "dumb buffer size overflows"))?;
    if end > DUMB_POOL_SIZE {
        return_errno_with_message!(Errno::ENOMEM, "dumb buffer pool is exhausted");
    }
    *offset_guard = end.align_up(PAGE_SIZE);
    drop(offset_guard);

    let object_id = handle
        .gpu_manager
        .next_gem_id
        .fetch_add(1, Ordering::Relaxed);
    let gem_obj = super::GemObject {
        name: AtomicU32::new(0),
        ref_count: AtomicU32::new(1),
        buffer: DumbBuffer {
            offset,
            size,
            width: req.width,
            height: req.height,
            bpp: req.bpp,
        },
    };
    handle
        .gpu_manager
        .gem_objects
        .lock()
        .insert(object_id, Arc::new(gem_obj));

    let mut inner = handle.inner.lock();
    let gem_handle = inner.next_handle;
    inner.next_handle += 1;
    inner.handles.insert(gem_handle, object_id);

    Ok(DrmModeCreateDumb {
        handle: gem_handle,
        pitch,
        size: size as u64,
        ..*req
    })
}

/// Returns the byte offset of a dumb buffer within the pool for mmap.
pub(super) fn map_dumb(handle: &super::DriHandle, req: &DrmModeMapDumb) -> Result<DrmModeMapDumb> {
    let inner = handle.inner.lock();
    let object_id = inner
        .handles
        .get(&req.handle)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown dumb buffer handle"))?;
    let guard = handle.gpu_manager.gem_objects.lock();
    let obj = guard
        .get(object_id)
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;
    Ok(DrmModeMapDumb {
        offset: obj.buffer.offset as u64,
        ..*req
    })
}

/// Destroys a dumb buffer (removes the per-file handle, drops the GEM object).
pub(super) fn destroy_dumb(handle: &super::DriHandle, req: &DrmModeDestroyDumb) -> Result<()> {
    let _cursor_operation = handle.cursor_operation.lock();
    let mut inner = handle.inner.lock();
    if inner.cursor.uses_handle(req.handle) {
        return_errno_with_message!(Errno::EBUSY, "dumb buffer is active as the cursor");
    }
    if inner.handles.remove(&req.handle).is_none() {
        return_errno_with_message!(Errno::EINVAL, "unknown dumb buffer handle");
    }
    // The freed pool space is intentionally not reclaimed: the pool is a
    // bump allocator, so a destroyed buffer's span is simply leaked within
    // the pool. Fine for the handful of buffers a client allocates.
    Ok(())
}
