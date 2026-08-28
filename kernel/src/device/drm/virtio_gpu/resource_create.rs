// SPDX-License-Identifier: MPL-2.0

//! Transactional virtio-gpu resource creation.
//!
//! The ioctl is split into validation, reversible GEM preparation,
//! host-side creation, userspace copyout, and publication.
//! Any return before publication rolls back context membership and the host resource through [`Drop`].
//!
//! The wire layout follows Linux's `virtgpu_drm.h`.
//! Linux treats `bo_handle` as output-only;
//! Asterinas also accepts an existing GEM handle so a dumb/KMS buffer can become virgl backing without a second allocation.
//! <https://github.com/torvalds/linux/blob/master/include/uapi/drm/virtgpu_drm.h>
//! <https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/virtio/virtgpu_ioctl.c>

use aster_virtio::device::gpu::Resource3dCreateParams;

use super::super::{
    DriHandle, DumbBuffer, GemResourceState,
    dumb::{self, PendingDumbBuffer, PoolAllocation},
    gem::{GemObjectRef, PendingGemHandle},
    virgl_resource::{self, LiveGemResource},
};
use crate::prelude::*;

/// `struct drm_virtgpu_resource_create`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(in super::super) struct DrmVirtgpuResourceCreate {
    pub(in super::super) target: u32,
    pub(in super::super) format: u32,
    pub(in super::super) bind: u32,
    pub(in super::super) width: u32,
    pub(in super::super) height: u32,
    pub(in super::super) depth: u32,
    pub(in super::super) array_size: u32,
    pub(in super::super) last_level: u32,
    pub(in super::super) nr_samples: u32,
    pub(in super::super) flags: u32,
    pub(in super::super) bo_handle: u32,
    pub(in super::super) res_handle: u32,
    pub(in super::super) size: u32,
    pub(in super::super) stride: u32,
}

/// Creates a 3D resource and publishes its GEM handle only after copyout.
pub(in super::super) fn virtgpu_resource_create(
    handle: &DriHandle,
    cmd: crate::util::ioctl::Ioctl<
        b'd',
        0x44,
        true,
        crate::util::ioctl::InOutData<DrmVirtgpuResourceCreate>,
    >,
) -> Result<i32> {
    let validated = ValidatedResourceCreate::validate(cmd.read()?)?;
    let prepared = validated.prepare(handle)?;
    prepared.execute(handle, cmd)?;
    Ok(0)
}

/// A validated request that has not allocated GEM or host state.
struct ValidatedResourceCreate {
    request: DrmVirtgpuResourceCreate,
    create: Resource3dCreateParams,
    minimum_backing_size_bytes: Option<u32>,
}

impl ValidatedResourceCreate {
    fn validate(request: DrmVirtgpuResourceCreate) -> Result<Self> {
        let create = Resource3dCreateParams {
            resource_id: 0,
            target: request.target,
            format: request.format,
            bind: request.bind,
            width: request.width,
            height: request.height,
            depth: request.depth,
            array_size: request.array_size,
            last_level: request.last_level,
            nr_samples: request.nr_samples,
            flags: request.flags,
        };
        virgl_resource::validate_create(&create)?;
        let minimum_backing_size_bytes = virgl_resource::minimum_backing_size(&create)?;
        Ok(Self {
            request,
            create,
            minimum_backing_size_bytes,
        })
    }

    fn prepare<'a>(self, handle: &'a DriHandle) -> Result<PreparedResourceCreate<'a>> {
        let backing = if self.request.bo_handle == 0 {
            PreparedBacking::allocate(handle, &self.request, self.minimum_backing_size_bytes)?
        } else {
            PreparedBacking::retain(handle, self.request.bo_handle)?
        };
        if self
            .minimum_backing_size_bytes
            .is_some_and(|minimum| backing.size_bytes < minimum)
        {
            return_errno_with_message!(Errno::EINVAL, "GEM backing is smaller than the resource");
        }
        Ok(PreparedResourceCreate {
            request: self.request,
            create: self.create,
            backing,
        })
    }
}

/// Reversible GEM backing, pinned independently of its public or pending handle.
struct PreparedBacking<'a> {
    transaction_object: GemObjectRef<'a>,
    pending_buffer: Option<PendingDumbBuffer<'a>>,
    device_addr: u64,
    size_bytes: u32,
    owner: Arc<PoolAllocation>,
}

impl<'a> PreparedBacking<'a> {
    fn retain(handle: &'a DriHandle, gem_handle: u32) -> Result<Self> {
        let object_id = handle.object_id_for_handle(gem_handle)?;
        let transaction_object = GemObjectRef::retain(&handle.gpu_manager, object_id)?;
        let buffer = transaction_object.buffer();
        let size_bytes = u32::try_from(buffer.size)
            .map_err(|_| Error::with_message(Errno::EINVAL, "GEM backing is too large"))?;
        let device_addr = backing_device_addr(handle, buffer.offset)?;
        Ok(Self {
            transaction_object,
            pending_buffer: None,
            device_addr,
            size_bytes,
            owner: buffer.allocation,
        })
    }

    fn allocate(
        handle: &'a DriHandle,
        request: &DrmVirtgpuResourceCreate,
        minimum_size_bytes: Option<u32>,
    ) -> Result<Self> {
        const BITS_PER_PIXEL: u32 = 32;
        const BYTES_PER_PIXEL: u32 = BITS_PER_PIXEL / 8;
        let pitch = if request.stride == 0 {
            request
                .width
                .checked_mul(BYTES_PER_PIXEL)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "resource stride overflows"))?
        } else {
            request.stride
        };
        let size_bytes = if request.size == 0 {
            let default_size_bytes = (pitch as usize)
                .checked_mul(request.height as usize)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "resource size overflows"))?;
            default_size_bytes.max(minimum_size_bytes.unwrap_or(0) as usize)
        } else {
            request.size as usize
        };
        if size_bytes == 0 {
            return_errno_with_message!(Errno::EINVAL, "resource backing has zero size");
        }
        let backing_size_bytes = u32::try_from(size_bytes)
            .map_err(|_| Error::with_message(Errno::EINVAL, "GEM backing is too large"))?;
        if minimum_size_bytes.is_some_and(|minimum| backing_size_bytes < minimum) {
            return_errno_with_message!(Errno::EINVAL, "GEM backing is smaller than the resource");
        }
        let allocation = dumb::allocate_pool_span(&handle.gpu_manager, size_bytes)?;
        let offset = allocation.offset();
        let device_addr = backing_device_addr(handle, offset)?;
        let owner = allocation.clone();
        let object = GemObjectRef::insert_new(
            &handle.gpu_manager,
            DumbBuffer {
                offset,
                size: size_bytes,
                width: request.width,
                height: request.height,
                bpp: BITS_PER_PIXEL,
                allocation,
            },
        )?;
        let object_id = object.object_id();
        let pending_buffer = PendingDumbBuffer::new(PendingGemHandle::new(handle, object)?);
        let transaction_object = GemObjectRef::retain(&handle.gpu_manager, object_id)?;
        Ok(Self {
            transaction_object,
            pending_buffer: Some(pending_buffer),
            device_addr,
            size_bytes: backing_size_bytes,
            owner,
        })
    }

    fn object_id(&self) -> u32 {
        self.transaction_object.object_id()
    }
}

fn backing_device_addr(handle: &DriHandle, offset: usize) -> Result<u64> {
    let base = handle.gpu_manager.pool_paddr()?;
    let addr = base
        .checked_add(offset)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "GEM backing address overflows"))?;
    Ok(addr as u64)
}

/// All recoverable allocation is complete; no host state exists yet.
struct PreparedResourceCreate<'a> {
    request: DrmVirtgpuResourceCreate,
    create: Resource3dCreateParams,
    backing: PreparedBacking<'a>,
}

impl<'a> PreparedResourceCreate<'a> {
    fn execute(
        self,
        handle: &'a DriHandle,
        cmd: crate::util::ioctl::Ioctl<
            b'd',
            0x44,
            true,
            crate::util::ioctl::InOutData<DrmVirtgpuResourceCreate>,
        >,
    ) -> Result<()> {
        let resource_creation = handle.gpu_manager.resource_creation.lock();
        handle.gpu_manager.drain_pending_context_cleanup();
        handle.gpu_manager.drain_pending_resource_cleanup();
        if handle
            .gpu_manager
            .has_gem_resource(self.backing.object_id())
        {
            drop(resource_creation);
            return_errno_with_message!(Errno::EBUSY, "GEM object already has a 3D resource");
        }

        let resource_id =
            handle.gpu_manager.gpu.allocate_resource_id().map_err(|_| {
                Error::with_message(Errno::ENOSPC, "virtio-gpu resource ids exhausted")
            })?;
        let mut create = self.create;
        create.resource_id = resource_id;
        let mut transaction = ResourceCreateTransaction {
            // This guard must be the first field. Rust drops fields in declaration
            // order, so pending GEM cleanup can reacquire `resource_creation`.
            _resource_creation: resource_creation,
            handle,
            request: self.request,
            create,
            backing: self.backing,
            state: ResourceCreateState::Reserved,
        };
        transaction.create_resource()?;
        transaction.attach_backing()?;
        transaction.attach_to_context()?;
        transaction.write_response(cmd)?;
        transaction.publish();
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ResourceCreateState {
    Reserved,
    Created,
    ContextAttached,
    Published,
}

/// Owns host rollback and the GEM pin until the response becomes visible.
struct ResourceCreateTransaction<'a> {
    _resource_creation: MutexGuard<'a, ()>,
    handle: &'a DriHandle,
    request: DrmVirtgpuResourceCreate,
    create: Resource3dCreateParams,
    backing: PreparedBacking<'a>,
    state: ResourceCreateState,
}

impl ResourceCreateTransaction<'_> {
    fn create_resource(&mut self) -> Result<()> {
        self.handle
            .gpu_manager
            .gpu
            .resource_create_3d(self.create)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu resource creation failed"))?;
        self.state = ResourceCreateState::Created;
        Ok(())
    }

    fn attach_backing(&self) -> Result<()> {
        self.handle
            .gpu_manager
            .gpu
            .attach_backing(
                self.create.resource_id,
                self.backing.device_addr,
                self.backing.size_bytes,
                self.backing.owner.clone(),
            )
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu backing attach failed"))?;
        Ok(())
    }

    fn attach_to_context(&mut self) -> Result<()> {
        self.handle
            .attach_resource_to_context(self.create.resource_id)?;
        self.state = ResourceCreateState::ContextAttached;
        Ok(())
    }

    fn write_response(
        &mut self,
        cmd: crate::util::ioctl::Ioctl<
            b'd',
            0x44,
            true,
            crate::util::ioctl::InOutData<DrmVirtgpuResourceCreate>,
        >,
    ) -> Result<()> {
        self.request.res_handle = self.create.resource_id;
        self.request.size = self.backing.size_bytes;
        self.request.stride = self
            .request
            .width
            .checked_mul(4)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "resource stride overflows"))?;
        if let Some(pending_buffer) = self.backing.pending_buffer.as_ref() {
            self.request.bo_handle = pending_buffer.id();
        }
        cmd.write(&self.request)
    }

    fn publish(mut self) {
        self.handle.gpu_manager.insert_gem_resource(
            self.backing.object_id(),
            GemResourceState::Live(LiveGemResource {
                create: self.create,
                backing_size: self.backing.size_bytes,
            }),
        );
        self.state = ResourceCreateState::Published;
        let pending_buffer = self.backing.pending_buffer.take();
        // Release the transaction lock and its extra GEM pin before publishing
        // the reserved handle. Pending-handle cleanup may acquire the same lock,
        // and no userspace-visible handle may observe a half-committed mapping.
        drop(self);
        if let Some(pending_buffer) = pending_buffer {
            pending_buffer.publish();
        }
    }
}

impl Drop for ResourceCreateTransaction<'_> {
    fn drop(&mut self) {
        if self.state == ResourceCreateState::ContextAttached
            && let Err(error) = self
                .handle
                .detach_resource_from_context(self.create.resource_id)
        {
            ostd::warn!(
                "cannot detach failed virtio-gpu resource {}: {:?}",
                self.create.resource_id,
                error
            );
        }
        if matches!(
            self.state,
            ResourceCreateState::Created | ResourceCreateState::ContextAttached
        ) && self
            .handle
            .gpu_manager
            .gpu
            .resource_unref(self.create.resource_id)
            .is_err()
        {
            self.handle.gpu_manager.insert_gem_resource(
                self.backing.object_id(),
                GemResourceState::CleanupOnly(self.create.resource_id),
            );
        }
    }
}
