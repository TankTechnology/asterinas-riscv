// SPDX-License-Identifier: MPL-2.0

//! Implements virtio-gpu device instances (device ID 16).
//!
//! This MVP targets the 2D control-queue path: create a single 2D resource,
//! attach guest memory as its backing store, present it as scanout 0, then push
//! a test pattern to the host via `TRANSFER_TO_HOST_2D` + `RESOURCE_FLUSH`.
//! The cursor queue drives the hardware cursor (`UPDATE_CURSOR`/`MOVE_CURSOR`),
//! while EDID and the virgl 3D path are deliberately left dormant, mirroring the
//! scope of the initial DRM bring-up.

use alloc::{format, sync::Arc, vec::Vec};
use core::{
    hint::spin_loop,
    sync::atomic::{AtomicU32, AtomicUsize, Ordering},
};

use aster_util::mem_obj_slice::Slice;
use ostd::{
    mm::{HasDaddr, PAGE_SIZE, VmIo, dma::DmaStream},
    sync::SpinLock,
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
    /// The cursor queue carries the hardware-cursor commands
    /// (`UPDATE_CURSOR`/`MOVE_CURSOR`).
    cursor_queue: SpinLock<VirtQueue>,
    control_buf: Arc<DmaStream>,
    /// Cursor-queue buffer, mirroring the control buffer's request/response
    /// layout (the cursor request/response are both smaller than one page).
    cursor_buf: Arc<DmaStream>,
    /// Backing memory of the scanout resource, in B8G8R8X8.
    framebuffer: Arc<DmaStream>,
    scanout_width: u32,
    scanout_height: u32,
    /// Resource id of the most recent framebuffer presented via
    /// [`present_framebuffer`], tracked so it can be unref'd before the next one.
    present_resource: SpinLock<Option<u32>>,
    /// Resource id of the most recent cursor presented via [`present_cursor`].
    cursor_resource: SpinLock<Option<u32>>,
    /// Next resource id handed out by [`present_framebuffer`] / [`present_cursor`].
    pub next_resource_id: AtomicU32,
}

impl GpuDevice {
    pub(crate) fn negotiate_features(features: u64) -> u64 {
        // Enable virgl 3D if the device offers it; clear everything else
        // (EDID, resource UUID, blob, context init) for now.
        features & super::VIRTIO_GPU_F_VIRGL
    }

    pub(crate) fn init(mut device_transport: DeviceTransport) -> Result<(), VirtioDeviceError> {
        let config_manager = VirtioGpuConfig::new_manager(device_transport.as_ref());
        let config = config_manager.read_config();
        ostd::debug!("virtio_gpu_config = {:?}", config);

        // The cursor queue is allowed (and in QEMU, is) much smaller than the
        // control queue (16 vs 256). Clamp each queue to what the device
        // actually offers instead of assuming both are `QUEUE_SIZE`.
        let control_queue_size = QUEUE_SIZE
            .min(device_transport.max_queue_size(VQ_CONTROL).unwrap_or(QUEUE_SIZE));
        let cursor_queue_size = QUEUE_SIZE
            .min(device_transport.max_queue_size(VQ_CURSOR).unwrap_or(QUEUE_SIZE));
        let mut control_queue =
            VirtQueue::new(VQ_CONTROL, control_queue_size, device_transport.as_mut())?;
        let cursor_queue =
            VirtQueue::new(VQ_CURSOR, cursor_queue_size, device_transport.as_mut())?;
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
            cursor_resource: SpinLock::new(None),
            // Resource id 1 is reserved for the boot-time test pattern.
            next_resource_id: AtomicU32::new(2),
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
        self.resource_create_2d(resource_id, VIRTIO_GPU_FORMAT_B8G8R8X8_UNORM, width, height)?;
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

    /// Presents an externally-owned guest buffer as the hardware cursor.
    ///
    /// Creates an ARGB 2D resource of `width`x`height`, attaches `addr`/`size`
    /// of guest memory as its backing store, and shows it at the origin via
    /// `UPDATE_CURSOR`. Unlike the scanout path there is no
    /// `TRANSFER_TO_HOST_2D`: the host reads the cursor pixels straight out of
    /// the attached backing memory, so a guest-side mmap write is visible
    /// immediately. Any previously presented cursor resource is unref'd first.
    pub fn present_cursor(
        &self,
        addr: u64,
        size: u32,
        width: u32,
        height: u32,
        hot_x: u32,
        hot_y: u32,
    ) -> Result<(), VirtioDeviceError> {
        if let Some(prev) = *self.cursor_resource.lock() {
            let _ = self.resource_unref(prev);
        }

        let resource_id = self.next_resource_id.fetch_add(1, Ordering::Relaxed);
        self.resource_create_2d(resource_id, VIRTIO_GPU_FORMAT_B8G8R8A8_UNORM, width, height)?;
        self.attach_backing(resource_id, addr, size)?;
        self.update_cursor(resource_id, 0, 0, hot_x, hot_y)?;

        *self.cursor_resource.lock() = Some(resource_id);
        Ok(())
    }

    /// Repositions the hardware cursor to (`x`, `y`).
    pub fn move_cursor(&self, x: u32, y: u32) -> Result<(), VirtioDeviceError> {
        self.send_cursor(VIRTIO_GPU_CMD_MOVE_CURSOR, 0, x, y, 0, 0)
    }

    /// Hides the hardware cursor (resource id 0 disables the cursor overlay).
    pub fn hide_cursor(&self) -> Result<(), VirtioDeviceError> {
        self.send_cursor(VIRTIO_GPU_CMD_UPDATE_CURSOR, 0, 0, 0, 0, 0)
    }

    /// Shows the cursor resource at the origin with the given hotspot.
    fn update_cursor(
        &self,
        resource_id: u32,
        x: u32,
        y: u32,
        hot_x: u32,
        hot_y: u32,
    ) -> Result<(), VirtioDeviceError> {
        self.send_cursor(
            VIRTIO_GPU_CMD_UPDATE_CURSOR,
            resource_id,
            x,
            y,
            hot_x,
            hot_y,
        )
    }

    /// Sends a cursor-queue command (`UPDATE_CURSOR` or `MOVE_CURSOR`).
    fn send_cursor(
        &self,
        type_: u32,
        resource_id: u32,
        x: u32,
        y: u32,
        hot_x: u32,
        hot_y: u32,
    ) -> Result<(), VirtioDeviceError> {
        let req = VirtioGpuUpdateCursor {
            hdr: ctrl_hdr(type_),
            pos: VirtioGpuCursorPos {
                scanout_id: SCANOUT_ID,
                x,
                y,
                padding: 0,
            },
            resource_id,
            hot_x,
            hot_y,
            padding: 0,
        };
        let mut queue = self.cursor_queue.lock();
        cursor_cmd(&mut queue, &self.cursor_buf, &req)
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
        self.resource_create_2d(
            RESOURCE_ID,
            VIRTIO_GPU_FORMAT_B8G8R8X8_UNORM,
            self.scanout_width,
            self.scanout_height,
        )?;
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

    pub fn resource_create_2d(
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

    pub fn attach_backing(
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
        let (code, _) = submit_control(
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

    /// 3D: create a 3D resource (texture, render target, or buffer).
    #[expect(clippy::too_many_arguments)]
    pub fn resource_create_3d(
        &self,
        resource_id: u32,
        target: u32,
        format: u32,
        bind: u32,
        width: u32,
        height: u32,
        depth: u32,
        array_size: u32,
        last_level: u32,
        nr_samples: u32,
        flags: u32,
    ) -> Result<(), VirtioDeviceError> {
        use super::VirtioGpuResourceCreate3d;
        let req = VirtioGpuResourceCreate3d {
            hdr: ctrl_hdr(super::VIRTIO_GPU_CMD_RESOURCE_CREATE_3D),
            resource_id,
            target,
            format,
            bind,
            width,
            height,
            depth,
            array_size,
            last_level,
            nr_samples,
            flags,
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

    /// 3D: create a virgl rendering context.
    pub fn ctx_create(
        &self,
        capset_id: u32,
        debug_name: &[u8; 64],
    ) -> Result<(), VirtioDeviceError> {
        let mut name = [0u8; 64];
        let copy_len = debug_name.len().min(64);
        name[..copy_len].copy_from_slice(&debug_name[..copy_len]);
        let req = super::VirtioGpuCtxCreate {
            hdr: ctrl_hdr(super::VIRTIO_GPU_CMD_CTX_CREATE),
            nlen: copy_len as u32,
            context_init: capset_id & 0xff,
            debug_name: name,
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

    /// 3D: destroy a virgl rendering context.
    pub fn ctx_destroy(&self) -> Result<(), VirtioDeviceError> {
        let req = super::VirtioGpuCtxDestroy {
            hdr: ctrl_hdr(super::VIRTIO_GPU_CMD_CTX_DESTROY),
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

    /// 3D: attach a resource to the virgl context.
    pub fn ctx_attach_resource(&self, resource_id: u32) -> Result<(), VirtioDeviceError> {
        let req = super::VirtioGpuCtxResource {
            hdr: ctrl_hdr(super::VIRTIO_GPU_CMD_CTX_ATTACH_RESOURCE),
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

    /// 3D: submit a virgl command buffer to the host (unfenced — the response
    /// acknowledges receipt, not completion).
    pub fn submit_3d(&self, size: u32, data: &[u8]) -> Result<(), VirtioDeviceError> {
        self.submit_3d_with_fence(size, data, 0, 0)
    }

    /// 3D: submit a virgl command buffer with `VIRTIO_GPU_FLAG_FENCE` set.
    ///
    /// The device defers the response until the command has completed, so the
    /// synchronous [`submit_control`] wait below returns only after rendering
    /// finishes. This is how the render→scanout path synchronizes.
    pub fn submit_3d_fenced(
        &self,
        size: u32,
        data: &[u8],
        fence_id: u64,
    ) -> Result<(), VirtioDeviceError> {
        self.submit_3d_with_fence(size, data, super::VIRTIO_GPU_FLAG_FENCE, fence_id)
    }

    fn submit_3d_with_fence(
        &self,
        size: u32,
        data: &[u8],
        flags: u32,
        fence_id: u64,
    ) -> Result<(), VirtioDeviceError> {
        use super::VirtioGpuCmdSubmit;
        let mut hdr = ctrl_hdr(super::VIRTIO_GPU_CMD_SUBMIT_3D);
        hdr.flags = flags;
        hdr.fence_id = fence_id;
        let req = VirtioGpuCmdSubmit {
            hdr,
            size,
            padding: 0,
        };
        let req_len = size_of::<VirtioGpuCmdSubmit>();
        let total_len = req_len + size as usize;

        let req_slice = Slice::new(
            self.control_buf.clone(),
            CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + total_len,
        );
        req_slice.write_val(0, &req).unwrap();
        req_slice.write_bytes(req_len, data).unwrap();

        let mut queue = self.control_queue.lock();
        let (code, _) = submit_control(
            &mut queue,
            &self.control_buf,
            total_len,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    /// 3D: query capset info from the device.
    pub fn get_capset_info(
        &self,
        capset_id: u32,
    ) -> Result<super::VirtioGpuRespCapsetInfo, VirtioDeviceError> {
        let req = super::VirtioGpuGetCapsetInfo {
            hdr: ctrl_hdr(super::VIRTIO_GPU_CMD_GET_CAPSET_INFO),
            capset_index: capset_id,
            padding: 0,
        };
        let resp_len = size_of::<super::VirtioGpuRespCapsetInfo>();
        let mut queue = self.control_queue.lock();
        let code = control_cmd(&mut queue, &self.control_buf, &req, resp_len)?;
        if code != super::VIRTIO_GPU_RESP_OK_CAPSET_INFO {
            return Err(VirtioDeviceError::UnsupportedConfig);
        }
        let resp_slice = Slice::new(
            self.control_buf.clone(),
            CTRL_RESP_OFFSET..CTRL_RESP_OFFSET + resp_len,
        );
        resp_slice.sync_from_device().unwrap();
        let resp: super::VirtioGpuRespCapsetInfo = resp_slice.read_val(0).unwrap();
        Ok(resp)
    }

    /// 3D: fetch the capset data blob from the device.
    pub fn get_capset(&self, capset_id: u32, version: u32) -> Result<Vec<u8>, VirtioDeviceError> {
        let req = super::VirtioGpuGetCapset {
            hdr: ctrl_hdr(super::VIRTIO_GPU_CMD_GET_CAPSET),
            capset_id,
            capset_version: version,
        };

        // First, query the capset info to know the size
        let info = self.get_capset_info(capset_id)?;
        let capset_size = info.capset_max_size as usize;
        if capset_size == 0 {
            return Ok(Vec::new());
        }

        // The device returns the actual capset size, which may be smaller
        // than the advertised maximum.
        let resp_len = size_of::<VirtioGpuCtrlHdr>() + capset_size;
        let req_len = size_of::<super::VirtioGpuGetCapset>();
        let req_slice = Slice::new(
            self.control_buf.clone(),
            CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len,
        );
        req_slice.write_val(0, &req).unwrap();

        let mut queue = self.control_queue.lock();
        let (code, used_len) = submit_control(&mut queue, &self.control_buf, req_len, resp_len)?;
        if code != super::VIRTIO_GPU_RESP_OK_CAPSET {
            return Err(VirtioDeviceError::UnsupportedConfig);
        }

        let data_len = used_len.saturating_sub(size_of::<VirtioGpuCtrlHdr>());
        let resp_slice = Slice::new(
            self.control_buf.clone(),
            CTRL_RESP_OFFSET..CTRL_RESP_OFFSET + resp_len,
        );
        resp_slice.sync_from_device().unwrap();
        let mut data = alloc::vec![0u8; data_len];
        resp_slice
            .read_bytes(size_of::<VirtioGpuCtrlHdr>(), &mut data)
            .unwrap();
        Ok(data)
    }

    /// 3D: transfer data from guest to host for a 3D resource.
    #[expect(clippy::too_many_arguments)]
    pub fn transfer_to_host_3d(
        &self,
        resource_id: u32,
        x: u32,
        y: u32,
        z: u32,
        w: u32,
        h: u32,
        d: u32,
        offset: u64,
        level: u32,
        stride: u32,
        layer_stride: u32,
    ) -> Result<(), VirtioDeviceError> {
        let req = super::VirtioGpuTransferHost3d {
            hdr: ctrl_hdr(super::VIRTIO_GPU_CMD_TRANSFER_TO_HOST_3D),
            box_: super::VirtioGpuBox { x, y, z, w, h, d },
            offset,
            resource_id,
            level,
            stride,
            layer_stride,
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

    /// 3D: transfer data from host to guest for a 3D resource.
    #[expect(clippy::too_many_arguments)]
    pub fn transfer_from_host_3d(
        &self,
        resource_id: u32,
        x: u32,
        y: u32,
        z: u32,
        w: u32,
        h: u32,
        d: u32,
        offset: u64,
        level: u32,
        stride: u32,
        layer_stride: u32,
    ) -> Result<(), VirtioDeviceError> {
        let req = super::VirtioGpuTransferHost3d {
            hdr: ctrl_hdr(super::VIRTIO_GPU_CMD_TRANSFER_FROM_HOST_3D),
            box_: super::VirtioGpuBox { x, y, z, w, h, d },
            offset,
            resource_id,
            level,
            stride,
            layer_stride,
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
/// buffer by the caller) and waits for a response of at most `resp_len`
/// bytes, returning the response type code and the actual used length.
///
/// The device may legitimately write fewer bytes than the buffer size (for
/// example `GET_CAPSET` returns the actual capset size, not the maximum),
/// so the used length is validated against the header size, not `resp_len`.
fn submit_control(
    queue: &mut VirtQueue,
    buf: &Arc<DmaStream>,
    req_len: usize,
    resp_len: usize,
) -> Result<(u32, usize), VirtioDeviceError> {
    let req_slice = Slice::new(buf.clone(), CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len);
    req_slice.sync_to_device().unwrap();

    let resp_slice = Slice::new(buf.clone(), CTRL_RESP_OFFSET..CTRL_RESP_OFFSET + resp_len);
    queue
        .add_dma_bufs(&[&req_slice], &[&resp_slice])
        .expect("add control queue buffers");
    if queue.should_notify() {
        queue.notify();
    }

    let used_len = loop {
        if let Ok((_, len)) = queue.pop_used_with_min_bytes(size_of::<VirtioGpuCtrlHdr>()) {
            break (len as usize).min(resp_len);
        }
        spin_loop();
    };

    resp_slice.sync_from_device().unwrap();
    Ok((resp_slice.read_val::<u32>(0).unwrap(), used_len))
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
    let (code, _used_len) = submit_control(queue, buf, req_len, resp_len)?;
    Ok(code)
}

/// Submits a cursor-queue request and waits for the device to return the buffer.
///
/// Unlike [`submit_control`], this does not require a response body: QEMU
/// recycles cursor-queue buffers with a zero-length used entry (Linux's
/// `virtio_gpu_dequeue_cursor_func` likewise ignores the cursor response
/// length), so the buffer coming back is the only completion signal.
fn submit_cursor(
    queue: &mut VirtQueue,
    buf: &Arc<DmaStream>,
    req_len: usize,
) -> Result<(), VirtioDeviceError> {
    let req_slice = Slice::new(buf.clone(), CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len);
    req_slice.sync_to_device().unwrap();

    let resp_len = size_of::<VirtioGpuCtrlHdr>();
    let resp_slice = Slice::new(buf.clone(), CTRL_RESP_OFFSET..CTRL_RESP_OFFSET + resp_len);
    queue
        .add_dma_bufs(&[&req_slice], &[&resp_slice])
        .expect("add cursor queue buffers");
    if queue.should_notify() {
        queue.notify();
    }

    loop {
        if queue.pop_used().is_ok() {
            break;
        }
        spin_loop();
    }
    Ok(())
}

/// Sends a fixed-size cursor request and waits for its completion.
fn cursor_cmd<T: ostd_pod::Pod>(
    queue: &mut VirtQueue,
    buf: &Arc<DmaStream>,
    req: &T,
) -> Result<(), VirtioDeviceError> {
    let req_len = size_of::<T>();
    let req_slice = Slice::new(buf.clone(), CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len);
    req_slice.write_val(0, req).unwrap();
    submit_cursor(queue, buf, req_len)
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
