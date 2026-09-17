// SPDX-License-Identifier: MPL-2.0

//! DRM virtio-gpu-specific ioctls:
//! execbuffer, resource create, context init, get caps, and getparam.
//!
//! These are the kernel-side entry points for Mesa's virgl driver.
//! They translate DRM ioctl structs into virtio-gpu control queue commands.

mod execbuffer;
mod resource_create;

use core::time::Duration;

use aster_virtio::device::gpu;
pub(super) use execbuffer::{DrmVirtgpuExecbuffer, virtgpu_execbuffer};
use ostd::mm::VmIo;
pub(super) use resource_create::{DrmVirtgpuResourceCreate, virtgpu_resource_create};

use super::virgl_resource::Transfer3d;
use crate::{context::current_userspace, prelude::*};

// ---------------------------------------------------------------------------
// Wire types (matching Linux include/uapi/drm/virtgpu_drm.h)
// ---------------------------------------------------------------------------

/// `struct drm_virtgpu_getparam`.
///
/// Note: `value` is a userspace **pointer** to a `u64` that the kernel writes through,
/// not an inline value field.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpuGetparam {
    pub param: u64,
    pub value: u64,
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

impl DrmVirtgpu3dBox {
    fn into_transfer_3d(self, level: u32, offset: u32) -> Transfer3d {
        Transfer3d {
            x: self.x,
            y: self.y,
            z: self.z,
            width: self.w,
            height: self.h,
            depth: self.d,
            level,
            offset,
        }
    }
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

/// Returns a device parameter queried by Mesa.
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
    let gpu = handle.gpu_manager.gpu()?;
    let value: u64 = match req.param {
        VIRTGPU_PARAM_3D_FEATURES => u64::from(gpu.supports_virgl()),
        VIRTGPU_PARAM_CAPSET_QUERY_FIX => 1, // GET_CAPS handles non-zero versions
        VIRTGPU_PARAM_RESOURCE_BLOB => 0,    // no blob resources
        VIRTGPU_PARAM_HOST_VISIBLE => 0,     // no host-visible resources
        VIRTGPU_PARAM_CROSS_DEVICE => 0,     // no cross-device sharing
        VIRTGPU_PARAM_CONTEXT_INIT => 0,     // no context init extension
        // Bitmask of supported capsets: bit 1 = virgl, bit 2 = virgl2.
        VIRTGPU_PARAM_SUPPORTED_CAPSET_IDS => gpu
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

/// Returns information about a virtio-gpu resource.
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
    let object_id = handle.object_id_for_handle(req.bo_handle)?;
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
    req.res_handle = handle
        .gpu_manager
        .live_gem_resource(object_id)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "GEM object has no 3D resource"))?;
    // The blob_mem field is 0 for non-blob resources.
    req.blob_mem = 0;
    cmd.write(&req)?;
    Ok(0)
}

/// Returns the virgl capset data blob to userspace.
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
    if req.size == 0 {
        return_errno_with_message!(Errno::EINVAL, "zero-sized capset request");
    }
    if req.addr == 0 {
        return_errno_with_message!(Errno::EFAULT, "null capset output pointer");
    }

    // Only virgl and virgl2 capsets are supported
    if req.cap_set_id != gpu::VIRTIO_GPU_CAPSET_VIRGL
        && req.cap_set_id != gpu::VIRTIO_GPU_CAPSET_VIRGL2
    {
        return_errno_with_message!(Errno::EINVAL, "unsupported capset id");
    }
    let cap_set_id = req.cap_set_id;

    let gpu = handle.gpu_manager.gpu()?;
    let capset_info = gpu
        .get_capset_info(cap_set_id)
        .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;
    // If the host reports a zero-sized capset, it doesn't actually support
    // this capset. Return EINVAL so Mesa falls back to the older capset
    // (VIRGL v1) instead of getting an empty capset blob.
    if capset_info.capset_max_size == 0 {
        return_errno_with_message!(Errno::EINVAL, "capset not supported by device");
    }
    if req.cap_set_ver > capset_info.capset_max_version {
        return_errno_with_message!(Errno::EINVAL, "unsupported capset version");
    }
    let capset_data = gpu
        .get_capset(cap_set_id, req.cap_set_ver)
        .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;

    let copy_len = capset_data.len().min(req.size as usize);
    current_userspace!().write_bytes(req.addr as usize, &capset_data[..copy_len])?;

    let mut resp = req;
    resp.size = capset_data.len() as u32;
    cmd.write(&resp)?;
    Ok(0)
}

/// Rejects explicit context initialization when the corresponding
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

/// Transfers data from guest to host for a 3D resource.
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
    if req.stride != 0 || req.layer_stride != 0 {
        return_errno_with_message!(Errno::EINVAL, "layout overrides require blob resources");
    }
    let _resource_creation = handle.gpu_manager.resource_creation.lock();
    let object_id = handle.object_id_for_handle(req.bo_handle)?;
    let resource = handle
        .gpu_manager
        .live_gem_resource_metadata(object_id)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "GEM object has no 3D resource"))?;
    resource.validate_transfer(req.box_.into_transfer_3d(req.level, req.offset))?;
    let resource_id = resource.create.resource_id;
    let ctx_id = handle.attach_resource_to_context(resource_id)?;

    handle
        .gpu_manager
        .gpu()?
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

/// Transfers data from host to guest for a 3D resource.
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
    if req.stride != 0 || req.layer_stride != 0 {
        return_errno_with_message!(Errno::EINVAL, "layout overrides require blob resources");
    }
    let _resource_creation = handle.gpu_manager.resource_creation.lock();
    let object_id = handle.object_id_for_handle(req.bo_handle)?;
    let resource = handle
        .gpu_manager
        .live_gem_resource_metadata(object_id)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "GEM object has no 3D resource"))?;
    resource.validate_transfer(req.box_.into_transfer_3d(req.level, req.offset))?;
    let resource_id = resource.create.resource_id;
    let ctx_id = handle.attach_resource_to_context(resource_id)?;

    handle
        .gpu_manager
        .gpu()?
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

/// Returns the mmap offset for a GEM buffer.
pub(super) fn virtgpu_map(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<b'd', 0x41, true, crate::util::ioctl::InOutData<DrmVirtgpuMap>>,
) -> Result<i32> {
    let req = cmd.read()?;
    let object_id = handle.object_id_for_handle(req.handle)?;
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
const VIRTGPU_WAIT_TIMEOUT: Duration = Duration::from_secs(15);

/// Waits for every tracked command that references this GEM object.
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

    let object_id = handle.object_id_for_handle(req.handle)?;
    if handle.gpu_manager.live_gem_resource(object_id).is_none() {
        return_errno_with_message!(Errno::EINVAL, "GEM object has no 3D resource");
    }

    let fences = handle.gpu_manager.resource_fences(object_id);
    if fences.is_empty() {
        cmd.write(&req)?;
        return Ok(0);
    }

    if req.flags & VIRTGPU_WAIT_NOWAIT != 0 {
        for fence in &fences {
            match fence.try_finish() {
                Ok(true) => handle
                    .gpu_manager
                    .clear_resource_fences(object_id, core::slice::from_ref(fence)),
                Ok(false) => {
                    return_errno_with_message!(Errno::EBUSY, "virtio-gpu resource is busy");
                }
                Err(error) => {
                    if fence.is_signaled() {
                        handle
                            .gpu_manager
                            .clear_resource_fences(object_id, core::slice::from_ref(fence));
                    }
                    return Err(error);
                }
            }
        }
    } else {
        for fence in &fences {
            let wait_result = fence.wait_interruptible_or_timeout(&VIRTGPU_WAIT_TIMEOUT);
            if fence.is_signaled() {
                handle
                    .gpu_manager
                    .clear_resource_fences(object_id, core::slice::from_ref(fence));
            }
            if let Err(error) = wait_result {
                if error.error() == Errno::ETIME {
                    return_errno_with_message!(Errno::EBUSY, "virtio-gpu resource wait timed out");
                }
                return Err(error);
            }
        }
    }

    cmd.write(&req)?;
    Ok(0)
}
