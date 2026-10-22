// SPDX-License-Identifier: MPL-2.0

use alloc::{string::String, sync::Arc, vec, vec::Vec};
use core::{
    hint::spin_loop,
    sync::atomic::{AtomicU32, Ordering},
};

use aster_util::mem_obj_slice::Slice;
use ostd::{
    debug, info,
    mm::{dma::DmaStream, VmIo},
    sync::SpinLock,
};

use super::{
    ctrl_hdr, register_device, VirtioGpuCtrlHdr, VirtioGpuMemEntry, VirtioGpuRect,
    VirtioGpuResourceAttachBacking, VirtioGpuResourceCreate2d, VirtioGpuResourceFlush,
    VirtioGpuResourceUnref, VirtioGpuRespDisplayInfo, VirtioGpuSetScanout,
    VirtioGpuTransferToHost2d, MAX_SCANOUTS, VIRTIO_GPU_CMD_GET_DISPLAY_INFO,
    VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING, VIRTIO_GPU_CMD_RESOURCE_CREATE_2D,
    VIRTIO_GPU_CMD_RESOURCE_FLUSH, VIRTIO_GPU_CMD_RESOURCE_UNREF, VIRTIO_GPU_CMD_SET_SCANOUT,
    VIRTIO_GPU_CMD_TRANSFER_TO_HOST_2D, VIRTIO_GPU_RESP_OK_DISPLAY_INFO, VIRTIO_GPU_RESP_OK_NODATA,
};
use crate::{
    device::{gpu::config::VirtioGpuConfig, VirtioDeviceError},
    queue::VirtQueue,
    transport::DeviceTransport,
};

/// Number of descriptors per virtqueue.
const QUEUE_SIZE: u16 = 64;

/// Control buffer pages. The request area holds the attach-backing entry
/// array, the response area follows it; with 8 pages a 6 MiB resource fits
/// (1280x720x4 and 1024x768x4 both do).
const CTRL_PAGES: usize = 8;
const CTRL_REQ_OFFSET: usize = 0;
const CTRL_RESP_OFFSET: usize = 6 * 4096;
const CTRL_REQ_MAX: usize = CTRL_RESP_OFFSET;

/// Fallback mode when the host reports no enabled scanout (headless QEMU).
const FALLBACK_WIDTH: u32 = 1024;
const FALLBACK_HEIGHT: u32 = 768;

/// A virtio-gpu device exposing its 2D command set.
pub struct GpuDevice {
    /// Keeps the transport alive for the lifetime of the device.
    #[expect(dead_code)]
    transport: SpinLock<DeviceTransport>,
    control_queue: SpinLock<VirtQueue>,
    control_buf: Arc<DmaStream>,
    /// The rect of each scanout the host reported, index = scanout id.
    scanouts: Vec<VirtioGpuRect>,
    /// The resource currently shown on each scanout, index = scanout id.
    scanout_resources: SpinLock<Vec<Option<u32>>>,
    next_resource_id: AtomicU32,
}

impl GpuDevice {
    pub(crate) fn negotiate_features(features: u64) -> u64 {
        // The 2D path needs no optional feature; in particular do not take
        // VIRGL, so the host keeps answering with plain 2D resources.
        let _ = features;
        0
    }

    pub(crate) fn init(mut device_transport: DeviceTransport) -> Result<(), VirtioDeviceError> {
        let config_manager = VirtioGpuConfig::new_manager(device_transport.as_ref());
        let config = config_manager.read_config();
        debug!("virtio_gpu_config = {:?}", config);

        let mut control_queue = VirtQueue::new(0, QUEUE_SIZE, device_transport.as_mut())?;
        let control_buf = Arc::new(
            DmaStream::alloc(CTRL_PAGES, false).map_err(VirtioDeviceError::ResourceAlloc)?,
        );

        // Mark the device ready before issuing the first control request.
        device_transport.finish_init();

        let display_info = query_display_info(&mut control_queue, &control_buf)?;
        let scanouts = pick_scanouts(&display_info, config.num_scanouts);
        let first = scanouts[0];
        info!(
            "virtio-gpu: {} scanout(s), using {}x{}",
            scanouts.len(),
            first.width,
            first.height
        );

        let device = Arc::new(Self {
            transport: SpinLock::new(device_transport),
            control_queue: SpinLock::new(control_queue),
            control_buf,
            scanout_resources: SpinLock::new(vec![None; scanouts.len()]),
            scanouts,
            next_resource_id: AtomicU32::new(1),
        });

        register_device(String::from("virtio_gpu.0"), device);
        Ok(())
    }

    /// Returns the dimensions of the primary scanout.
    pub fn dimensions(&self) -> (u32, u32) {
        let rect = self.scanouts[0];
        (rect.width, rect.height)
    }

    /// Returns how many scanouts (display pipelines) the host exposes.
    pub fn scanout_count(&self) -> usize {
        self.scanouts.len()
    }

    /// Returns the dimensions of one scanout.
    pub fn scanout_dimensions(&self, scanout_id: usize) -> (u32, u32) {
        let rect = self
            .scanouts
            .get(scanout_id)
            .copied()
            .unwrap_or(self.scanouts[0]);
        (rect.width, rect.height)
    }

    /// Reserves a resource id from the device's single id space.
    pub fn reserve_resource_id(&self) -> u32 {
        self.next_resource_id.fetch_add(1, Ordering::Relaxed)
    }

    /// Creates a 2D resource.
    pub fn resource_create_2d(
        &self,
        resource_id: u32,
        format: u32,
        width: u32,
        height: u32,
    ) -> Result<(), VirtioDeviceError> {
        let request = VirtioGpuResourceCreate2d {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_RESOURCE_CREATE_2D),
            resource_id,
            format,
            width,
            height,
        };
        let mut queue = self.control_queue.lock();
        let code = control_cmd(&mut queue, &self.control_buf, &request)?;
        check_ok(code)
    }

    /// Attaches guest memory to a resource.
    ///
    /// The protocol carries one contiguous run per entry, so a resource backed
    /// by non-contiguous pages is attached with one entry per page.
    pub fn attach_backing(
        &self,
        resource_id: u32,
        entries: &[VirtioGpuMemEntry],
    ) -> Result<(), VirtioDeviceError> {
        if entries.is_empty() {
            return Err(VirtioDeviceError::UnsupportedConfig);
        }
        let header_len = size_of::<VirtioGpuResourceAttachBacking>();
        let entries_len = size_of::<VirtioGpuMemEntry>() * entries.len();
        let req_len = header_len + entries_len;
        if req_len > CTRL_REQ_MAX {
            return Err(VirtioDeviceError::UnsupportedConfig);
        }

        let request = VirtioGpuResourceAttachBacking {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING),
            resource_id,
            nr_entries: entries.len() as u32,
        };
        let buf = Slice::new(
            self.control_buf.clone(),
            CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len,
        );
        buf.write_val(0, &request)
            .map_err(VirtioDeviceError::ResourceAlloc)?;
        for (index, entry) in entries.iter().enumerate() {
            buf.write_val(header_len + index * size_of::<VirtioGpuMemEntry>(), entry)
                .map_err(VirtioDeviceError::ResourceAlloc)?;
        }

        let mut queue = self.control_queue.lock();
        let code = submit_control(&mut queue, &self.control_buf, req_len)?;
        check_ok(code)
    }

    /// Points one scanout at a resource, or disables it with `resource_id == 0`.
    pub fn set_scanout(
        &self,
        scanout_id: u32,
        resource_id: u32,
        rect: VirtioGpuRect,
    ) -> Result<(), VirtioDeviceError> {
        let request = VirtioGpuSetScanout {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_SET_SCANOUT),
            r: rect,
            scanout_id,
            resource_id,
        };
        let mut queue = self.control_queue.lock();
        let code = control_cmd(&mut queue, &self.control_buf, &request)?;
        check_ok(code)
    }

    /// Transfers a guest region of a resource to the host.
    pub fn transfer_to_host_2d(
        &self,
        resource_id: u32,
        rect: VirtioGpuRect,
        offset: u64,
    ) -> Result<(), VirtioDeviceError> {
        let request = VirtioGpuTransferToHost2d {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_TRANSFER_TO_HOST_2D),
            r: rect,
            offset,
            resource_id,
            padding: 0,
        };
        let mut queue = self.control_queue.lock();
        let code = control_cmd(&mut queue, &self.control_buf, &request)?;
        check_ok(code)
    }

    /// Flushes a resource region to the display.
    pub fn flush(&self, resource_id: u32, rect: VirtioGpuRect) -> Result<(), VirtioDeviceError> {
        let request = VirtioGpuResourceFlush {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_RESOURCE_FLUSH),
            r: rect,
            resource_id,
            padding: 0,
        };
        let mut queue = self.control_queue.lock();
        let code = control_cmd(&mut queue, &self.control_buf, &request)?;
        check_ok(code)
    }

    /// Releases a resource and its backing.
    pub fn resource_unref(&self, resource_id: u32) -> Result<(), VirtioDeviceError> {
        let request = VirtioGpuResourceUnref {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_RESOURCE_UNREF),
            resource_id,
            padding: 0,
        };
        let mut queue = self.control_queue.lock();
        let code = control_cmd(&mut queue, &self.control_buf, &request)?;
        check_ok(code)
    }

    /// Presents a resource on one scanout and tracks it for the next present.
    pub fn present(
        &self,
        scanout_id: u32,
        resource_id: u32,
        rect: VirtioGpuRect,
    ) -> Result<(), VirtioDeviceError> {
        let to_unref = {
            let mut resources = self.scanout_resources.lock();
            let index = scanout_id as usize;
            let previous = resources[index].replace(resource_id);
            // A resource still shown on another scanout must not be unrefed.
            match previous {
                Some(previous)
                    if previous != resource_id
                        && !resources
                            .iter()
                            .enumerate()
                            .any(|(other_index, other)| {
                                other_index != index && *other == Some(previous)
                            }) =>
                {
                    Some(previous)
                }
                _ => None,
            }
        };
        if let Some(previous) = to_unref {
            let _ = self.resource_unref(previous);
        }
        self.set_scanout(scanout_id, resource_id, rect)?;
        self.transfer_to_host_2d(resource_id, rect, 0)?;
        self.flush(resource_id, rect)
    }

    /// Stops scanout on one scanout.
    pub fn disable_scanout(&self, scanout_id: u32) -> Result<(), VirtioDeviceError> {
        self.scanout_resources.lock()[scanout_id as usize] = None;
        let rect = self.scanouts[scanout_id as usize];
        self.set_scanout(scanout_id, 0, rect)
    }
}

/// Builds the per-scanout rect list from the host's display info.
///
/// `config.num_scanouts` is what the host advertises; a scanout without an
/// enabled rect keeps the fallback mode so a guest can still modeset it.
fn pick_scanouts(
    display_info: &VirtioGpuRespDisplayInfo,
    num_scanouts: u32,
) -> Vec<VirtioGpuRect> {
    let count = (num_scanouts as usize).clamp(1, MAX_SCANOUTS);
    let mut rects = vec![
        VirtioGpuRect {
            x: 0,
            y: 0,
            width: FALLBACK_WIDTH,
            height: FALLBACK_HEIGHT,
        };
        count
    ];
    for (index, mode) in display_info.pmodes.iter().take(count).enumerate() {
        if mode.enabled != 0 && mode.r.width > 0 && mode.r.height > 0 {
            rects[index] = mode.r;
        }
    }
    rects
}

fn query_display_info(
    queue: &mut VirtQueue,
    buf: &Arc<DmaStream>,
) -> Result<VirtioGpuRespDisplayInfo, VirtioDeviceError> {
    let request = VirtioGpuCtrlHdr {
        type_: VIRTIO_GPU_CMD_GET_DISPLAY_INFO,
        ..VirtioGpuCtrlHdr::default()
    };
    let req_slice = Slice::new(
        buf.clone(),
        CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + size_of::<VirtioGpuCtrlHdr>(),
    );
    req_slice
        .write_val(0, &request)
        .map_err(VirtioDeviceError::ResourceAlloc)?;
    let resp_len = size_of::<VirtioGpuRespDisplayInfo>();
    let code = submit_control(queue, buf, size_of::<VirtioGpuCtrlHdr>())?;
    if code != VIRTIO_GPU_RESP_OK_DISPLAY_INFO {
        return Err(VirtioDeviceError::UnsupportedConfig);
    }
    let resp_slice = Slice::new(buf.clone(), CTRL_RESP_OFFSET..CTRL_RESP_OFFSET + resp_len);
    resp_slice
        .read_val::<VirtioGpuRespDisplayInfo>(0)
        .map_err(VirtioDeviceError::ResourceAlloc)
}

/// Submits a request already written at [`CTRL_REQ_OFFSET`] and returns the
/// response type code.
fn submit_control(
    queue: &mut VirtQueue,
    buf: &Arc<DmaStream>,
    req_len: usize,
) -> Result<u32, VirtioDeviceError> {
    let resp_len = size_of::<VirtioGpuCtrlHdr>();
    let req_slice = Slice::new(buf.clone(), CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len);
    req_slice
        .sync_to_device()
        .map_err(VirtioDeviceError::ResourceAlloc)?;
    let resp_slice = Slice::new(buf.clone(), CTRL_RESP_OFFSET..CTRL_RESP_OFFSET + resp_len);
    queue
        .add_dma_bufs(&[&req_slice], &[&resp_slice])
        .map_err(|_| VirtioDeviceError::UnsupportedConfig)?;
    if queue.should_notify() {
        queue.notify();
    }
    loop {
        if queue.pop_used_with_min_bytes(resp_len).is_ok() {
            break;
        }
        spin_loop();
    }
    resp_slice
        .sync_from_device()
        .map_err(VirtioDeviceError::ResourceAlloc)?;
    resp_slice
        .read_val::<u32>(0)
        .map_err(VirtioDeviceError::ResourceAlloc)
}

fn control_cmd<T: ostd_pod::Pod>(
    queue: &mut VirtQueue,
    buf: &Arc<DmaStream>,
    request: &T,
) -> Result<u32, VirtioDeviceError> {
    let req_len = size_of::<T>();
    let req_slice = Slice::new(buf.clone(), CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len);
    req_slice
        .write_val(0, request)
        .map_err(VirtioDeviceError::ResourceAlloc)?;
    submit_control(queue, buf, req_len)
}

fn check_ok(code: u32) -> Result<(), VirtioDeviceError> {
    if code == VIRTIO_GPU_RESP_OK_NODATA {
        Ok(())
    } else {
        Err(VirtioDeviceError::UnsupportedConfig)
    }
}
