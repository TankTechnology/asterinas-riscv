// SPDX-License-Identifier: MPL-2.0

//! Implements virtio-gpu device instances (device ID 16).
//!
//! The driver covers the 2D control-queue and hardware-cursor paths. EDID and
//! the virgl 3D path remain outside this milestone.

use alloc::{format, sync::Arc, vec::Vec};
use core::{
    hint::spin_loop,
    sync::atomic::{AtomicU32, AtomicUsize, Ordering},
};

use aster_util::mem_obj_slice::Slice;
use ostd::{
    mm::{HasDaddr, PAGE_SIZE, VmIo, dma::DmaStream},
    sync::{Mutex, SpinLock},
};

use super::{
    MAX_SCANOUTS, VIRTIO_GPU_CMD_GET_DISPLAY_INFO, VIRTIO_GPU_CMD_MOVE_CURSOR,
    VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING, VIRTIO_GPU_CMD_RESOURCE_CREATE_2D,
    VIRTIO_GPU_CMD_RESOURCE_FLUSH, VIRTIO_GPU_CMD_RESOURCE_UNREF, VIRTIO_GPU_CMD_SET_SCANOUT,
    VIRTIO_GPU_CMD_TRANSFER_TO_HOST_2D, VIRTIO_GPU_CMD_UPDATE_CURSOR,
    VIRTIO_GPU_FORMAT_B8G8R8A8_UNORM, VIRTIO_GPU_FORMAT_B8G8R8X8_UNORM,
    VIRTIO_GPU_RESP_OK_DISPLAY_INFO, VIRTIO_GPU_RESP_OK_NODATA, VQ_CONTROL, VQ_CURSOR,
    VirtioGpuCtrlHdr, VirtioGpuCursorPos, VirtioGpuDisplayOne, VirtioGpuMemEntry, VirtioGpuRect,
    VirtioGpuResourceAttachBacking, VirtioGpuResourceCreate2d, VirtioGpuResourceFlush,
    VirtioGpuResourceUnref, VirtioGpuSetScanout, VirtioGpuTransferToHost2d, VirtioGpuUpdateCursor,
    config::VirtioGpuConfig,
};
use crate::{
    device::{VirtioDeviceError, gpu::register_device},
    queue::VirtQueue,
    transport::DeviceTransport,
};

/// Number of descriptors per virtqueue.
const QUEUE_SIZE: u16 = 64;

/// Cursor requests have no device-written response body.
const CURSOR_COMPLETION_BYTES: usize = 0;

/// Control-buffer layout (single page): the request is written at the start of
/// the page and the response is read from a fixed offset, keeping the two areas
/// disjoint. The largest response (`GET_DISPLAY_INFO`, 408 bytes) still fits.
const CTRL_REQ_OFFSET: usize = 0;
const CTRL_RESP_OFFSET: usize = 1024;

/// Resource and scanout ids used by the MVP (there is only one of each).
const RESOURCE_ID: u32 = 1;
const SCANOUT_ID: u32 = 0;

/// Bytes per pixel of the B8G8R8X8 backing store.
const BPP: usize = 4;

/// A virtio-gpu device.
pub struct GpuDevice {
    /// Keeps the virtio transport alive for the device's lifetime. The control
    /// queue borrows it during `init` and holds its own handle afterwards, so
    /// this field is never read directly.
    #[expect(dead_code)]
    transport: SpinLock<DeviceTransport>,
    control_queue: SpinLock<VirtQueue>,
    cursor_queue: SpinLock<VirtQueue>,
    control_buf: Arc<DmaStream>,
    cursor_buf: Arc<DmaStream>,
    /// Backing memory of the scanout resource, in B8G8R8X8.
    framebuffer: Arc<DmaStream>,
    scanout_width: u32,
    scanout_height: u32,
    /// Resource id of the most recent framebuffer presented via
    /// [`present_framebuffer`], tracked so it can be unref'd before the next one.
    present_resource: SpinLock<Option<u32>>,
    /// Next resource id handed out by [`present_framebuffer`].
    next_resource_id: AtomicU32,
    /// Serializes multi-command cursor resource transactions without spinning.
    cursor_operation: Mutex<()>,
    /// Host-visible cursor resource, or zero when the cursor is hidden.
    cursor_resource: AtomicU32,
}

impl GpuDevice {
    pub(crate) fn negotiate_features(_features: u64) -> u64 {
        // The MVP drives only the plain 2D path, so clear every device-specific
        // feature (virgl, EDID, resource UUID, blob, context init).
        0
    }

    pub(crate) fn init(mut device_transport: DeviceTransport) -> Result<(), VirtioDeviceError> {
        let config_manager = VirtioGpuConfig::new_manager(device_transport.as_ref());
        let config = config_manager.read_config();
        ostd::debug!("virtio_gpu_config = {:?}", config);

        let mut control_queue = VirtQueue::new(VQ_CONTROL, QUEUE_SIZE, device_transport.as_mut())?;
        let cursor_size = cursor_queue_size(device_transport.max_queue_size(VQ_CURSOR)?)
            .ok_or(VirtioDeviceError::InvalidQueueArgs)?;
        let cursor_queue = VirtQueue::new(VQ_CURSOR, cursor_size, device_transport.as_mut())?;
        let control_buf =
            Arc::new(DmaStream::alloc(1, false).map_err(VirtioDeviceError::ResourceAlloc)?);
        let cursor_buf =
            Arc::new(DmaStream::alloc(1, false).map_err(VirtioDeviceError::ResourceAlloc)?);

        // Mark the device ready before issuing the first control request.
        device_transport.finish_init();

        let (scanout_width, scanout_height) =
            query_display_info(&mut control_queue, &control_buf, config.num_scanouts)?;
        ostd::info!(
            "virtio-gpu: {} scanout(s), primary {}x{}",
            config.num_scanouts,
            scanout_width,
            scanout_height
        );
        if scanout_width == 0 || scanout_height == 0 {
            ostd::warn!("virtio-gpu reported an empty scanout; leaving it unconfigured");
            return Err(VirtioDeviceError::UnsupportedConfig);
        }

        let framebuffer = alloc_framebuffer(scanout_width, scanout_height)
            .map_err(VirtioDeviceError::ResourceAlloc)?;

        let device = Arc::new(Self {
            transport: SpinLock::new(device_transport),
            control_queue: SpinLock::new(control_queue),
            cursor_queue: SpinLock::new(cursor_queue),
            control_buf,
            cursor_buf,
            framebuffer,
            scanout_width,
            scanout_height,
            present_resource: SpinLock::new(None),
            // Resource id 1 is reserved for the boot-time test pattern.
            next_resource_id: AtomicU32::new(2),
            cursor_operation: Mutex::new(()),
            cursor_resource: AtomicU32::new(0),
        });

        register_device(
            format!(
                "virtio_gpu.{}",
                GPU_DEVICE_ID.fetch_add(1, Ordering::Relaxed)
            ),
            device.clone(),
        );

        device.render_test_pattern();
        Ok(())
    }

    /// Returns the scanout width in pixels.
    pub fn width(&self) -> u32 {
        self.scanout_width
    }

    /// Returns the scanout height in pixels.
    pub fn height(&self) -> u32 {
        self.scanout_height
    }

    /// Presents an externally-owned guest buffer as scanout 0.
    ///
    /// Runs the full 2D pipeline for a caller-provided framebuffer: create a
    /// resource, attach `addr`/`size` of guest memory as its backing store, set
    /// it as scanout 0, transfer the pixels to the host, and flush. Any
    /// previously presented resource is unref'd first so repeated present calls
    /// (e.g. page flips or mode switches) do not leak resources.
    pub fn present_framebuffer(
        &self,
        addr: u64,
        size: u32,
        width: u32,
        height: u32,
    ) -> Result<(), VirtioDeviceError> {
        if let Some(prev) = *self.present_resource.lock() {
            // Best-effort cleanup: a stale resource id must not wedge a later present.
            let _ = self.resource_unref(prev);
        }

        let resource_id = self.next_resource_id.fetch_add(1, Ordering::Relaxed);
        self.resource_create_2d(resource_id, width, height)?;
        self.attach_backing(resource_id, addr, size)?;

        let r = VirtioGpuRect {
            x: 0,
            y: 0,
            width,
            height,
        };
        self.set_scanout(SCANOUT_ID, resource_id, r)?;
        self.transfer_to_host_2d(resource_id, r, 0)?;
        self.flush(resource_id, r)?;

        *self.present_resource.lock() = Some(resource_id);
        Ok(())
    }

    /// Creates a cursor resource and selects it at `x`,`y` on scanout 0.
    #[expect(clippy::too_many_arguments)]
    pub fn update_cursor(
        &self,
        addr: u64,
        size: u32,
        width: u32,
        height: u32,
        hot_x: u32,
        hot_y: u32,
        x: i32,
        y: i32,
    ) -> Result<u32, VirtioDeviceError> {
        let _operation = self.cursor_operation.lock();
        let resource_id = self.next_resource_id.fetch_add(1, Ordering::Relaxed);
        let mut created = false;
        let result = (|| {
            self.resource_create_2d_with_format(
                resource_id,
                VIRTIO_GPU_FORMAT_B8G8R8A8_UNORM,
                width,
                height,
            )?;
            created = true;
            self.attach_backing(resource_id, addr, size)?;
            self.transfer_to_host_2d(
                resource_id,
                VirtioGpuRect {
                    x: 0,
                    y: 0,
                    width,
                    height,
                },
                0,
            )?;
            self.submit_cursor(
                VIRTIO_GPU_CMD_UPDATE_CURSOR,
                resource_id,
                hot_x,
                hot_y,
                x,
                y,
            )
        })();
        if let Err(error) = result {
            if created {
                let _ = self.resource_unref(resource_id);
            }
            return Err(error);
        }

        let previous = self.cursor_resource.swap(resource_id, Ordering::AcqRel);
        if previous != 0 {
            let _ = self.resource_unref(previous);
        }
        Ok(resource_id)
    }

    /// Moves the active hardware cursor without replacing its image.
    pub fn move_cursor(&self, x: i32, y: i32) -> Result<(), VirtioDeviceError> {
        let _operation = self.cursor_operation.lock();
        self.submit_cursor(VIRTIO_GPU_CMD_MOVE_CURSOR, 0, 0, 0, x, y)
    }

    /// Hides the hardware cursor and releases its active resource.
    pub fn hide_cursor(&self, x: i32, y: i32) -> Result<(), VirtioDeviceError> {
        let _operation = self.cursor_operation.lock();
        self.submit_cursor(VIRTIO_GPU_CMD_UPDATE_CURSOR, 0, 0, 0, x, y)?;
        let previous = self.cursor_resource.swap(0, Ordering::AcqRel);
        if previous != 0 {
            let _ = self.resource_unref(previous);
        }
        Ok(())
    }

    /// Hides the cursor only if `resource_id` is still active.
    ///
    /// A closing DRM file uses this to avoid hiding a newer cursor installed
    /// by another file.
    pub fn clear_cursor(
        &self,
        resource_id: u32,
        x: i32,
        y: i32,
    ) -> Result<bool, VirtioDeviceError> {
        let _operation = self.cursor_operation.lock();
        if self.cursor_resource.load(Ordering::Acquire) != resource_id {
            return Ok(false);
        }
        self.submit_cursor(VIRTIO_GPU_CMD_UPDATE_CURSOR, 0, 0, 0, x, y)?;
        self.cursor_resource.store(0, Ordering::Release);
        let _ = self.resource_unref(resource_id);
        Ok(true)
    }

    /// Renders the test pattern and presents it on scanout 0.
    ///
    /// This is the full 2D pipeline: create the resource, attach backing
    /// memory, set it as the scanout, copy a gradient into the backing store,
    /// transfer it to the host, and flush the scanout region. Failures are
    /// logged rather than propagated so a misbehaving GPU cannot wedge boot.
    pub fn render_test_pattern(&self) {
        if let Err(e) = self.do_render_test_pattern() {
            ostd::error!("virtio-gpu render_test_pattern failed: {:?}", e);
        }
    }

    fn do_render_test_pattern(&self) -> Result<(), VirtioDeviceError> {
        self.resource_create_2d(RESOURCE_ID, self.scanout_width, self.scanout_height)?;
        ostd::info!("virtio-gpu: RESOURCE_CREATE_2D ok");

        let backing_len = self.scanout_width as usize * self.scanout_height as usize * BPP;
        self.attach_backing(
            RESOURCE_ID,
            self.framebuffer.daddr() as u64,
            backing_len as u32,
        )?;
        ostd::info!("virtio-gpu: ATTACH_BACKING ok");

        let r = VirtioGpuRect {
            x: 0,
            y: 0,
            width: self.scanout_width,
            height: self.scanout_height,
        };
        self.set_scanout(SCANOUT_ID, RESOURCE_ID, r)?;
        ostd::info!("virtio-gpu: SET_SCANOUT ok");

        self.fill_framebuffer();
        self.framebuffer
            .sync_to_device(0..backing_len)
            .map_err(VirtioDeviceError::ResourceAlloc)?;

        self.transfer_to_host_2d(RESOURCE_ID, r, 0)?;
        ostd::info!("virtio-gpu: TRANSFER_TO_HOST_2D ok");
        self.flush(RESOURCE_ID, r)?;
        ostd::info!("virtio-gpu: FLUSH ok");

        ostd::info!(
            "virtio-gpu: presented {}x{} test pattern on scanout {}",
            self.scanout_width,
            self.scanout_height,
            SCANOUT_ID
        );
        Ok(())
    }

    fn resource_create_2d(
        &self,
        resource_id: u32,
        width: u32,
        height: u32,
    ) -> Result<(), VirtioDeviceError> {
        self.resource_create_2d_with_format(
            resource_id,
            VIRTIO_GPU_FORMAT_B8G8R8X8_UNORM,
            width,
            height,
        )
    }

    fn resource_create_2d_with_format(
        &self,
        resource_id: u32,
        format: u32,
        width: u32,
        height: u32,
    ) -> Result<(), VirtioDeviceError> {
        let req = VirtioGpuResourceCreate2d {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_RESOURCE_CREATE_2D),
            resource_id,
            format,
            width,
            height,
        };
        let mut queue = self.control_queue.lock();
        let code = control_cmd(
            &mut queue,
            &self.control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    fn submit_cursor(
        &self,
        command: u32,
        resource_id: u32,
        hot_x: u32,
        hot_y: u32,
        x: i32,
        y: i32,
    ) -> Result<(), VirtioDeviceError> {
        let request = VirtioGpuUpdateCursor {
            hdr: ctrl_hdr(command),
            pos: VirtioGpuCursorPos {
                scanout_id: SCANOUT_ID,
                x: x as u32,
                y: y as u32,
                padding: 0,
            },
            resource_id,
            hot_x,
            hot_y,
            padding: 0,
        };
        let mut queue = self.cursor_queue.lock();
        cursor_cmd(&mut queue, &self.cursor_buf, &request)
    }

    fn attach_backing(
        &self,
        resource_id: u32,
        addr: u64,
        length: u32,
    ) -> Result<(), VirtioDeviceError> {
        let attach_len = size_of::<VirtioGpuResourceAttachBacking>();
        let entry_len = size_of::<VirtioGpuMemEntry>();
        let req_len = attach_len + entry_len;

        let attach = VirtioGpuResourceAttachBacking {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING),
            resource_id,
            nr_entries: 1,
        };
        let entry = VirtioGpuMemEntry {
            addr,
            length,
            padding: 0,
        };

        let req_slice = Slice::new(
            self.control_buf.clone(),
            CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len,
        );
        req_slice.write_val(0, &attach).unwrap();
        req_slice.write_val(attach_len, &entry).unwrap();

        let mut queue = self.control_queue.lock();
        let code = submit_control(
            &mut queue,
            &self.control_buf,
            req_len,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    fn set_scanout(
        &self,
        scanout_id: u32,
        resource_id: u32,
        r: VirtioGpuRect,
    ) -> Result<(), VirtioDeviceError> {
        let req = VirtioGpuSetScanout {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_SET_SCANOUT),
            r,
            scanout_id,
            resource_id,
        };
        let mut queue = self.control_queue.lock();
        let code = control_cmd(
            &mut queue,
            &self.control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    fn transfer_to_host_2d(
        &self,
        resource_id: u32,
        r: VirtioGpuRect,
        offset: u64,
    ) -> Result<(), VirtioDeviceError> {
        let req = VirtioGpuTransferToHost2d {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_TRANSFER_TO_HOST_2D),
            r,
            offset,
            resource_id,
            padding: 0,
        };
        let mut queue = self.control_queue.lock();
        let code = control_cmd(
            &mut queue,
            &self.control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    fn flush(&self, resource_id: u32, r: VirtioGpuRect) -> Result<(), VirtioDeviceError> {
        let req = VirtioGpuResourceFlush {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_RESOURCE_FLUSH),
            r,
            resource_id,
            padding: 0,
        };
        let mut queue = self.control_queue.lock();
        let code = control_cmd(
            &mut queue,
            &self.control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    fn resource_unref(&self, resource_id: u32) -> Result<(), VirtioDeviceError> {
        let req = VirtioGpuResourceUnref {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_RESOURCE_UNREF),
            resource_id,
            padding: 0,
        };
        let mut queue = self.control_queue.lock();
        let code = control_cmd(
            &mut queue,
            &self.control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    /// Fills the backing store with a horizontal red-to-blue gradient.
    fn fill_framebuffer(&self) {
        let width = self.scanout_width as usize;
        let height = self.scanout_height as usize;
        let mut pixels = Vec::with_capacity(width * height * BPP);
        for _ in 0..height {
            for x in 0..width {
                let t = x * 255 / width;
                let r = 255 - t;
                let b = t;
                // B8G8R8X8: byte order is [B, G, R, X].
                pixels.extend_from_slice(&[b as u8, 0, r as u8, 0]);
            }
        }

        let fb_slice = Slice::new(self.framebuffer.clone(), 0..pixels.len());
        fb_slice.write_bytes(0, &pixels).unwrap();
    }
}

/// Builds a control header with the given type and a zeroed fence.
fn ctrl_hdr(type_: u32) -> VirtioGpuCtrlHdr {
    VirtioGpuCtrlHdr {
        type_,
        flags: 0,
        fence_id: 0,
        ctx_id: 0,
        padding: 0,
    }
}

fn check_ok(code: u32) -> Result<(), VirtioDeviceError> {
    match code {
        VIRTIO_GPU_RESP_OK_NODATA | VIRTIO_GPU_RESP_OK_DISPLAY_INFO => Ok(()),
        _ => {
            ostd::warn!("virtio-gpu control request failed: response = {:#x}", code);
            Err(VirtioDeviceError::UnsupportedConfig)
        }
    }
}

/// Submits a control request of `req_len` bytes (already written into the
/// buffer by the caller) and waits for a `resp_len`-byte response, returning
/// the response type code.
fn submit_control(
    queue: &mut VirtQueue,
    buf: &Arc<DmaStream>,
    req_len: usize,
    resp_len: usize,
) -> Result<u32, VirtioDeviceError> {
    let req_slice = Slice::new(buf.clone(), CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len);
    req_slice.sync_to_device().unwrap();

    let resp_slice = Slice::new(buf.clone(), CTRL_RESP_OFFSET..CTRL_RESP_OFFSET + resp_len);
    queue
        .add_dma_bufs(&[&req_slice], &[&resp_slice])
        .expect("add control queue buffers");
    if queue.should_notify() {
        queue.notify();
    }

    loop {
        if queue.pop_used_with_min_bytes(resp_len).is_ok() {
            break;
        }
        spin_loop();
    }

    resp_slice.sync_from_device().unwrap();
    Ok(resp_slice.read_val::<u32>(0).unwrap())
}

/// Sends a fixed-size control request and waits for its response.
fn control_cmd<T: ostd_pod::Pod>(
    queue: &mut VirtQueue,
    buf: &Arc<DmaStream>,
    req: &T,
    resp_len: usize,
) -> Result<u32, VirtioDeviceError> {
    let req_len = size_of::<T>();
    let req_slice = Slice::new(buf.clone(), CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len);
    req_slice.write_val(0, req).unwrap();
    submit_control(queue, buf, req_len, resp_len)
}

/// Sends one request-only command and accepts the cursor queue's zero-byte
/// used entry as completion.
fn cursor_cmd<T: ostd_pod::Pod>(
    queue: &mut VirtQueue,
    buf: &Arc<DmaStream>,
    request: &T,
) -> Result<(), VirtioDeviceError> {
    let request_len = size_of::<T>();
    let request_slice = Slice::new(buf.clone(), 0..request_len);
    request_slice.write_val(0, request).unwrap();
    request_slice.sync_to_device().unwrap();
    queue
        .add_input_bufs(&[&request_slice])
        .expect("add cursor queue request");
    if queue.should_notify() {
        queue.notify();
    }
    loop {
        if queue
            .pop_used_with_min_bytes(CURSOR_COMPLETION_BYTES)
            .is_ok()
        {
            return Ok(());
        }
        spin_loop();
    }
}

/// Chooses the largest power-of-two queue size up to the driver's cap.
fn cursor_queue_size(device_max: u16) -> Option<u16> {
    let capped = device_max.min(QUEUE_SIZE);
    if capped == 0 {
        return None;
    }
    let mut size = 1;
    while size <= capped / 2 {
        size *= 2;
    }
    Some(size)
}

/// Queries the display info and returns the first enabled scanout's dimensions.
fn query_display_info(
    queue: &mut VirtQueue,
    buf: &Arc<DmaStream>,
    num_scanouts: u32,
) -> Result<(u32, u32), VirtioDeviceError> {
    if num_scanouts == 0 {
        return Err(VirtioDeviceError::UnsupportedConfig);
    }

    let resp_len = size_of::<VirtioGpuCtrlHdr>() + MAX_SCANOUTS * size_of::<VirtioGpuDisplayOne>();
    let req = ctrl_hdr(VIRTIO_GPU_CMD_GET_DISPLAY_INFO);
    let code = control_cmd(queue, buf, &req, resp_len)?;
    if code != VIRTIO_GPU_RESP_OK_DISPLAY_INFO {
        ostd::warn!("virtio-gpu GET_DISPLAY_INFO failed: response = {:#x}", code);
        return Err(VirtioDeviceError::UnsupportedConfig);
    }

    let resp_slice = Slice::new(buf.clone(), CTRL_RESP_OFFSET..CTRL_RESP_OFFSET + resp_len);
    resp_slice.sync_from_device().unwrap();
    let one: VirtioGpuDisplayOne = resp_slice.read_val(size_of::<VirtioGpuCtrlHdr>()).unwrap();
    Ok((one.r.width, one.r.height))
}

/// Allocates a DMA backing store for a `width`x`height` B8G8R8X8 resource.
fn alloc_framebuffer(width: u32, height: u32) -> Result<Arc<DmaStream>, ostd::Error> {
    let nbytes = width as usize * height as usize * BPP;
    let nframes = nbytes.div_ceil(PAGE_SIZE);
    Ok(Arc::new(DmaStream::alloc(nframes, false)?))
}

static GPU_DEVICE_ID: AtomicUsize = AtomicUsize::new(0);

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn cursor_queue_size_respects_the_device_limit() {
        assert_eq!(cursor_queue_size(0), None);
        assert_eq!(cursor_queue_size(1), Some(1));
        assert_eq!(cursor_queue_size(3), Some(2));
        assert_eq!(cursor_queue_size(16), Some(16));
        assert_eq!(cursor_queue_size(63), Some(32));
        assert_eq!(cursor_queue_size(64), Some(64));
        assert_eq!(cursor_queue_size(256), Some(64));
    }

    #[ktest]
    fn cursor_completion_has_no_response_body() {
        assert_eq!(CURSOR_COMPLETION_BYTES, 0);
    }
}
