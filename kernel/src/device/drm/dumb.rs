// SPDX-License-Identifier: MPL-2.0

//! Dumb-buffer allocation, mapping, and destruction.
//!
//! Dumb buffers are carved from the shared pool in [`super::GpuManager`].
//! Each allocation is wrapped in a [`super::GemObject`] and assigned a
//! per-file handle.

use align_ext::AlignExt;

use super::{
    DUMB_POOL_SIZE, DrmModeCreateDumb, DrmModeDestroyDumb, DrmModeMapDumb, DumbBuffer,
    gem::{GemObjectRef, PendingGemHandle},
};
use crate::prelude::*;

/// A serialized dumb-pool reservation that is not yet visible to userspace.
pub(super) struct PendingPoolAllocation<'a> {
    cursor: MutexGuard<'a, usize>,
    offset: usize,
    published: bool,
}

/// A dumb buffer whose handle and pool span are not yet visible to userspace.
pub(super) struct PendingDumbBuffer<'a> {
    handle: PendingGemHandle<'a>,
    allocation: PendingPoolAllocation<'a>,
}

impl<'a> PendingDumbBuffer<'a> {
    pub(super) fn new(handle: PendingGemHandle<'a>, allocation: PendingPoolAllocation<'a>) -> Self {
        PendingDumbBuffer { handle, allocation }
    }

    pub(super) fn id(&self) -> u32 {
        self.handle.id()
    }

    /// Publishes the handle and commits its pool allocation.
    pub(super) fn publish(self) {
        self.handle.publish();
        self.allocation.publish();
    }

    /// Discards the handle and rolls back or quarantines its pool span.
    pub(super) fn discard_after_failed_resource(self) -> Result<()> {
        let Self { handle, allocation } = self;
        match handle.discard() {
            Ok(cleanup_status) => {
                allocation.finish_cleanup(cleanup_status);
                Ok(())
            }
            Err(error) => {
                allocation.finish_cleanup(super::HostCleanupStatus::Unconfirmed);
                Err(error)
            }
        }
    }
}

impl<'a> PendingPoolAllocation<'a> {
    pub(super) fn new(manager: &'a super::GpuManager, size: usize) -> Result<Self> {
        Self::reserve(&manager.next_offset, size)
    }

    fn reserve(cursor: &'a Mutex<usize>, size: usize) -> Result<Self> {
        let mut cursor = cursor.lock();
        let offset = cursor.align_up(PAGE_SIZE);
        let end = offset
            .checked_add(size)
            .ok_or_else(|| Error::with_message(Errno::ENOMEM, "dumb buffer size overflows"))?;
        if end > DUMB_POOL_SIZE {
            return_errno_with_message!(Errno::ENOMEM, "dumb buffer pool is exhausted");
        }
        *cursor = end.align_up(PAGE_SIZE);
        Ok(Self {
            cursor,
            offset,
            published: false,
        })
    }

    pub(super) fn offset(&self) -> usize {
        self.offset
    }

    pub(super) fn publish(mut self) {
        self.published = true;
    }

    fn finish_cleanup(self, cleanup_status: super::HostCleanupStatus) {
        match cleanup_status {
            super::HostCleanupStatus::Confirmed => drop(self),
            super::HostCleanupStatus::Unconfirmed => self.publish(),
        }
    }
}

impl Drop for PendingPoolAllocation<'_> {
    fn drop(&mut self) {
        if !self.published {
            *self.cursor = self.offset;
        }
    }
}

/// Creates a dumb buffer, allocating from the global pool and wrapping it
/// in a GEM object.
pub(super) fn create_dumb<'a>(
    handle: &'a super::DriHandle,
    req: &DrmModeCreateDumb,
) -> Result<(DrmModeCreateDumb, PendingDumbBuffer<'a>)> {
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

    let allocation = PendingPoolAllocation::new(&handle.gpu_manager, size)?;
    let offset = allocation.offset();

    let object = GemObjectRef::insert_new(
        &handle.gpu_manager,
        DumbBuffer {
            offset,
            size,
            width: req.width,
            height: req.height,
            bpp: req.bpp,
        },
    )?;
    let pending = PendingGemHandle::new(handle, object)?;

    Ok((
        DrmModeCreateDumb {
            handle: pending.id(),
            pitch,
            size: size as u64,
            ..*req
        },
        PendingDumbBuffer::new(pending, allocation),
    ))
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
    super::gem::gem_close(handle, req.handle)
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;
    use crate::device::drm::HostCleanupStatus;

    #[ktest]
    fn pending_pool_allocation_excludes_interleaving_before_rollback() {
        let cursor = Mutex::new(0);
        let pending = PendingPoolAllocation::reserve(&cursor, PAGE_SIZE).unwrap();

        assert!(cursor.try_lock().is_none());
        pending.finish_cleanup(HostCleanupStatus::Confirmed);
        assert_eq!(*cursor.lock(), 0);
    }

    #[ktest]
    fn pending_pool_allocation_quarantines_unconfirmed_host_cleanup() {
        // Regression for reusing pages after a failed virtio-gpu RESOURCE_UNREF.
        let cursor = Mutex::new(0);
        let pending = PendingPoolAllocation::reserve(&cursor, PAGE_SIZE).unwrap();

        pending.finish_cleanup(HostCleanupStatus::Unconfirmed);
        assert_eq!(*cursor.lock(), PAGE_SIZE);
    }
}
