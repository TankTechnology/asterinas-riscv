// SPDX-License-Identifier: MPL-2.0

//! Dumb-buffer allocation, mapping, and destruction.
//!
//! Dumb buffers are carved from the shared pool in [`super::GpuManager`].
//! Each allocation is wrapped in a [`super::GemObject`]
//! and assigned a per-file handle.

use super::{
    DrmModeCreateDumb, DrmModeDestroyDumb, DrmModeMapDumb, DumbBuffer,
    gem::{GemObjectRef, PendingGemHandle},
};
use crate::prelude::*;

#[derive(Debug)]
struct DumbPoolState {
    free_ranges: BTreeMap<usize, usize>,
    used_bytes: usize,
    high_water_bytes: usize,
}

/// Page-granular first-fit allocator for the shared contiguous VMO.
pub(super) struct DumbPool {
    capacity_bytes: usize,
    state: Mutex<DumbPoolState>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(super) struct DumbPoolUsage {
    used_bytes: usize,
    high_water_bytes: usize,
}

/// A pool span that returns itself to the allocator after every owner releases it.
pub(super) struct PoolAllocation {
    pool: Weak<DumbPool>,
    offset: usize,
    size_bytes: usize,
}

/// A dumb buffer whose handle and pool span are not yet visible to userspace.
pub(super) struct PendingDumbBuffer<'a> {
    handle: PendingGemHandle<'a>,
}

impl<'a> PendingDumbBuffer<'a> {
    pub(super) fn new(handle: PendingGemHandle<'a>) -> Self {
        PendingDumbBuffer { handle }
    }

    pub(super) fn id(&self) -> u32 {
        self.handle.id()
    }

    /// Publishes the handle and its pool allocation.
    pub(super) fn publish(self) {
        self.handle.publish();
    }
}

impl DumbPool {
    pub(super) fn new(capacity_bytes: usize) -> Arc<Self> {
        let mut free_ranges = BTreeMap::new();
        free_ranges.insert(0, capacity_bytes);
        Arc::new(Self {
            capacity_bytes,
            state: Mutex::new(DumbPoolState {
                free_ranges,
                used_bytes: 0,
                high_water_bytes: 0,
            }),
        })
    }

    pub(super) fn allocate(
        self: &Arc<Self>,
        requested_size_bytes: usize,
    ) -> Result<Arc<PoolAllocation>> {
        let allocated_size_bytes = requested_size_bytes
            .checked_add(PAGE_SIZE - 1)
            .and_then(|size| size.checked_div(PAGE_SIZE))
            .and_then(|pages| pages.checked_mul(PAGE_SIZE))
            .ok_or_else(|| Error::with_message(Errno::ENOMEM, "dumb buffer size overflows"))?;
        let mut state = self.state.lock();
        let selected = state
            .free_ranges
            .iter()
            .find_map(|(&start, &end)| {
                (end - start >= allocated_size_bytes).then_some((start, end))
            })
            .ok_or_else(|| Error::with_message(Errno::ENOMEM, "dumb buffer pool is exhausted"))?;
        let (offset, free_end) = selected;
        let end = offset.checked_add(allocated_size_bytes).ok_or_else(|| {
            Error::with_message(Errno::ENOMEM, "dumb buffer allocation overflows")
        })?;
        if end > self.capacity_bytes {
            return_errno_with_message!(Errno::ENOMEM, "dumb buffer pool is exhausted");
        }
        state.free_ranges.remove(&offset);
        if end < free_end {
            state.free_ranges.insert(end, free_end);
        }
        state.used_bytes += allocated_size_bytes;
        state.high_water_bytes = state.high_water_bytes.max(state.used_bytes);
        drop(state);
        Ok(Arc::new(PoolAllocation {
            pool: Arc::downgrade(self),
            offset,
            size_bytes: allocated_size_bytes,
        }))
    }

    pub(super) fn usage(&self) -> DumbPoolUsage {
        let state = self.state.lock();
        DumbPoolUsage {
            used_bytes: state.used_bytes,
            high_water_bytes: state.high_water_bytes,
        }
    }
}

impl PoolAllocation {
    pub(super) fn offset(&self) -> usize {
        self.offset
    }

    pub(super) fn size_bytes(&self) -> usize {
        self.size_bytes
    }
}

impl DumbPoolUsage {
    pub(super) fn used_bytes(self) -> usize {
        self.used_bytes
    }

    pub(super) fn high_water_bytes(self) -> usize {
        self.high_water_bytes
    }
}

impl Debug for PoolAllocation {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        formatter
            .debug_struct("PoolAllocation")
            .field("offset", &self.offset)
            .field("size_bytes", &self.size_bytes)
            .finish()
    }
}

impl Drop for PoolAllocation {
    fn drop(&mut self) {
        let Some(pool) = self.pool.upgrade() else {
            return;
        };
        let mut state = pool.state.lock();
        let mut start = self.offset;
        let mut end = self.offset + self.size_bytes;
        if let Some((&previous_start, &previous_end)) = state.free_ranges.range(..start).next_back()
            && previous_end == start
        {
            state.free_ranges.remove(&previous_start);
            start = previous_start;
        }
        if let Some((&next_start, &next_end)) = state.free_ranges.range(end..).next()
            && next_start == end
        {
            state.free_ranges.remove(&next_start);
            end = next_end;
        }
        let previous = state.free_ranges.insert(start, end);
        debug_assert!(previous.is_none());
        state.used_bytes -= self.size_bytes;
    }
}

/// Reserves and clears a shared-pool span before it can become visible.
pub(super) fn allocate_pool_span(
    manager: &super::GpuManager,
    size: usize,
) -> Result<Arc<PoolAllocation>> {
    let pool_vmo = manager.ensure_pool()?;
    let allocation = manager.dumb_pool.allocate(size)?;
    let range = allocation.offset()..allocation.offset() + allocation.size_bytes();
    pool_vmo.fill_zeros(range)?;
    Ok(allocation)
}

/// Creates a dumb buffer by allocating from the global pool
/// and wrapping it in a GEM object.
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

    let allocation = allocate_pool_span(&handle.gpu_manager, size)?;
    let offset = allocation.offset();

    let object = GemObjectRef::insert_new(
        &handle.gpu_manager,
        DumbBuffer {
            offset,
            size,
            width: req.width,
            height: req.height,
            bpp: req.bpp,
            allocation,
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
        PendingDumbBuffer::new(pending),
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
    #[ktest]
    fn pool_reuses_released_ranges() {
        let pool = DumbPool::new(3 * PAGE_SIZE);
        let first = pool.allocate(PAGE_SIZE).unwrap();
        let second = pool.allocate(PAGE_SIZE).unwrap();
        assert_eq!(first.offset(), 0);
        assert_eq!(second.offset(), PAGE_SIZE);
        drop(first);
        let reused = pool.allocate(PAGE_SIZE).unwrap();
        assert_eq!(reused.offset(), 0);
        assert_eq!(
            pool.usage(),
            DumbPoolUsage {
                used_bytes: 2 * PAGE_SIZE,
                high_water_bytes: 2 * PAGE_SIZE,
            }
        );
    }

    #[ktest]
    fn pool_coalesces_adjacent_released_ranges() {
        let pool = DumbPool::new(3 * PAGE_SIZE);
        let first = pool.allocate(PAGE_SIZE).unwrap();
        let second = pool.allocate(PAGE_SIZE).unwrap();
        let third = pool.allocate(PAGE_SIZE).unwrap();
        drop(second);
        drop(first);
        assert_eq!(pool.allocate(2 * PAGE_SIZE).unwrap().offset(), 0);
        drop(third);
    }
}
