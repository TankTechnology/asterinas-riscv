// SPDX-License-Identifier: MPL-2.0

//! Manages virtio-gpu devices.
//!
//! The 2D command set is enough for scanout of DRM framebuffers: query the
//! display, create a 2D resource, attach guest pages as its backing, set it as
//! scanout, then transfer and flush dirty regions.

use alloc::{collections::btree_map::BTreeMap, string::String, sync::Arc};

use ostd::sync::SpinLock;
use spin::Once;

use crate::device::gpu::device::GpuDevice;

mod config;
pub mod device;

pub const DEVICE_NAME: &str = "Virtio-GPU";

/// Virtqueue indices (virtio-gpu 5.7.6.1).
pub const VQ_CONTROL: u16 = 0;
pub const VQ_CURSOR: u16 = 1;

/// Control request codes (virtio-gpu 5.7.6.2).
pub const VIRTIO_GPU_CMD_GET_DISPLAY_INFO: u32 = 0x0100;
pub const VIRTIO_GPU_CMD_RESOURCE_CREATE_2D: u32 = 0x0101;
pub const VIRTIO_GPU_CMD_RESOURCE_UNREF: u32 = 0x0102;
pub const VIRTIO_GPU_CMD_SET_SCANOUT: u32 = 0x0103;
pub const VIRTIO_GPU_CMD_RESOURCE_FLUSH: u32 = 0x0104;
pub const VIRTIO_GPU_CMD_TRANSFER_TO_HOST_2D: u32 = 0x0105;
pub const VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING: u32 = 0x0106;

/// Control response codes (virtio-gpu 5.7.6.3).
pub const VIRTIO_GPU_RESP_OK_NODATA: u32 = 0x1100;
pub const VIRTIO_GPU_RESP_OK_DISPLAY_INFO: u32 = 0x1101;

/// Pixel formats accepted by `RESOURCE_CREATE_2D` (virtio-gpu 5.7.6.5).
pub const VIRTIO_GPU_FORMAT_B8G8R8A8_UNORM: u32 = 1;
pub const VIRTIO_GPU_FORMAT_B8G8R8X8_UNORM: u32 = 2;

/// Maximum number of scanouts described by a single display-info response.
pub const MAX_SCANOUTS: usize = 16;

/// Registers a [`GpuDevice`] under `name`.
fn register_device(name: String, device: Arc<GpuDevice>) {
    let mut gpu_devs = GPU_DEVICE_TABLE.get().unwrap().lock();
    gpu_devs.insert(name, device);
}

/// Returns the first registered [`GpuDevice`], if there is one.
pub fn first_device() -> Option<Arc<GpuDevice>> {
    let gpu_devs = GPU_DEVICE_TABLE.get()?.lock();
    gpu_devs.values().next().cloned()
}

/// Initializes the gpu device registry.
pub(crate) fn init() {
    GPU_DEVICE_TABLE.call_once(|| SpinLock::new(BTreeMap::new()));
}

static GPU_DEVICE_TABLE: Once<SpinLock<BTreeMap<String, Arc<GpuDevice>>>> = Once::new();

/// `virtio_gpu_ctrl_hdr` (virtio-gpu 5.7.6).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct VirtioGpuCtrlHdr {
    pub type_: u32,
    pub flags: u32,
    pub fence_id: u64,
    pub ctx_id: u32,
    pub padding: u32,
}

/// `virtio_gpu_rect`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct VirtioGpuRect {
    pub x: u32,
    pub y: u32,
    pub width: u32,
    pub height: u32,
}

/// `virtio_gpu_display_one`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct VirtioGpuDisplayOne {
    pub r: VirtioGpuRect,
    pub enabled: u32,
    pub flags: u32,
}

/// `virtio_gpu_resp_display_info` (virtio-gpu 5.7.6.4).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuRespDisplayInfo {
    pub hdr: VirtioGpuCtrlHdr,
    pub pmodes: [VirtioGpuDisplayOne; MAX_SCANOUTS],
}

/// `virtio_gpu_resource_create_2d` (virtio-gpu 5.7.6.5).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct VirtioGpuResourceCreate2d {
    pub hdr: VirtioGpuCtrlHdr,
    pub resource_id: u32,
    pub format: u32,
    pub width: u32,
    pub height: u32,
}

/// `virtio_gpu_resource_attach_backing` (virtio-gpu 5.7.6.5). The
/// `VirtioGpuMemEntry` array follows this header on the wire.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct VirtioGpuResourceAttachBacking {
    pub hdr: VirtioGpuCtrlHdr,
    pub resource_id: u32,
    pub nr_entries: u32,
}

/// `virtio_gpu_mem_entry`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct VirtioGpuMemEntry {
    pub addr: u64,
    pub length: u32,
    pub padding: u32,
}

/// `virtio_gpu_set_scanout` (virtio-gpu 5.7.6.5).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct VirtioGpuSetScanout {
    pub hdr: VirtioGpuCtrlHdr,
    pub r: VirtioGpuRect,
    pub scanout_id: u32,
    pub resource_id: u32,
}

/// `virtio_gpu_transfer_to_host_2d` (virtio-gpu 5.7.6.5).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct VirtioGpuTransferToHost2d {
    pub hdr: VirtioGpuCtrlHdr,
    pub r: VirtioGpuRect,
    pub offset: u64,
    pub resource_id: u32,
    pub padding: u32,
}

/// `virtio_gpu_resource_flush` (virtio-gpu 5.7.6.5).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct VirtioGpuResourceFlush {
    pub hdr: VirtioGpuCtrlHdr,
    pub r: VirtioGpuRect,
    pub resource_id: u32,
    pub padding: u32,
}

/// `virtio_gpu_resource_unref` (virtio-gpu 5.7.6.5).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct VirtioGpuResourceUnref {
    pub hdr: VirtioGpuCtrlHdr,
    pub resource_id: u32,
    pub padding: u32,
}

impl Default for VirtioGpuRespDisplayInfo {
    fn default() -> Self {
        Self {
            hdr: VirtioGpuCtrlHdr::default(),
            pmodes: [VirtioGpuDisplayOne::default(); MAX_SCANOUTS],
        }
    }
}

pub(super) fn ctrl_hdr(type_: u32) -> VirtioGpuCtrlHdr {
    VirtioGpuCtrlHdr {
        type_,
        ..VirtioGpuCtrlHdr::default()
    }
}
