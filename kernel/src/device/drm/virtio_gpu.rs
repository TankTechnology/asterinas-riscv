// SPDX-License-Identifier: MPL-2.0

//! DRM virtio-gpu specific ioctls: execbuffer, resource create, context
//! init, get caps, and getparam.
//!
//! These are the kernel-side entry points for Mesa's virgl driver. They
//! translate DRM ioctl structs into virtio-gpu control queue commands.

use crate::prelude::*;

// ---------------------------------------------------------------------------
// Wire types (matching Linux include/uapi/drm/virtgpu_drm.h)
// ---------------------------------------------------------------------------

/// `struct drm_virtgpu_execbuffer`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpuExecbuffer {
    pub flags: u32,
    pub size: u32,
    pub command: u64,       // void* — userspace pointer to command buffer
    pub bo_handles: u64,    // __u32* — array of GEM handle indices
    pub num_bo_handles: u32,
    pub fence_fd: i32,      // in/out fence fd
    pub ring_idx: u32,
    pub syncobj_stride: u32,
    pub num_in_syncobjs: u32,
    pub num_out_syncobjs: u32,
    pub in_syncobjs: u64,
    pub out_syncobjs: u64,
}

/// `struct drm_virtgpu_getparam`.
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
    pub bo_handle: u32,     // in: existing GEM handle, or 0
    pub res_handle: u32,    // out: virtio-gpu resource handle
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
    pub addr: u64,          // void* — userspace output buffer
    pub size: u32,
    pub pad: u32,
}

/// `struct drm_virtgpu_context_set_param`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVirtgpuContextSetParam {
    pub param: u64,
    pub value: u64,
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
    pub handle: u32,
    pub pad: u32,
    pub offset: u64,
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
const VIRTGPU_PARAM_SUPPORTED_CAPSET_IDs: u64 = 7;
const VIRTGPU_PARAM_EXPLICIT_DEBUG_NAME: u64 = 8;
const VIRTGPU_PARAM_BLOB_ALIGNMENT: u64 = 9;

// Context init param constants
const VIRTGPU_CONTEXT_PARAM_CAPSET_ID: u64 = 0x0001;
const VIRTGPU_CONTEXT_PARAM_NUM_RINGS: u64 = 0x0002;
const VIRTGPU_CONTEXT_PARAM_POLL_RINGS_MASK: u64 = 0x0003;
const VIRTGPU_CONTEXT_PARAM_DEBUG_NAME: u64 = 0x0004;

// Execbuffer flags
const VIRTGPU_EXECBUF_FENCE_FD_IN: u32 = 0x01;
const VIRTGPU_EXECBUF_FENCE_FD_OUT: u32 = 0x02;
const VIRTGPU_EXECBUF_RING_IDX: u32 = 0x04;

// ---------------------------------------------------------------------------
// ioctl type aliases
// ---------------------------------------------------------------------------

mod ioctl_defs {
    use super::*;
    use crate::util::ioctl::{InOutData, ioc};

    /// DRM_IOCTL_VIRTGPU_EXECBUFFER   = 0xc0406442
    pub(super) type VirtgpuExecbuffer =
        ioc!(DRM_VIRTGPU_EXECBUFFER, b'd', 0x42, InOutData<DrmVirtgpuExecbuffer>);

    /// DRM_IOCTL_VIRTGPU_GETPARAM     = 0xc0406443
    pub(super) type VirtgpuGetparam =
        ioc!(DRM_VIRTGPU_GETPARAM, b'd', 0x43, InOutData<DrmVirtgpuGetparam>);

    /// DRM_IOCTL_VIRTGPU_RESOURCE_CREATE = 0xc0406444
    pub(super) type VirtgpuResourceCreate =
        ioc!(DRM_VIRTGPU_RESOURCE_CREATE, b'd', 0x44, InOutData<DrmVirtgpuResourceCreate>);

    /// DRM_IOCTL_VIRTGPU_RESOURCE_INFO = 0xc0406445
    pub(super) type VirtgpuResourceInfo =
        ioc!(DRM_VIRTGPU_RESOURCE_INFO, b'd', 0x45, InOutData<DrmVirtgpuResourceInfo>);

    /// DRM_IOCTL_VIRTGPU_TRANSFER_FROM_HOST = 0xc0406446
    pub(super) type VirtgpuTransferFromHost =
        ioc!(DRM_VIRTGPU_TRANSFER_FROM_HOST, b'd', 0x46, InOutData<DrmVirtgpu3dTransferFromHost>);

    /// DRM_IOCTL_VIRTGPU_TRANSFER_TO_HOST = 0xc0406447
    pub(super) type VirtgpuTransferToHost =
        ioc!(DRM_VIRTGPU_TRANSFER_TO_HOST, b'd', 0x47, InOutData<DrmVirtgpu3dTransferToHost>);

    /// DRM_IOCTL_VIRTGPU_WAIT          = 0xc0406448
    pub(super) type VirtgpuWait =
        ioc!(DRM_VIRTGPU_WAIT, b'd', 0x48, InOutData<DrmVirtgpu3dWait>);

    /// DRM_IOCTL_VIRTGPU_GET_CAPS      = 0xc0406449
    pub(super) type VirtgpuGetCaps =
        ioc!(DRM_VIRTGPU_GET_CAPS, b'd', 0x49, InOutData<DrmVirtgpuGetCaps>);

    /// DRM_IOCTL_VIRTGPU_CONTEXT_INIT  = 0xc040644b
    pub(super) type VirtgpuContextInit =
        ioc!(DRM_VIRTGPU_CONTEXT_INIT, b'd', 0x4b, InOutData<DrmVirtgpuContextInit>);

    /// DRM_IOCTL_VIRTGPU_MAP           = 0xc0406441
    pub(super) type VirtgpuMap =
        ioc!(DRM_VIRTGPU_MAP, b'd', 0x41, InOutData<DrmVirtgpuMap>);
}

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

use aster_virtio::device::gpu::{
    VIRTIO_GPU_CAPSET_VIRGL, VIRTIO_GPU_CAPSET_VIRGL2,
    VIRTIO_GPU_CMD_CTX_CREATE, VIRTIO_GPU_CMD_CTX_DESTROY,
    VIRTIO_GPU_CMD_RESOURCE_CREATE_3D, VIRTIO_GPU_CMD_SUBMIT_3D,
    VIRTIO_GPU_CMD_GET_CAPSET_INFO, VIRTIO_GPU_CMD_GET_CAPSET,
    VIRTIO_GPU_CMD_CTX_ATTACH_RESOURCE, VIRTIO_GPU_CMD_CTX_DETACH_RESOURCE,
    VIRTIO_GPU_CMD_TRANSFER_TO_HOST_3D, VIRTIO_GPU_CMD_TRANSFER_FROM_HOST_3D,
    VIRTIO_GPU_RESOURCE_BIND_BUFFER, VIRTIO_GPU_RESOURCE_BIND_RENDER_TARGET,
    VIRTIO_GPU_RESOURCE_BIND_SAMPLER,
    VIRTIO_GPU_RESOURCE_TARGET_TEXTURE_2D,
    VirtioGpuCtrlHdr, VirtioGpuCtxCreate, VirtioGpuCtxDestroy,
    VirtioGpuCtxResource, VirtioGpuResourceCreate3d, VirtioGpuCmdSubmit,
    VirtioGpuGetCapsetInfo, VirtioGpuGetCapset, VirtioGpuRespCapsetInfo,
    VirtioGpuTransferHost3d, VirtioGpuBox,
};

use crate::context::current_userspace;
use ostd::mm::VmIo;

/// GETPARAM: return device parameters queried by Mesa.
pub(super) fn virtgpu_getparam(
    _handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<b'd', { 0x43 }, true, crate::util::ioctl::InOutData<DrmVirtgpuGetparam>>,
) -> Result<i32> {
    let mut req = cmd.read()?;
    req.value = match req.param {
        VIRTGPU_PARAM_3D_FEATURES => 1,          // virgl 3D is supported
        VIRTGPU_PARAM_CAPSET_QUERY_FIX => 1,     // GET_CAPS handles non-zero versions
        VIRTGPU_PARAM_RESOURCE_BLOB => 0,         // no blob resources
        VIRTGPU_PARAM_HOST_VISIBLE => 0,          // no host-visible resources
        VIRTGPU_PARAM_CROSS_DEVICE => 0,          // no cross-device sharing
        VIRTGPU_PARAM_CONTEXT_INIT => 0,          // no context init extension
        VIRTGPU_PARAM_SUPPORTED_CAPSET_IDs => 0,  // query capsets individually
        VIRTGPU_PARAM_EXPLICIT_DEBUG_NAME => 0,   // no debug name support
        VIRTGPU_PARAM_BLOB_ALIGNMENT => 0,        // no blob alignment
        _ => {
            return_errno_with_message!(Errno::EINVAL, "unknown virtio-gpu parameter");
        }
    };
    cmd.write(&req)?;
    Ok(0)
}

/// RESOURCE_CREATE: create a 3D resource on the virtio-gpu device.
///
/// Maps a GEM buffer (via bo_handle) to a virtio-gpu 3D resource.
/// If bo_handle is 0, creates a resource without backing (for
/// render targets that are written to by the GPU).
pub(super) fn virtgpu_resource_create(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<b'd', { 0x44 }, true, crate::util::ioctl::InOutData<DrmVirtgpuResourceCreate>>,
) -> Result<i32> {
    let mut req = cmd.read()?;

    // Allocate a new virtio-gpu resource id
    let res_handle = handle.gpu_manager.gpu.next_resource_id.fetch_add(1, core::sync::atomic::Ordering::Relaxed);

    // If a GEM buffer handle is provided, look up the backing memory
    if req.bo_handle != 0 {
        let inner = handle.inner.lock();
        let object_id = inner
            .handles
            .get(&req.bo_handle)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?;
        let guard = handle.gpu_manager.gem_objects.lock();
        let obj = guard
            .get(object_id)
            .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;
        let base = handle.gpu_manager.pool_paddr()?;
        let addr = base + obj.buffer.offset;
        let size = obj.buffer.size as u32;

        // Create a 2D resource for the backing store (virtio-gpu resources
        // are always 2D; 3D resources are a superset with additional
        // target/bind attributes)
        handle.gpu_manager.gpu.resource_create_2d(res_handle, 2 /* X8R8G8B8 */, req.width, req.height).map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;
        handle.gpu_manager.gpu.attach_backing(res_handle, addr as u64, size).map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;
    }

    // Create the 3D resource on the virtio-gpu device
    if req.target != 0 {
        handle.gpu_manager.gpu.resource_create_3d(
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
        ).map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;
    }

    // Attach the resource to the virgl context
    if req.target != 0 {
        handle.gpu_manager.gpu.ctx_attach_resource(res_handle).map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;
    }

    req.res_handle = res_handle;
    req.size = 0; // Size is validated by the host during transfer
    req.stride = 0;
    cmd.write(&req)?;
    Ok(0)
}

/// RESOURCE_INFO: return information about a virtio-gpu resource.
pub(super) fn virtgpu_resource_info(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<b'd', { 0x45 }, true, crate::util::ioctl::InOutData<DrmVirtgpuResourceInfo>>,
) -> Result<i32> {
    let mut req = cmd.read()?;
    // For now, return the GEM buffer size as the resource size.
    // The blob_mem field is 0 for non-blob resources.
    let inner = handle.inner.lock();
    if let Some(object_id) = inner.handles.get(&req.bo_handle) {
        let guard = handle.gpu_manager.gem_objects.lock();
        if let Some(obj) = guard.get(object_id) {
            req.size = obj.buffer.size as u32;
        }
    }
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
    cmd: crate::util::ioctl::Ioctl<b'd', { 0x49 }, true, crate::util::ioctl::InOutData<DrmVirtgpuGetCaps>>,
) -> Result<i32> {
    let req = cmd.read()?;

    // Only virgl and virgl2 capsets are supported
    if req.cap_set_id != VIRTIO_GPU_CAPSET_VIRGL && req.cap_set_id != VIRTIO_GPU_CAPSET_VIRGL2 {
        return_errno_with_message!(Errno::EINVAL, "unsupported capset id");
    }

    // Query the capset info from the device
    let capset_info = handle.gpu_manager.gpu.get_capset_info(req.cap_set_id).map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;
    let capset_data = handle.gpu_manager.gpu.get_capset(req.cap_set_id, req.cap_set_ver).map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;

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

/// EXECBUFFER: submit a virgl command stream to the host.
///
/// This is the core ioctl for virgl rendering. Mesa encodes GL commands
/// in a virgl command buffer and submits them via this ioctl.
pub(super) fn virtgpu_execbuffer(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<b'd', { 0x42 }, true, crate::util::ioctl::InOutData<DrmVirtgpuExecbuffer>>,
) -> Result<i32> {
    let req = cmd.read()?;

    // Read the command buffer from userspace
    if req.size == 0 || req.command == 0 {
        return_errno_with_message!(Errno::EINVAL, "empty command buffer");
    }

    let mut cmd_buf = vec![0u8; req.size as usize];
    current_userspace!().read_bytes(req.command as usize, &mut cmd_buf)?;

    // Look up the GEM buffer handles for the resource list
    let mut bo_resource_ids = Vec::new();
    if req.num_bo_handles > 0 && req.bo_handles != 0 {
        let handle_count = req.num_bo_handles as usize;
        let mut bo_handles = vec![0u32; handle_count];
        let mut raw = vec![0u8; handle_count * 4];
        current_userspace!().read_bytes(req.bo_handles as usize, &mut raw)?;
        for (i, chunk) in raw.chunks_exact(4).enumerate() {
            bo_handles[i] = u32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
        }

        let inner = handle.inner.lock();
        let guard = handle.gpu_manager.gem_objects.lock();
        for bo_h in &bo_handles {
            if let Some(object_id) = inner.handles.get(bo_h) {
                if let Some(_obj) = guard.get(object_id) {
                    // For now, use the object_id as the virtio resource id
                    bo_resource_ids.push(*object_id);
                }
            }
        }
    }

    // Submit the command buffer to the host
    handle.gpu_manager.gpu.submit_3d(req.size, &cmd_buf).map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;

    // Always return fence_fd = -1 (no fence support for now)
    let mut resp = req;
    resp.fence_fd = -1;
    cmd.write(&resp)?;
    Ok(0)
}

/// CONTEXT_INIT: create a 3D context (virgl context) on the virtio-gpu device.
///
/// Mesa calls this to initialize the virgl rendering context. The capset
/// id determines which virgl version (virgl or virgl2) is used.
pub(super) fn virtgpu_context_init(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<b'd', { 0x4b }, true, crate::util::ioctl::InOutData<DrmVirtgpuContextInit>>,
) -> Result<i32> {
    let req = cmd.read()?;

    // Parse context parameters
    let mut capset_id = VIRTIO_GPU_CAPSET_VIRGL; // default to virgl
    let mut _num_rings = 1u32;
    let mut _debug_name: [u8; 64] = [0; 64];

    if req.num_params > 0 && req.ctx_set_params != 0 {
        let param_count = req.num_params as usize;
        let mut params = vec![DrmVirtgpuContextSetParam::default(); param_count];
        let param_size = size_of::<DrmVirtgpuContextSetParam>();
        let mut raw = vec![0u8; param_count * param_size];
        current_userspace!().read_bytes(req.ctx_set_params as usize, &mut raw)?;
        for (i, chunk) in raw.chunks_exact(param_size).enumerate() {
            // DrmVirtgpuContextSetParam is { param: u64, value: u64 } = 16 bytes
            let param = u64::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3], chunk[4], chunk[5], chunk[6], chunk[7]]);
            let value = u64::from_le_bytes([chunk[8], chunk[9], chunk[10], chunk[11], chunk[12], chunk[13], chunk[14], chunk[15]]);
            params[i] = DrmVirtgpuContextSetParam { param, value };
        }

        for param in &params {
            match param.param {
                VIRTGPU_CONTEXT_PARAM_CAPSET_ID => {
                    capset_id = param.value as u32;
                }
                VIRTGPU_CONTEXT_PARAM_NUM_RINGS => {
                    _num_rings = param.value as u32;
                }
                VIRTGPU_CONTEXT_PARAM_DEBUG_NAME => {
                    // Copy debug name from userspace
                    if param.value != 0 {
                        let _ = current_userspace!().read_bytes(param.value as usize, &mut _debug_name);
                    }
                }
                _ => {
                    // Ignore unknown context params
                }
            }
        }
    }

    // Create the virgl context on the virtio-gpu device
    handle.gpu_manager.gpu.ctx_create(capset_id, &_debug_name).map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;

    cmd.write(&req)?;
    Ok(0)
}

/// TRANSFER_TO_HOST: transfer data from guest to host for a 3D resource.
pub(super) fn virtgpu_transfer_to_host(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<b'd', { 0x47 }, true, crate::util::ioctl::InOutData<DrmVirtgpu3dTransferToHost>>,
) -> Result<i32> {
    let req = cmd.read()?;

    let inner = handle.inner.lock();
    let object_id = inner
        .handles
        .get(&req.bo_handle)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?;
    let guard = handle.gpu_manager.gem_objects.lock();
    let obj = guard
        .get(object_id)
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;
    let base = handle.gpu_manager.pool_paddr()?;
    let addr = base + obj.buffer.offset;

    handle.gpu_manager.gpu.transfer_to_host_3d(
        *object_id, // use object_id as resource handle
        req.box_.x, req.box_.y, req.box_.z,
        req.box_.w, req.box_.h, req.box_.d,
        req.offset as u64, req.level,
        req.stride, req.layer_stride,
    ).map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;

    cmd.write(&req)?;
    Ok(0)
}

/// TRANSFER_FROM_HOST: transfer data from host to guest for a 3D resource.
pub(super) fn virtgpu_transfer_from_host(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<b'd', { 0x46 }, true, crate::util::ioctl::InOutData<DrmVirtgpu3dTransferFromHost>>,
) -> Result<i32> {
    let req = cmd.read()?;

    let inner = handle.inner.lock();
    let object_id = inner
        .handles
        .get(&req.bo_handle)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?;
    let guard = handle.gpu_manager.gem_objects.lock();
    let obj = guard
        .get(object_id)
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;
    let base = handle.gpu_manager.pool_paddr()?;
    let addr = base + obj.buffer.offset;

    handle.gpu_manager.gpu.transfer_from_host_3d(
        *object_id,
        req.box_.x, req.box_.y, req.box_.z,
        req.box_.w, req.box_.h, req.box_.d,
        req.offset as u64, req.level,
        req.stride, req.layer_stride,
    ).map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;

    cmd.write(&req)?;
    Ok(0)
}

/// MAP: return the mmap offset for a GEM buffer.
pub(super) fn virtgpu_map(
    handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<b'd', { 0x41 }, true, crate::util::ioctl::InOutData<DrmVirtgpuMap>>,
) -> Result<i32> {
    let req = cmd.read()?;
    let inner = handle.inner.lock();
    let object_id = inner
        .handles
        .get(&req.handle)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?;
    let guard = handle.gpu_manager.gem_objects.lock();
    let obj = guard
        .get(object_id)
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;
    let mut resp = req;
    resp.offset = obj.buffer.offset as u64;
    cmd.write(&resp)?;
    Ok(0)
}

/// WAIT: wait for a resource to become idle (no-op for now).
pub(super) fn virtgpu_wait(
    _handle: &super::DriHandle,
    cmd: crate::util::ioctl::Ioctl<b'd', { 0x48 }, true, crate::util::ioctl::InOutData<DrmVirtgpu3dWait>>,
) -> Result<i32> {
    let req = cmd.read()?;
    // Always succeed — no async GPU operations to wait for.
    cmd.write(&req)?;
    Ok(0)
}