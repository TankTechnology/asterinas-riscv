// SPDX-License-Identifier: MPL-2.0

//! DRM virtio-gpu specific ioctls: execbuffer, resource create, context
//! init, get caps, and getparam.
//!
//! These are the kernel-side entry points for Mesa's virgl driver. They
//! translate DRM ioctl structs into virtio-gpu control queue commands.

use core::sync::atomic::Ordering;

use super::{
    DumbBuffer,
    dumb::{PendingDumbBuffer, PendingPoolAllocation},
    gem::{GemObjectRef, PendingGemHandle},
};
use crate::{fs::file::file_table::FdFlags, prelude::*, process::posix_thread::FileTableRefMut};

// ---------------------------------------------------------------------------
// Wire types (matching Linux include/uapi/drm/virtgpu_drm.h)
// ---------------------------------------------------------------------------

/// `struct drm_virtgpu_execbuffer`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpuExecbuffer {
    pub flags: u32,
    pub size: u32,
    pub command: u64,    // void* — userspace pointer to command buffer
    pub bo_handles: u64, // __u32* — array of GEM handle indices
    pub num_bo_handles: u32,
    pub fence_fd: i32, // in/out fence fd
    pub ring_idx: u32,
    pub syncobj_stride: u32,
    pub num_in_syncobjs: u32,
    pub num_out_syncobjs: u32,
    pub in_syncobjs: u64,
    pub out_syncobjs: u64,
}

/// `struct drm_virtgpu_getparam`.
///
/// Note: `value` is a userspace **pointer** to a `u64` that the kernel writes
/// through, not an inline value field.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpuGetparam {
    pub param: u64,
    pub value: u64,
}

/// `struct drm_virtgpu_resource_create`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpuResourceCreate {
    pub target: u32,
    pub format: u32,
    pub bind: u32,
    pub width: u32,
    pub height: u32,
    pub depth: u32,
    pub array_size: u32,
    pub last_level: u32,
    pub nr_samples: u32,
    pub flags: u32,
    pub bo_handle: u32,  // in: existing GEM handle, or 0
    pub res_handle: u32, // out: virtio-gpu resource handle
    pub size: u32,
    pub stride: u32,
}

/// `struct drm_virtgpu_resource_info`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpuResourceInfo {
    pub bo_handle: u32,
    pub res_handle: u32,
    pub size: u32,
    pub blob_mem: u32,
}

/// `struct drm_virtgpu_get_caps`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpuGetCaps {
    pub cap_set_id: u32,
    pub cap_set_ver: u32,
    pub addr: u64, // void* — userspace output buffer
    pub size: u32,
    pub pad: u32,
}

/// `struct drm_virtgpu_context_init`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpuContextInit {
    pub num_params: u32,
    pub pad: u32,
    pub ctx_set_params: u64, // __u64 — pointer to array of DrmVirtgpuContextSetParam
}

/// `struct drm_virtgpu_map`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpuMap {
    pub offset: u64,
    pub handle: u32,
    pub pad: u32,
}

/// `struct drm_virtgpu_3d_box`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpu3dBox {
    pub x: u32,
    pub y: u32,
    pub z: u32,
    pub w: u32,
    pub h: u32,
    pub d: u32,
}

/// `struct drm_virtgpu_3d_transfer_to_host`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpu3dTransferToHost {
    pub bo_handle: u32,
    pub box_: DrmVirtgpu3dBox,
    pub level: u32,
    pub offset: u32,
    pub stride: u32,
    pub layer_stride: u32,
}

/// `struct drm_virtgpu_3d_transfer_from_host`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpu3dTransferFromHost {
    pub bo_handle: u32,
    pub box_: DrmVirtgpu3dBox,
    pub level: u32,
    pub offset: u32,
    pub stride: u32,
    pub layer_stride: u32,
}

/// `struct drm_virtgpu_3d_wait`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpu3dWait {
    pub handle: u32,
    pub flags: u32,
}

// Param constants
const VIRTGPU_PARAM_3D_FEATURES: u64 = 1;
const VIRTGPU_PARAM_CAPSET_QUERY_FIX: u64 = 2;
const VIRTGPU_PARAM_RESOURCE_BLOB: u64 = 3;
const VIRTGPU_PARAM_HOST_VISIBLE: u64 = 4;
const VIRTGPU_PARAM_CROSS_DEVICE: u64 = 5;
const VIRTGPU_PARAM_CONTEXT_INIT: u64 = 6;
const VIRTGPU_PARAM_SUPPORTED_CAPSET_IDS: u64 = 7;
const VIRTGPU_PARAM_EXPLICIT_DEBUG_NAME: u64 = 8;
const VIRTGPU_PARAM_BLOB_ALIGNMENT: u64 = 9;

/// Upper bounds for one userspace-provided virgl submission.
const MAX_EXECBUFFER_SIZE: usize = 16 * 1024 * 1024;
const MAX_EXECBUFFER_HANDLES: usize = 4096;

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

use aster_virtio::device::gpu::{VIRTIO_GPU_CAPSET_VIRGL, VIRTIO_GPU_CAPSET_VIRGL2};
use ostd::mm::VmIo;

use crate::context::current_userspace;

/// GETPARAM: return device parameters queried by Mesa.
pub(super) fn virtgpu_getparam(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<
        b'd',
        0x43,
        true,
        crate::util::ioctl::InOutData<DrmVirtgpuGetparam>,
    >,
) -> Result<i32> {
    let req = cmd.read()?;
    let value: u64 = match req.param {
        VIRTGPU_PARAM_3D_FEATURES => u64::from(handle.gpu_manager.gpu.supports_virgl()),
        VIRTGPU_PARAM_CAPSET_QUERY_FIX => 1, // GET_CAPS handles non-zero versions
        VIRTGPU_PARAM_RESOURCE_BLOB => 0,    // no blob resources
        VIRTGPU_PARAM_HOST_VISIBLE => 0,     // no host-visible resources
        VIRTGPU_PARAM_CROSS_DEVICE => 0,     // no cross-device sharing
        VIRTGPU_PARAM_CONTEXT_INIT => 0,     // no context init extension
        // Bitmask of supported capsets: bit 1 = virgl, bit 2 = virgl2.
        VIRTGPU_PARAM_SUPPORTED_CAPSET_IDS => handle
            .gpu_manager
            .gpu
            .supported_capset_ids()
            .map_err(|_| Error::with_message(Errno::EIO, "cannot enumerate virgl capsets"))?,
        VIRTGPU_PARAM_EXPLICIT_DEBUG_NAME => 0, // no debug name support
        VIRTGPU_PARAM_BLOB_ALIGNMENT => 0,      // no blob alignment
        _ => {
            return_errno_with_message!(Errno::EINVAL, "unknown virtio-gpu parameter");
        }
    };
    // `value` is a userspace pointer to the `u64` result.
    current_userspace!().write_val(req.value as usize, &value)?;
    Ok(0)
}

/// RESOURCE_CREATE: create a 3D resource on the virtio-gpu device.
///
/// Maps a GEM buffer (via bo_handle) to a virtio-gpu 3D resource.
/// If bo_handle is 0, creates a resource without backing (for
/// render targets that are written to by the GPU).
pub(super) fn virtgpu_resource_create(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<
        b'd',
        0x44,
        true,
        crate::util::ioctl::InOutData<DrmVirtgpuResourceCreate>,
    >,
) -> Result<i32> {
    let mut req = cmd.read()?;
    let ctx_id = handle.ensure_virgl_context()?;
    let _resource_creation = handle.gpu_manager.resource_creation.lock();

    // Allocate a new virtio-gpu resource id
    let res_handle = handle.gpu_manager.gpu.allocate_resource_id();

    // If a GEM buffer handle is provided, look up the backing memory.
    // Otherwise allocate a fresh dumb buffer so the resource has a GEM
    // handle for scanout (ADDFB2 / KMS) and the rendered image is host-visible.
    let mut new_dumb_buffer: Option<PendingDumbBuffer<'_>> = None;
    let retained_backing_object: u32;
    let backing = if req.bo_handle != 0 {
        let inner = handle.inner.lock();
        let object_id = *inner
            .handles
            .get(&req.bo_handle)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?;
        drop(inner);
        let base = handle.gpu_manager.pool_paddr()?;
        let buffer = handle.gpu_manager.retain_gem_object(object_id)?;
        retained_backing_object = object_id;
        let size = match u32::try_from(buffer.size) {
            Ok(size) => size,
            Err(_) => {
                let _ = handle.gpu_manager.release_gem_object(object_id);
                return_errno_with_message!(Errno::EINVAL, "GEM backing is too large");
            }
        };
        Some((object_id, base + buffer.offset, size))
    } else {
        // Allocate a dumb buffer from the shared pool to back this resource.
        let bpp = 32;
        let pitch = if req.stride == 0 {
            req.width.saturating_mul(bpp / 8)
        } else {
            req.stride
        };
        let size = if req.size == 0 {
            (pitch as usize).saturating_mul(req.height as usize)
        } else {
            req.size as usize
        };
        handle.gpu_manager.ensure_pool()?;
        let base = handle.gpu_manager.pool_paddr()?;
        let allocation = PendingPoolAllocation::new(&handle.gpu_manager, size)?;
        let offset = allocation.offset();

        let object = GemObjectRef::insert_new(
            &handle.gpu_manager,
            DumbBuffer {
                offset,
                size,
                width: req.width,
                height: req.height,
                bpp,
            },
        )?;
        let object_id = object.object_id();
        let pending = PendingGemHandle::new(handle, object)?;
        // Pin the object separately while the host-resource transaction runs.
        handle.gpu_manager.retain_gem_object(object_id)?;
        retained_backing_object = object_id;
        new_dumb_buffer = Some(PendingDumbBuffer::new(pending, allocation));

        Some((object_id, base + offset, size as u32))
    };

    let backing_object_id = backing.map(|(object_id, _, _)| object_id);
    if let Some(object_id) = backing_object_id
        && handle
            .gpu_manager
            .gem_resources
            .lock()
            .contains_key(&object_id)
    {
        let _ = handle
            .gpu_manager
            .release_gem_object(retained_backing_object);
        return_errno_with_message!(Errno::EBUSY, "GEM object already has a 3D resource");
    }

    let mut resource_created = false;
    let mut context_attached = false;
    let operation = (|| -> Result<()> {
        // The gallium pipe target (0 = buffer, 2 = 2D texture, ...) is passed
        // through as-is.
        handle
            .gpu_manager
            .gpu
            .resource_create_3d(
                res_handle,
                req.target,
                req.format,
                req.bind,
                req.width,
                req.height,
                req.depth,
                req.array_size,
                req.last_level,
                req.nr_samples,
                req.flags,
            )
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;
        resource_created = true;

        if let Some((object_id, addr, size)) = backing {
            handle
                .gpu_manager
                .gpu
                .attach_backing(res_handle, addr as u64, size)
                .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;
            handle
                .gpu_manager
                .gem_resources
                .lock()
                .insert(object_id, res_handle);
            req.size = size;
        } else {
            // Host-only resource: report a sensible size estimate.
            req.size = req.width.saturating_mul(req.height).saturating_mul(4);
        }

        // Every 3D resource, including Gallium buffer resources (`target ==
        // 0`), must be visible to the submitting context.
        handle
            .gpu_manager
            .gpu
            .ctx_attach_resource(ctx_id, res_handle)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;
        context_attached = true;

        req.res_handle = res_handle;
        req.stride = req.width.saturating_mul(4);
        if let Some(pending_buffer) = new_dumb_buffer.as_ref() {
            req.bo_handle = pending_buffer.id();
        }
        cmd.write(&req)
    })();

    if let Err(error) = operation {
        if context_attached {
            let _ = handle
                .gpu_manager
                .gpu
                .ctx_detach_resource(ctx_id, res_handle);
        }
        let resource_released =
            !resource_created || handle.gpu_manager.gpu.resource_unref(res_handle).is_ok();
        if let Some(object_id) = backing_object_id {
            let mut resources = handle.gpu_manager.gem_resources.lock();
            if resource_released && resources.get(&object_id) == Some(&res_handle) {
                resources.remove(&object_id);
            } else if !resource_released {
                resources.insert(object_id, res_handle);
            }
        }
        if !resource_released && let Some(pending_buffer) = new_dumb_buffer.take() {
            // The host may still access this backing. Publish its allocation so
            // a later buffer cannot reuse the pages while cleanup is retried.
            pending_buffer.publish_allocation_only();
        }
        let _ = handle
            .gpu_manager
            .release_gem_object(retained_backing_object);
        return Err(error);
    }
    handle
        .gpu_manager
        .release_gem_object(retained_backing_object)?;
    if let Some(pending_buffer) = new_dumb_buffer {
        pending_buffer.publish();
    }
    Ok(0)
}

/// RESOURCE_INFO: return information about a virtio-gpu resource.
pub(super) fn virtgpu_resource_info(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<
        b'd',
        0x45,
        true,
        crate::util::ioctl::InOutData<DrmVirtgpuResourceInfo>,
    >,
) -> Result<i32> {
    let mut req = cmd.read()?;
    let object_id = {
        let inner = handle.inner.lock();
        *inner
            .handles
            .get(&req.bo_handle)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?
    };
    let buffer_size = {
        let objects = handle.gpu_manager.gem_objects.lock();
        objects
            .get(&object_id)
            .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?
            .buffer
            .size
    };
    req.size = u32::try_from(buffer_size)
        .map_err(|_| Error::with_message(Errno::EINVAL, "GEM backing is too large"))?;
    req.res_handle = *handle
        .gpu_manager
        .gem_resources
        .lock()
        .get(&object_id)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "GEM object has no 3D resource"))?;
    // The blob_mem field is 0 for non-blob resources.
    req.blob_mem = 0;
    cmd.write(&req)?;
    Ok(0)
}

/// GET_CAPS: return the virgl capset data blob to userspace.
///
/// Mesa's virgl driver uses this to discover the capset version and
/// feature bits supported by the host (virglrenderer).
pub(super) fn virtgpu_get_caps(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<
        b'd',
        0x49,
        true,
        crate::util::ioctl::InOutData<DrmVirtgpuGetCaps>,
    >,
) -> Result<i32> {
    let req = cmd.read()?;

    // Only virgl and virgl2 capsets are supported
    if req.cap_set_id != VIRTIO_GPU_CAPSET_VIRGL && req.cap_set_id != VIRTIO_GPU_CAPSET_VIRGL2 {
        return_errno_with_message!(Errno::EINVAL, "unsupported capset id");
    }
    let cap_set_id = req.cap_set_id;

    // Query the capset info from the device
    let capset_info = handle
        .gpu_manager
        .gpu
        .get_capset_info(cap_set_id)
        .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;
    // If the host reports a zero-sized capset, it doesn't actually support
    // this capset. Return EINVAL so Mesa falls back to the older capset
    // (VIRGL v1) instead of getting an empty capset blob.
    if capset_info.capset_max_size == 0 {
        return_errno_with_message!(Errno::EINVAL, "capset not supported by device");
    }
    // `cap_set_ver == 0` asks for the newest version the device supports.
    // This virglrenderer advertises max_version=2 but returns a zeroed v2
    // capset (only v1 carries real data), so pin to v1.
    let version = if req.cap_set_ver == 0 {
        1
    } else {
        req.cap_set_ver as u32
    };
    let capset_data = handle
        .gpu_manager
        .gpu
        .get_capset(cap_set_id, version)
        .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;

    // Copy the capset data to userspace
    if req.addr != 0 && req.size > 0 {
        let copy_len = capset_data.len().min(req.size as usize);
        current_userspace!().write_bytes(req.addr as usize, &capset_data[..copy_len])?;
    }

    // Write back the response with the actual size
    let mut resp = req;
    resp.size = capset_data.len() as u32;
    cmd.write(&resp)?;
    Ok(0)
}

/// `VIRTGPU_EXECBUF_FENCE_FD_OUT` — the caller requests an out-fence (a pollable
/// fd signaling when the submitted command completes).
const VIRTGPU_EXECBUF_FENCE_FD_OUT: u32 = 0x02;

/// EXECBUFFER: submit a virgl command stream to the host.
///
/// This is the core ioctl for virgl rendering. Mesa encodes GL commands
/// in a virgl command buffer and submits them via this ioctl. When the caller
/// sets `VIRTGPU_EXECBUF_FENCE_FD_OUT` in `flags`, the submission is fenced:
/// the device defers its response until the command completes, so by the time
/// this ioctl returns the render is done, and we hand back a pre-signaled
/// [`super::fence::FenceFile`] fd in `fence_fd`.
pub(super) fn virtgpu_execbuffer(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<
        b'd',
        0x42,
        true,
        crate::util::ioctl::InOutData<DrmVirtgpuExecbuffer>,
    >,
    file_table: &mut FileTableRefMut,
) -> Option<Result<i32>> {
    Some((|| -> Result<i32> {
        let req = cmd.read()?;
        if req.flags & !VIRTGPU_EXECBUF_FENCE_FD_OUT != 0
            || req.ring_idx != 0
            || req.syncobj_stride != 0
            || req.num_in_syncobjs != 0
            || req.num_out_syncobjs != 0
            || req.in_syncobjs != 0
            || req.out_syncobjs != 0
        {
            return_errno_with_message!(Errno::EINVAL, "unsupported execbuffer synchronization");
        }

        // Read the command buffer from userspace
        if req.size == 0 || req.command == 0 {
            return_errno_with_message!(Errno::EINVAL, "empty command buffer");
        }

        let command_size = req.size as usize;
        if command_size > MAX_EXECBUFFER_SIZE {
            return_errno_with_message!(Errno::EINVAL, "command buffer is too large");
        }
        let mut cmd_buf = Vec::new();
        cmd_buf
            .try_reserve_exact(command_size)
            .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate command buffer"))?;
        cmd_buf.resize(command_size, 0);
        current_userspace!().read_bytes(req.command as usize, &mut cmd_buf)?;

        // Validate the GEM buffer handles in the resource list
        if req.num_bo_handles > 0 {
            if req.bo_handles == 0 {
                return_errno_with_message!(Errno::EINVAL, "missing execbuffer handle list");
            }
            let handle_count = req.num_bo_handles as usize;
            if handle_count > MAX_EXECBUFFER_HANDLES {
                return_errno_with_message!(Errno::EINVAL, "too many execbuffer handles");
            }
            let byte_count = handle_count
                .checked_mul(size_of::<u32>())
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "handle list overflows"))?;
            let mut raw = Vec::new();
            raw.try_reserve_exact(byte_count)
                .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate handle list"))?;
            raw.resize(byte_count, 0);
            current_userspace!().read_bytes(req.bo_handles as usize, &mut raw)?;

            let inner = handle.inner.lock();
            let guard = handle.gpu_manager.gem_objects.lock();
            for chunk in raw.as_chunks::<4>().0 {
                let bo_h = u32::from_le_bytes(*chunk);
                let valid = inner
                    .handles
                    .get(&bo_h)
                    .is_some_and(|object_id| guard.contains_key(object_id));
                if !valid {
                    return_errno_with_message!(Errno::EINVAL, "unknown GEM handle in execbuffer");
                }
            }
        }

        let ctx_id = handle.ensure_virgl_context()?;
        let mut resp = req;

        let mut installed_fence_fd = None;
        if req.flags & VIRTGPU_EXECBUF_FENCE_FD_OUT != 0 {
            // Fenced submit: blocks until the host finishes the command.
            let fence_id = handle
                .gpu_manager
                .next_fence_id
                .fetch_add(1, Ordering::Relaxed);
            handle
                .gpu_manager
                .gpu
                .submit_3d_fenced(ctx_id, req.size, &cmd_buf, fence_id)
                .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;

            // The fence is already signaled; hand back a pollable fd.
            let fence_file = Arc::new(super::fence::FenceFile::new());
            let fd = file_table
                .unwrap()
                .write()
                .insert(fence_file, FdFlags::empty());
            resp.fence_fd = u32::from(fd) as i32;
            installed_fence_fd = Some(fd);
        } else {
            handle
                .gpu_manager
                .gpu
                .submit_3d(ctx_id, req.size, &cmd_buf)
                .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;
            resp.fence_fd = -1;
        }

        if let Err(error) = cmd.write(&resp) {
            if let Some(fd) = installed_fence_fd {
                let closed = file_table.unwrap().write().close_file(fd);
                drop(closed);
            }
            return Err(error);
        }
        Ok(0)
    })())
}

/// CONTEXT_INIT: reject explicit context initialization when the corresponding
/// virtio-gpu feature was not negotiated.
pub(super) fn virtgpu_context_init(
    _handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<
        b'd',
        0x4b,
        true,
        crate::util::ioctl::InOutData<DrmVirtgpuContextInit>,
    >,
) -> Result<i32> {
    let _ = cmd.read()?;
    return_errno_with_message!(
        Errno::EINVAL,
        "virtio-gpu context init extension is not negotiated"
    );
}

/// TRANSFER_TO_HOST: transfer data from guest to host for a 3D resource.
pub(super) fn virtgpu_transfer_to_host(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<
        b'd',
        0x47,
        true,
        crate::util::ioctl::InOutData<DrmVirtgpu3dTransferToHost>,
    >,
) -> Result<i32> {
    let req = cmd.read()?;
    let ctx_id = handle.ensure_virgl_context()?;

    let object_id = {
        let inner = handle.inner.lock();
        *inner
            .handles
            .get(&req.bo_handle)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?
    };
    let resource_id = {
        let resources = handle.gpu_manager.gem_resources.lock();
        *resources
            .get(&object_id)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "GEM object has no 3D resource"))?
    };

    handle
        .gpu_manager
        .gpu
        .transfer_to_host_3d(
            ctx_id,
            resource_id,
            req.box_.x,
            req.box_.y,
            req.box_.z,
            req.box_.w,
            req.box_.h,
            req.box_.d,
            req.offset as u64,
            req.level,
            req.stride,
            req.layer_stride,
        )
        .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;

    cmd.write(&req)?;
    Ok(0)
}

/// TRANSFER_FROM_HOST: transfer data from host to guest for a 3D resource.
pub(super) fn virtgpu_transfer_from_host(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<
        b'd',
        0x46,
        true,
        crate::util::ioctl::InOutData<DrmVirtgpu3dTransferFromHost>,
    >,
) -> Result<i32> {
    let req = cmd.read()?;
    let ctx_id = handle.ensure_virgl_context()?;

    let object_id = {
        let inner = handle.inner.lock();
        *inner
            .handles
            .get(&req.bo_handle)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?
    };
    let resource_id = {
        let resources = handle.gpu_manager.gem_resources.lock();
        *resources
            .get(&object_id)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "GEM object has no 3D resource"))?
    };

    handle
        .gpu_manager
        .gpu
        .transfer_from_host_3d(
            ctx_id,
            resource_id,
            req.box_.x,
            req.box_.y,
            req.box_.z,
            req.box_.w,
            req.box_.h,
            req.box_.d,
            req.offset as u64,
            req.level,
            req.stride,
            req.layer_stride,
        )
        .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;

    cmd.write(&req)?;
    Ok(0)
}

/// MAP: return the mmap offset for a GEM buffer.
pub(super) fn virtgpu_map(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<b'd', 0x41, true, crate::util::ioctl::InOutData<DrmVirtgpuMap>>,
) -> Result<i32> {
    let req = cmd.read()?;
    let object_id = {
        let inner = handle.inner.lock();
        *inner
            .handles
            .get(&req.handle)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?
    };
    let offset = {
        let objects = handle.gpu_manager.gem_objects.lock();
        objects
            .get(&object_id)
            .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?
            .buffer
            .offset
    };
    let mut resp = req;
    resp.offset = offset as u64;
    cmd.write(&resp)?;
    Ok(0)
}

/// `VIRTGPU_WAIT_NOWAIT` — reports busy instead of waiting.
const VIRTGPU_WAIT_NOWAIT: u32 = 0x01;

/// A one-dword `VIRGL_CCMD_NOP` command stream.
const VIRGL_NOP: [u8; 4] = [0; 4];

/// WAIT: waits for all earlier work on this file's virgl timeline.
pub(super) fn virtgpu_wait(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<
        b'd',
        0x48,
        true,
        crate::util::ioctl::InOutData<DrmVirtgpu3dWait>,
    >,
) -> Result<i32> {
    let req = cmd.read()?;
    if req.flags & !VIRTGPU_WAIT_NOWAIT != 0 {
        return_errno_with_message!(Errno::EINVAL, "unsupported virtgpu wait flags");
    }

    let object_id = {
        let inner = handle.inner.lock();
        *inner
            .handles
            .get(&req.handle)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?
    };
    if !handle
        .gpu_manager
        .gem_resources
        .lock()
        .contains_key(&object_id)
    {
        return_errno_with_message!(Errno::EINVAL, "GEM object has no 3D resource");
    }

    // The transport currently has no nonblocking used-ring query for a
    // resource timeline. Reject NOWAIT explicitly rather than sleeping or
    // claiming completion.
    if req.flags & VIRTGPU_WAIT_NOWAIT != 0 {
        return_errno_with_message!(
            Errno::EOPNOTSUPP,
            "nonblocking virtio-gpu wait is unavailable"
        );
    }

    // Virtio 1.3 section 5.7.6.7 requires a fenced response to be delayed
    // until the associated command and all earlier commands on this context's
    // timeline have completed. A fenced virgl NOP is therefore a timeline
    // barrier without replaying or mutating the resource.
    let context_id = handle.ensure_virgl_context()?;
    let fence_id = handle
        .gpu_manager
        .next_fence_id
        .fetch_add(1, Ordering::Relaxed);
    handle
        .gpu_manager
        .gpu
        .submit_3d_fenced(context_id, VIRGL_NOP.len() as u32, &VIRGL_NOP, fence_id)
        .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu wait failed"))?;

    cmd.write(&req)?;
    Ok(0)
}
