// SPDX-License-Identifier: MPL-2.0

//! Manages virtio-gpu devices.
//!
//! This module owns the global registry of discovered [`GpuDevice`] instances
//! and the wire types shared between the driver and the device. Virtio transport
//! initialization creates devices in [`device`], then registers them here.

use alloc::{collections::btree_map::BTreeMap, string::String, sync::Arc};

use ostd::sync::SpinLock;
use spin::Once;

use crate::device::gpu::device::GpuDevice;

mod config;
pub mod device;

pub const DEVICE_NAME: &str = "Virtio-GPU";

/// Registers a [`GpuDevice`] under `name`.
fn register_device(name: String, device: Arc<GpuDevice>) {
    let mut gpu_devs = GPU_DEVICE_TABLE.get().unwrap().lock();
    gpu_devs.insert(name, device);
}

/// Returns the first registered [`GpuDevice`].
pub fn first_device() -> Option<Arc<GpuDevice>> {
    let gpu_devs = GPU_DEVICE_TABLE.get().unwrap().lock();
    gpu_devs.values().next().cloned()
}

/// Initializes the gpu device registry.
pub(crate) fn init() {
    GPU_DEVICE_TABLE.call_once(|| SpinLock::new(BTreeMap::new()));
}

static GPU_DEVICE_TABLE: Once<SpinLock<BTreeMap<String, Arc<GpuDevice>>>> = Once::new();

/// Virtqueue indices (5.7.6.1).
pub const VQ_CONTROL: u16 = 0;
pub const VQ_CURSOR: u16 = 1;

/// Control request codes (5.7.6.2). The 2D block is a contiguous enum starting
/// at `VIRTIO_GPU_CMD_GET_DISPLAY_INFO = 0x0100`; the values below follow the
/// Linux `include/uapi/linux/virtio_gpu.h` layout exactly.
pub const VIRTIO_GPU_CMD_GET_DISPLAY_INFO: u32 = 0x0100;
pub const VIRTIO_GPU_CMD_RESOURCE_CREATE_2D: u32 = 0x0101;
pub const VIRTIO_GPU_CMD_RESOURCE_UNREF: u32 = 0x0102;
pub const VIRTIO_GPU_CMD_SET_SCANOUT: u32 = 0x0103;
pub const VIRTIO_GPU_CMD_RESOURCE_FLUSH: u32 = 0x0104;
pub const VIRTIO_GPU_CMD_TRANSFER_TO_HOST_2D: u32 = 0x0105;
pub const VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING: u32 = 0x0106;
pub const VIRTIO_GPU_CMD_RESOURCE_DETACH_BACKING: u32 = 0x0107;
pub const VIRTIO_GPU_CMD_GET_CAPSET_INFO: u32 = 0x0108;
pub const VIRTIO_GPU_CMD_GET_CAPSET: u32 = 0x0109;
pub const VIRTIO_GPU_CMD_GET_EDID: u32 = 0x010a;

pub const VIRTIO_GPU_CMD_UPDATE_CURSOR: u32 = 0x0300;
pub const VIRTIO_GPU_CMD_MOVE_CURSOR: u32 = 0x0301;

/// Control response codes (5.7.6.3).
pub const VIRTIO_GPU_RESP_OK_NODATA: u32 = 0x1100;
pub const VIRTIO_GPU_RESP_OK_DISPLAY_INFO: u32 = 0x1101;
pub const VIRTIO_GPU_RESP_ERR_UNSPEC: u32 = 0x1200;
pub const VIRTIO_GPU_RESP_ERR_OUT_OF_MEMORY: u32 = 0x1201;
pub const VIRTIO_GPU_RESP_ERR_INVALID_SCANOUT_ID: u32 = 0x1202;
pub const VIRTIO_GPU_RESP_ERR_INVALID_RESOURCE_ID: u32 = 0x1203;
pub const VIRTIO_GPU_RESP_ERR_INVALID_CONTEXT_ID: u32 = 0x1204;
pub const VIRTIO_GPU_RESP_ERR_INVALID_PARAMETER: u32 = 0x1205;

/// Pixel formats accepted by `RESOURCE_CREATE_2D` (5.7.6.5).
pub const VIRTIO_GPU_FORMAT_B8G8R8A8_UNORM: u32 = 1;
pub const VIRTIO_GPU_FORMAT_B8G8R8X8_UNORM: u32 = 2;
pub const VIRTIO_GPU_FORMAT_A8R8G8B8_UNORM: u32 = 3;
pub const VIRTIO_GPU_FORMAT_X8R8G8B8_UNORM: u32 = 4;

/// Maximum number of scanouts described by a single display-info response.
pub const MAX_SCANOUTS: usize = 16;

/// Common control header (5.7.6.3).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuCtrlHdr {
    pub type_: u32,
    pub flags: u32,
    pub fence_id: u64,
    pub ctx_id: u32,
    pub padding: u32,
}

/// A 2D rectangle (5.7.6.4).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuRect {
    pub x: u32,
    pub y: u32,
    pub width: u32,
    pub height: u32,
}

/// `RESOURCE_CREATE_2D` request (5.7.6.5.3).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuResourceCreate2d {
    pub hdr: VirtioGpuCtrlHdr,
    pub resource_id: u32,
    pub format: u32,
    pub width: u32,
    pub height: u32,
}

/// A single guest-memory entry for `RESOURCE_ATTACH_BACKING` (5.7.6.5.2).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuMemEntry {
    pub addr: u64,
    pub length: u32,
    pub padding: u32,
}

/// `RESOURCE_ATTACH_BACKING` request (5.7.6.5.1), without the trailing
/// [`VirtioGpuMemEntry`] array that follows it on the wire.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuResourceAttachBacking {
    pub hdr: VirtioGpuCtrlHdr,
    pub resource_id: u32,
    pub nr_entries: u32,
}

/// `SET_SCANOUT` request (5.7.6.5.4).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuSetScanout {
    pub hdr: VirtioGpuCtrlHdr,
    pub r: VirtioGpuRect,
    pub scanout_id: u32,
    pub resource_id: u32,
}

/// `TRANSFER_TO_HOST_2D` request (5.7.6.5.5).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuTransferToHost2d {
    pub hdr: VirtioGpuCtrlHdr,
    pub r: VirtioGpuRect,
    pub offset: u64,
    pub resource_id: u32,
    pub padding: u32,
}

/// `RESOURCE_FLUSH` request (5.7.6.5.6).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuResourceFlush {
    pub hdr: VirtioGpuCtrlHdr,
    pub r: VirtioGpuRect,
    pub resource_id: u32,
    pub padding: u32,
}

/// `RESOURCE_UNREF` request (5.7.6.5.7).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuResourceUnref {
    pub hdr: VirtioGpuCtrlHdr,
    pub resource_id: u32,
    pub padding: u32,
}

/// Cursor location carried on the cursor virtqueue (5.7.6.7).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuCursorPos {
    pub scanout_id: u32,
    pub x: u32,
    pub y: u32,
    pub padding: u32,
}

/// `UPDATE_CURSOR` and `MOVE_CURSOR` request (5.7.6.7).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuUpdateCursor {
    pub hdr: VirtioGpuCtrlHdr,
    pub pos: VirtioGpuCursorPos,
    pub resource_id: u32,
    pub hot_x: u32,
    pub hot_y: u32,
    pub padding: u32,
}

/// One scanout entry of a display-info response (5.7.6.6.1).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioGpuDisplayOne {
    pub r: VirtioGpuRect,
    pub enabled: u32,
    pub flags: u32,
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn cursor_wire_layout_matches_virtio_gpu() {
        assert_eq!(size_of::<VirtioGpuCursorPos>(), 16);
        assert_eq!(size_of::<VirtioGpuUpdateCursor>(), 56);

        let request = VirtioGpuUpdateCursor {
            hdr: VirtioGpuCtrlHdr {
                type_: VIRTIO_GPU_CMD_UPDATE_CURSOR,
                flags: 0,
                fence_id: 0,
                ctx_id: 0,
                padding: 0,
            },
            pos: VirtioGpuCursorPos {
                scanout_id: 3,
                x: 17,
                y: 29,
                padding: 0,
            },
            resource_id: 41,
            hot_x: 5,
            hot_y: 7,
            padding: 0,
        };
        assert_eq!(request.hdr.type_, VIRTIO_GPU_CMD_UPDATE_CURSOR);
        assert_eq!(request.pos.scanout_id, 3);
        assert_eq!(request.resource_id, 41);
        assert_eq!((request.hot_x, request.hot_y), (5, 7));
    }
}
