// SPDX-License-Identifier: MPL-2.0

//! Implements virtio-gpu device instances (device ID 16).
//!
//! The driver covers the 2D control queue, hardware cursor,
//! and virgl 3D command paths.
//! EDID and newer resource/context features remain disabled.

use alloc::{
    boxed::Box,
    collections::{BTreeMap, BTreeSet},
    format,
    sync::Arc,
    vec::Vec,
};
use core::{
    hint::spin_loop,
    sync::atomic::{AtomicU32, AtomicUsize, Ordering},
};

use aster_util::mem_obj_slice::Slice;
use ostd::{
    arch::trap::TrapFrame,
    mm::{HasDaddr, PAGE_SIZE, VmIo, dma::DmaStream},
    sync::{Mutex, SpinLock},
};

use super::{
    GpuBackingOwner, GpuCommandCompletion, MAX_SCANOUTS, VIRTIO_GPU_CMD_GET_DISPLAY_INFO,
    VIRTIO_GPU_CMD_MOVE_CURSOR, VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING,
    VIRTIO_GPU_CMD_RESOURCE_CREATE_2D, VIRTIO_GPU_CMD_RESOURCE_FLUSH,
    VIRTIO_GPU_CMD_RESOURCE_UNREF, VIRTIO_GPU_CMD_SET_SCANOUT, VIRTIO_GPU_CMD_TRANSFER_TO_HOST_2D,
    VIRTIO_GPU_CMD_UPDATE_CURSOR, VIRTIO_GPU_FORMAT_B8G8R8A8_UNORM,
    VIRTIO_GPU_FORMAT_B8G8R8X8_UNORM, VIRTIO_GPU_RESP_OK_DISPLAY_INFO, VIRTIO_GPU_RESP_OK_NODATA,
    VQ_CONTROL, VQ_CURSOR, VirtioGpuCtrlHdr, VirtioGpuCursorPos, VirtioGpuDisplayOne,
    VirtioGpuMemEntry, VirtioGpuRect, VirtioGpuResourceAttachBacking, VirtioGpuResourceCreate2d,
    VirtioGpuResourceFlush, VirtioGpuResourceUnref, VirtioGpuSetScanout, VirtioGpuTransferToHost2d,
    VirtioGpuUpdateCursor,
    config::VirtioGpuConfig,
    control_queue::{ControlQueue, ControlTicket},
};
use crate::{
    device::{VirtioDeviceError, gpu::register_device},
    queue::{PopUsedError, VirtQueue},
    transport::DeviceTransport,
};

/// Maximum number of descriptors selected for one virtqueue.
const MAX_QUEUE_SIZE: u16 = 64;

/// Cursor requests have no device-written response body.
const CURSOR_COMPLETION_BYTES: usize = 0;

/// An asynchronous fenced GPU command whose response can be collected later.
#[must_use = "an asynchronous GPU command must be observed or retained"]
pub struct GpuCommandTicket {
    control: ControlTicket,
    response: Slice<Arc<DmaStream>>,
    response_len: usize,
}

impl GpuCommandTicket {
    /// Polls the control queue once without consuming this command's result.
    pub fn poll_completion(&self) {
        self.control.poll_completion();
    }

    /// Waits for device completion and validates the command response.
    pub fn wait(self) -> Result<(), VirtioDeviceError> {
        let (code, _) = self.wait_for_response()?;
        check_ok(code)
    }

    fn wait_for_response(self) -> Result<(u32, usize), VirtioDeviceError> {
        let (_, used_len) = self
            .control
            .wait_for_used()
            .map_err(|_| VirtioDeviceError::UnsupportedConfig)?;
        let used_len = (used_len as usize).min(self.response_len);
        if used_len < size_of::<u32>() {
            return Err(VirtioDeviceError::UnsupportedConfig);
        }
        self.response.sync_from_device().unwrap();
        Ok((self.response.read_val::<u32>(0).unwrap(), used_len))
    }
}

/// Upper bound for a device-advertised virgl capability blob.
const MAX_CAPSET_SIZE: usize = 1024 * 1024;

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

#[derive(Clone, Copy)]
struct PresentedResource {
    resource_id: u32,
    backing_addr: u64,
    backing_size: u32,
    width: u32,
    height: u32,
}

impl PresentedResource {
    fn matches(self, addr: u64, size: u32, width: u32, height: u32) -> bool {
        (
            self.backing_addr,
            self.backing_size,
            self.width,
            self.height,
        ) == (addr, size, width, height)
    }
}

/// A virtio-gpu device.
pub struct GpuDevice {
    /// Keeps the virtio transport alive for the device's lifetime. The control
    /// queue borrows it during `init` and holds its own handle afterwards, so
    /// this field is never read directly.
    _transport: SpinLock<DeviceTransport>,
    control_queue: Arc<ControlQueue>,
    /// The cursor queue carries the hardware-cursor commands
    /// (`UPDATE_CURSOR`/`MOVE_CURSOR`).
    cursor_queue: Mutex<VirtQueue>,
    /// Shared page for small control commands. Holding this sleeping mutex
    /// only serializes users of this page; independently allocated 3D command
    /// buffers can be submitted concurrently.
    control_buf: Mutex<Arc<DmaStream>>,
    /// DMA buffer containing the request submitted to the cursor virtqueue.
    cursor_buf: Arc<DmaStream>,
    /// Backing memory of the scanout resource, in B8G8R8X8.
    framebuffer: Arc<DmaStream>,
    /// Owners of memory that remains attached to live host resources.
    backing_owners: SpinLock<BTreeMap<u32, Arc<dyn GpuBackingOwner>>>,
    /// Resource IDs whose host cleanup failed and must be retried.
    pending_resource_cleanup: SpinLock<BTreeSet<u32>>,
    scanout_width: u32,
    scanout_height: u32,
    framebuffer_len: u32,
    num_capsets: u32,
    virgl_supported: bool,
    /// Most recent 2D scanout: resource id, backing address/size, and geometry.
    ///
    /// Repeated dirty updates reuse this resource. Recreating it for every
    /// update briefly detaches the active QEMU scanout and can leave the GTK
    /// display reporting that its output is inactive.
    present_resource: Mutex<Option<PresentedResource>>,
    /// Next resource id handed out by framebuffer, cursor, and virgl clients.
    next_resource_id: AtomicU32,
    /// Serializes multi-command cursor resource transactions without spinning.
    cursor_operation: Mutex<()>,
    /// Host-visible cursor resource, or zero when the cursor is hidden.
    cursor_resource: AtomicU32,
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
        let virgl_supported = Self::negotiate_features(device_transport.read_device_features())
            & super::VIRTIO_GPU_F_VIRGL
            != 0;
        ostd::debug!("virtio_gpu_config = {:?}", config);

        // The cursor queue is allowed (and in QEMU, is) much smaller than the
        // control queue (16 vs 256). Clamp each queue to what the device
        // actually offers instead of assuming both are `QUEUE_SIZE`.
        let control_queue_size = control_queue_size(
            device_transport
                .max_queue_size(VQ_CONTROL)
                .unwrap_or(MAX_QUEUE_SIZE),
        )
        .ok_or(VirtioDeviceError::InvalidQueueArgs)?;
        let cursor_queue_size = cursor_queue_size(
            device_transport
                .max_queue_size(VQ_CURSOR)
                .unwrap_or(MAX_QUEUE_SIZE),
        )
        .ok_or(VirtioDeviceError::InvalidQueueArgs)?;
        let control_queue = ControlQueue::new(VirtQueue::new(
            VQ_CONTROL,
            control_queue_size,
            device_transport.as_mut(),
        )?);
        let cursor_queue = VirtQueue::new(VQ_CURSOR, cursor_queue_size, device_transport.as_mut())?;
        let control_buf =
            Arc::new(DmaStream::alloc(1, false).map_err(VirtioDeviceError::ResourceAlloc)?);
        let cursor_buf =
            Arc::new(DmaStream::alloc(1, false).map_err(VirtioDeviceError::ResourceAlloc)?);

        let irq_control_queue = control_queue.clone();
        device_transport.register_queue_callback(
            VQ_CONTROL,
            Box::new(move |_: &TrapFrame| {
                irq_control_queue.handle_irq();
            }),
            false,
        )?;

        // Mark the device ready before issuing the first control request.
        // Boot-time requests still poll because task scheduling is not ready.
        device_transport.finish_init();

        let (scanout_width, scanout_height) =
            query_display_info(&control_queue, &control_buf, config.num_scanouts)?;
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

        let (framebuffer, framebuffer_len) = alloc_framebuffer(scanout_width, scanout_height)?;

        let device = Arc::new(Self {
            _transport: SpinLock::new(device_transport),
            control_queue,
            cursor_queue: Mutex::new(cursor_queue),
            control_buf: Mutex::new(control_buf),
            cursor_buf,
            framebuffer,
            backing_owners: SpinLock::new(BTreeMap::new()),
            pending_resource_cleanup: SpinLock::new(BTreeSet::new()),
            scanout_width,
            scanout_height,
            framebuffer_len,
            num_capsets: config.num_capsets,
            virgl_supported,
            present_resource: Mutex::new(None),
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
        device.control_queue.enable_irq_wait();
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

    /// Whether the host offered and the driver negotiated virgl 3D support.
    pub fn supports_virgl(&self) -> bool {
        self.virgl_supported
    }

    /// Allocates a resource id unique to this device instance.
    pub fn allocate_resource_id(&self) -> Result<u32, VirtioDeviceError> {
        self.next_resource_id
            .try_update(Ordering::Relaxed, Ordering::Relaxed, next_resource_id)
            .map_err(|_| VirtioDeviceError::InvalidQueueArgs)
    }

    /// Presents an externally-owned guest buffer as scanout 0.
    ///
    /// Runs the full 2D pipeline for a caller-provided framebuffer: create a
    /// resource, attach `addr`/`size` of guest memory as its backing store,
    /// transfer and flush the pixels, and finally set it as scanout 0. Any
    /// previously presented resource is unref'd after the replacement becomes
    /// active so repeated presents neither leak resources nor detach scanout 0.
    pub fn present_framebuffer(
        &self,
        addr: u64,
        size: u32,
        owner: Arc<dyn GpuBackingOwner>,
        width: u32,
        height: u32,
    ) -> Result<(), VirtioDeviceError> {
        self.drain_pending_resource_cleanup();
        let r = VirtioGpuRect {
            x: 0,
            y: 0,
            width,
            height,
        };

        let mut presented = self.present_resource.lock();
        let previous = *presented;
        if let Some(previous) = previous
            && previous.matches(addr, size, width, height)
        {
            self.transfer_to_host_2d(previous.resource_id, r, 0)?;
            self.flush(previous.resource_id, r)?;
            return Ok(());
        }

        let resource_id = self.allocate_resource_id()?;
        self.resource_create_2d(resource_id, VIRTIO_GPU_FORMAT_B8G8R8X8_UNORM, width, height)?;
        let prepare_result = (|| {
            self.attach_backing(resource_id, addr, size, owner)?;
            self.transfer_to_host_2d(resource_id, r, 0)?;
            self.flush(resource_id, r)?;
            self.set_scanout(SCANOUT_ID, resource_id, r)
        })();
        if let Err(error) = prepare_result {
            self.defer_resource_unref(resource_id);
            return Err(error);
        }

        *presented = Some(PresentedResource {
            resource_id,
            backing_addr: addr,
            backing_size: size,
            width,
            height,
        });
        if let Some(previous) = previous {
            // Switch scanout first, then release the old resource so scanout 0
            // is never transiently detached.
            self.defer_resource_unref(previous.resource_id);
        }
        Ok(())
    }

    /// Disables scanout 0 and releases the resource used for direct display.
    pub fn disable_scanout(&self) -> Result<(), VirtioDeviceError> {
        self.drain_pending_resource_cleanup();
        let mut presented = self.present_resource.lock();
        self.set_scanout(
            SCANOUT_ID,
            0,
            VirtioGpuRect {
                x: 0,
                y: 0,
                width: 0,
                height: 0,
            },
        )?;
        if let Some(previous) = presented.take() {
            self.defer_resource_unref(previous.resource_id);
        }
        Ok(())
    }

    /// Creates a cursor resource and selects it at `x`,`y` on scanout 0.
    #[expect(clippy::too_many_arguments)]
    pub fn update_cursor(
        &self,
        addr: u64,
        size: u32,
        owner: Arc<dyn GpuBackingOwner>,
        width: u32,
        height: u32,
        hot_x: u32,
        hot_y: u32,
        x: i32,
        y: i32,
    ) -> Result<u32, VirtioDeviceError> {
        let _operation = self.cursor_operation.lock();
        self.drain_pending_resource_cleanup();
        let resource_id = self.allocate_resource_id()?;
        let mut created = false;
        let result = (|| {
            self.resource_create_2d(resource_id, VIRTIO_GPU_FORMAT_B8G8R8A8_UNORM, width, height)?;
            created = true;
            self.attach_backing(resource_id, addr, size, owner)?;
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
                self.defer_resource_unref(resource_id);
            }
            return Err(error);
        }

        let previous = self.cursor_resource.swap(resource_id, Ordering::AcqRel);
        if previous != 0 {
            self.defer_resource_unref(previous);
        }
        Ok(resource_id)
    }

    /// Moves the active hardware cursor without replacing its image.
    pub fn move_cursor(&self, x: i32, y: i32) -> Result<(), VirtioDeviceError> {
        let _operation = self.cursor_operation.lock();
        self.drain_pending_resource_cleanup();
        self.submit_cursor(VIRTIO_GPU_CMD_MOVE_CURSOR, 0, 0, 0, x, y)
    }

    /// Hides the hardware cursor and releases its active resource.
    pub fn hide_cursor(&self, x: i32, y: i32) -> Result<(), VirtioDeviceError> {
        let _operation = self.cursor_operation.lock();
        self.drain_pending_resource_cleanup();
        self.submit_cursor(VIRTIO_GPU_CMD_UPDATE_CURSOR, 0, 0, 0, x, y)?;
        let previous = self.cursor_resource.swap(0, Ordering::AcqRel);
        if previous != 0 {
            self.defer_resource_unref(previous);
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
        self.drain_pending_resource_cleanup();
        if self.cursor_resource.load(Ordering::Acquire) != resource_id {
            return Ok(false);
        }
        self.submit_cursor(VIRTIO_GPU_CMD_UPDATE_CURSOR, 0, 0, 0, x, y)?;
        self.cursor_resource.store(0, Ordering::Release);
        self.defer_resource_unref(resource_id);
        Ok(true)
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

        self.attach_backing(
            RESOURCE_ID,
            self.framebuffer.daddr() as u64,
            self.framebuffer_len,
            self.framebuffer.clone(),
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
            .sync_to_device(0..self.framebuffer_len as usize)
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
        let control_buf = self.control_buf.lock();
        let code = control_cmd(
            &self.control_queue,
            &control_buf,
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
        owner: Arc<dyn GpuBackingOwner>,
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

        let control_buf = self.control_buf.lock();
        let req_slice = Slice::new(
            control_buf.clone(),
            CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len,
        );
        req_slice.write_val(0, &attach).unwrap();
        req_slice.write_val(attach_len, &entry).unwrap();

        let (code, _) = submit_control(
            &self.control_queue,
            &control_buf,
            req_len,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)?;
        let previous = self.backing_owners.lock().insert(resource_id, owner);
        debug_assert!(previous.is_none());
        Ok(())
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
        let control_buf = self.control_buf.lock();
        let code = control_cmd(
            &self.control_queue,
            &control_buf,
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
        let control_buf = self.control_buf.lock();
        let code = control_cmd(
            &self.control_queue,
            &control_buf,
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
        let control_buf = self.control_buf.lock();
        let code = control_cmd(
            &self.control_queue,
            &control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    pub fn resource_unref(&self, resource_id: u32) -> Result<(), VirtioDeviceError> {
        let req = VirtioGpuResourceUnref {
            hdr: ctrl_hdr(VIRTIO_GPU_CMD_RESOURCE_UNREF),
            resource_id,
            padding: 0,
        };
        let control_buf = self.control_buf.lock();
        let code = control_cmd(
            &self.control_queue,
            &control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)?;
        self.backing_owners.lock().remove(&resource_id);
        Ok(())
    }

    /// Attempts cleanup and records the resource for a later retry on failure.
    fn defer_resource_unref(&self, resource_id: u32) {
        if self.resource_unref(resource_id).is_err() {
            self.pending_resource_cleanup.lock().insert(resource_id);
        }
    }

    /// Retries resource cleanup without holding a spin lock across device I/O.
    fn drain_pending_resource_cleanup(&self) {
        let pending = core::mem::take(&mut *self.pending_resource_cleanup.lock());
        for resource_id in pending {
            if self.resource_unref(resource_id).is_err() {
                self.pending_resource_cleanup.lock().insert(resource_id);
            }
        }
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
        let control_buf = self.control_buf.lock();
        let code = control_cmd(
            &self.control_queue,
            &control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    /// 3D: create a virgl rendering context.
    pub fn ctx_create(
        &self,
        ctx_id: u32,
        context_init: u32,
        debug_name: &[u8],
    ) -> Result<(), VirtioDeviceError> {
        let mut name = [0u8; 64];
        let copy_len = debug_name.len().min(64);
        name[..copy_len].copy_from_slice(&debug_name[..copy_len]);
        let req = super::VirtioGpuCtxCreate {
            hdr: ctrl_hdr_3d(super::VIRTIO_GPU_CMD_CTX_CREATE, ctx_id),
            nlen: copy_len as u32,
            context_init,
            debug_name: name,
        };
        let control_buf = self.control_buf.lock();
        let code = control_cmd(
            &self.control_queue,
            &control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    /// 3D: destroy a virgl rendering context.
    pub fn ctx_destroy(&self, ctx_id: u32) -> Result<(), VirtioDeviceError> {
        let req = super::VirtioGpuCtxDestroy {
            hdr: ctrl_hdr_3d(super::VIRTIO_GPU_CMD_CTX_DESTROY, ctx_id),
        };
        let control_buf = self.control_buf.lock();
        let code = control_cmd(
            &self.control_queue,
            &control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    /// 3D: attach a resource to the virgl context.
    pub fn ctx_attach_resource(
        &self,
        ctx_id: u32,
        resource_id: u32,
    ) -> Result<(), VirtioDeviceError> {
        let req = super::VirtioGpuCtxResource {
            hdr: ctrl_hdr_3d(super::VIRTIO_GPU_CMD_CTX_ATTACH_RESOURCE, ctx_id),
            resource_id,
            padding: 0,
        };
        let control_buf = self.control_buf.lock();
        let code = control_cmd(
            &self.control_queue,
            &control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    /// 3D: detach a resource from a virgl rendering context.
    pub fn ctx_detach_resource(
        &self,
        ctx_id: u32,
        resource_id: u32,
    ) -> Result<(), VirtioDeviceError> {
        let req = super::VirtioGpuCtxResource {
            hdr: ctrl_hdr_3d(super::VIRTIO_GPU_CMD_CTX_DETACH_RESOURCE, ctx_id),
            resource_id,
            padding: 0,
        };
        let control_buf = self.control_buf.lock();
        let code = control_cmd(
            &self.control_queue,
            &control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    /// 3D: submit a virgl command buffer to the host (unfenced — the response
    /// acknowledges receipt, not completion).
    pub fn submit_3d(&self, ctx_id: u32, size: u32, data: &[u8]) -> Result<(), VirtioDeviceError> {
        self.submit_3d_with_fence(ctx_id, size, data, 0, 0)
    }

    /// 3D: submit a virgl command buffer with `VIRTIO_GPU_FLAG_FENCE` set.
    ///
    /// The device defers the response until the command has completed, so the
    /// synchronous [`submit_control`] wait below returns only after rendering
    /// finishes. This is how the render→scanout path synchronizes.
    pub fn submit_3d_fenced(
        &self,
        ctx_id: u32,
        size: u32,
        data: &[u8],
        fence_id: u64,
    ) -> Result<(), VirtioDeviceError> {
        self.submit_3d_with_fence(ctx_id, size, data, super::VIRTIO_GPU_FLAG_FENCE, fence_id)
    }

    /// Queues a fenced virgl command without waiting for device completion.
    pub fn submit_3d_fenced_async(
        &self,
        ctx_id: u32,
        size: u32,
        data: &[u8],
        fence_id: u64,
        completion: Arc<dyn GpuCommandCompletion>,
    ) -> Result<GpuCommandTicket, VirtioDeviceError> {
        let (submit_buf, total_len, resp_len) =
            build_submit_3d(ctx_id, size, data, super::VIRTIO_GPU_FLAG_FENCE, fence_id)?;
        Ok(submit_control_at_ticket(
            &self.control_queue,
            &submit_buf,
            total_len,
            total_len,
            resp_len,
            Some(completion),
        ))
    }

    fn submit_3d_with_fence(
        &self,
        ctx_id: u32,
        size: u32,
        data: &[u8],
        flags: u32,
        fence_id: u64,
    ) -> Result<(), VirtioDeviceError> {
        let (submit_buf, total_len, resp_len) =
            build_submit_3d(ctx_id, size, data, flags, fence_id)?;

        let (code, _) = submit_control_at(
            &self.control_queue,
            &submit_buf,
            total_len,
            total_len,
            resp_len,
        )?;
        check_ok(code)
    }

    /// 3D: query capset info from the device.
    fn get_capset_info_at(
        &self,
        capset_index: u32,
    ) -> Result<super::VirtioGpuRespCapsetInfo, VirtioDeviceError> {
        let req = super::VirtioGpuGetCapsetInfo {
            hdr: ctrl_hdr(super::VIRTIO_GPU_CMD_GET_CAPSET_INFO),
            capset_index,
            padding: 0,
        };
        let resp_len = size_of::<super::VirtioGpuRespCapsetInfo>();
        let control_buf = self.control_buf.lock();
        let code = control_cmd(&self.control_queue, &control_buf, &req, resp_len)?;
        if code != super::VIRTIO_GPU_RESP_OK_CAPSET_INFO {
            return Err(VirtioDeviceError::UnsupportedConfig);
        }
        let resp_slice = Slice::new(
            control_buf.clone(),
            CTRL_RESP_OFFSET..CTRL_RESP_OFFSET + resp_len,
        );
        resp_slice.sync_from_device().unwrap();
        let resp: super::VirtioGpuRespCapsetInfo = resp_slice.read_val(0).unwrap();
        Ok(resp)
    }

    /// 3D: finds device capability information by capset id.
    pub fn get_capset_info(
        &self,
        capset_id: u32,
    ) -> Result<super::VirtioGpuRespCapsetInfo, VirtioDeviceError> {
        for index in 0..self.num_capsets {
            let info = self.get_capset_info_at(index)?;
            if info.capset_id == capset_id {
                return Ok(info);
            }
        }
        Err(VirtioDeviceError::UnsupportedConfig)
    }

    /// Returns the bitmask of capset ids actually advertised by the device.
    pub fn supported_capset_ids(&self) -> Result<u64, VirtioDeviceError> {
        let mut ids = 0u64;
        for index in 0..self.num_capsets {
            let info = self.get_capset_info_at(index)?;
            if info.capset_id < u64::BITS {
                ids |= 1u64 << info.capset_id;
            }
        }
        Ok(ids)
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
        if capset_size > MAX_CAPSET_SIZE {
            return Err(VirtioDeviceError::UnsupportedConfig);
        }

        // The device returns the actual capset size, which may be smaller
        // than the advertised maximum.
        let resp_len = size_of::<VirtioGpuCtrlHdr>()
            .checked_add(capset_size)
            .ok_or(VirtioDeviceError::InvalidQueueArgs)?;
        let req_len = size_of::<super::VirtioGpuGetCapset>();
        let buffer_len = req_len
            .checked_add(resp_len)
            .ok_or(VirtioDeviceError::InvalidQueueArgs)?;
        let capset_buf = Arc::new(
            DmaStream::alloc(buffer_len.div_ceil(PAGE_SIZE), false)
                .map_err(VirtioDeviceError::ResourceAlloc)?,
        );
        let req_slice = Slice::new(capset_buf.clone(), 0..req_len);
        req_slice.write_val(0, &req).unwrap();

        let (code, used_len) =
            submit_control_at(&self.control_queue, &capset_buf, req_len, req_len, resp_len)?;
        if code != super::VIRTIO_GPU_RESP_OK_CAPSET {
            return Err(VirtioDeviceError::UnsupportedConfig);
        }

        let data_len = used_len.saturating_sub(size_of::<VirtioGpuCtrlHdr>());
        let resp_slice = Slice::new(capset_buf, req_len..req_len + resp_len);
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
        ctx_id: u32,
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
            hdr: ctrl_hdr_3d(super::VIRTIO_GPU_CMD_TRANSFER_TO_HOST_3D, ctx_id),
            box_: super::VirtioGpuBox { x, y, z, w, h, d },
            offset,
            resource_id,
            level,
            stride,
            layer_stride,
        };
        let control_buf = self.control_buf.lock();
        let code = control_cmd(
            &self.control_queue,
            &control_buf,
            &req,
            size_of::<VirtioGpuCtrlHdr>(),
        )?;
        check_ok(code)
    }

    /// 3D: transfer data from host to guest for a 3D resource.
    #[expect(clippy::too_many_arguments)]
    pub fn transfer_from_host_3d(
        &self,
        ctx_id: u32,
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
            hdr: ctrl_hdr_3d(super::VIRTIO_GPU_CMD_TRANSFER_FROM_HOST_3D, ctx_id),
            box_: super::VirtioGpuBox { x, y, z, w, h, d },
            offset,
            resource_id,
            level,
            stride,
            layer_stride,
        };
        let control_buf = self.control_buf.lock();
        let code = control_cmd(
            &self.control_queue,
            &control_buf,
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

/// Builds a 3D control header for the given virgl context.
fn ctrl_hdr_3d(type_: u32, ctx_id: u32) -> VirtioGpuCtrlHdr {
    VirtioGpuCtrlHdr {
        ctx_id,
        ..ctrl_hdr(type_)
    }
}

fn check_ok(code: u32) -> Result<(), VirtioDeviceError> {
    match code {
        VIRTIO_GPU_RESP_OK_NODATA => Ok(()),
        _ => {
            ostd::warn!("virtio-gpu control request failed: response = {:#x}", code);
            Err(VirtioDeviceError::UnsupportedConfig)
        }
    }
}

fn build_submit_3d(
    ctx_id: u32,
    size: u32,
    data: &[u8],
    flags: u32,
    fence_id: u64,
) -> Result<(Arc<DmaStream>, usize, usize), VirtioDeviceError> {
    use super::VirtioGpuCmdSubmit;

    if data.len() != size as usize {
        return Err(VirtioDeviceError::InvalidQueueArgs);
    }
    let mut hdr = ctrl_hdr_3d(super::VIRTIO_GPU_CMD_SUBMIT_3D, ctx_id);
    hdr.flags = flags;
    hdr.fence_id = fence_id;
    let req = VirtioGpuCmdSubmit {
        hdr,
        size,
        padding: 0,
    };
    let req_len = size_of::<VirtioGpuCmdSubmit>();
    let total_len = req_len
        .checked_add(data.len())
        .ok_or(VirtioDeviceError::InvalidQueueArgs)?;
    let resp_len = size_of::<VirtioGpuCtrlHdr>();
    let buffer_len = total_len
        .checked_add(resp_len)
        .ok_or(VirtioDeviceError::InvalidQueueArgs)?;
    let submit_buf = Arc::new(
        DmaStream::alloc(buffer_len.div_ceil(PAGE_SIZE), false)
            .map_err(VirtioDeviceError::ResourceAlloc)?,
    );
    let req_slice = Slice::new(
        submit_buf.clone(),
        CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + total_len,
    );
    req_slice.write_val(0, &req).unwrap();
    req_slice.write_bytes(req_len, data).unwrap();
    Ok((submit_buf, total_len, resp_len))
}

/// Submits a control request of `req_len` bytes (already written into the
/// buffer by the caller) and waits for a response of at most `resp_len`
/// bytes, returning the response type code and the actual used length.
///
/// The device may legitimately write fewer bytes than the buffer size (for
/// example `GET_CAPSET` returns the actual capset size, not the maximum),
/// so the used length is validated against the header size, not `resp_len`.
fn submit_control(
    queue: &Arc<ControlQueue>,
    buf: &Arc<DmaStream>,
    req_len: usize,
    resp_len: usize,
) -> Result<(u32, usize), VirtioDeviceError> {
    submit_control_at(queue, buf, req_len, CTRL_RESP_OFFSET, resp_len)
}

/// Like [`submit_control`], but places the response at a caller-selected offset.
///
/// Variable-sized `SUBMIT_3D` requests use an offset immediately after the
/// command stream so they are not constrained by the fixed small-command
/// layout in [`GpuDevice::control_buf`].
fn submit_control_at(
    queue: &Arc<ControlQueue>,
    buf: &Arc<DmaStream>,
    req_len: usize,
    resp_offset: usize,
    resp_len: usize,
) -> Result<(u32, usize), VirtioDeviceError> {
    submit_control_at_ticket(queue, buf, req_len, resp_offset, resp_len, None).wait_for_response()
}

fn submit_control_at_ticket(
    queue: &Arc<ControlQueue>,
    buf: &Arc<DmaStream>,
    req_len: usize,
    resp_offset: usize,
    resp_len: usize,
    listener: Option<Arc<dyn GpuCommandCompletion>>,
) -> GpuCommandTicket {
    let req_slice = Slice::new(buf.clone(), CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len);
    req_slice.sync_to_device().unwrap();

    let response = Slice::new(buf.clone(), resp_offset..resp_offset + resp_len);
    let control = queue.submit_dma_bufs(&[&req_slice], &[&response], listener);
    GpuCommandTicket {
        control,
        response,
        response_len: resp_len,
    }
}

/// Sends a fixed-size control request and waits for its response.
fn control_cmd<T: ostd_pod::Pod>(
    queue: &Arc<ControlQueue>,
    buf: &Arc<DmaStream>,
    req: &T,
    resp_len: usize,
) -> Result<u32, VirtioDeviceError> {
    let req_len = size_of::<T>();
    let req_slice = Slice::new(buf.clone(), CTRL_REQ_OFFSET..CTRL_REQ_OFFSET + req_len);
    req_slice.write_val(0, req).unwrap();
    let (code, used_len) = submit_control(queue, buf, req_len, resp_len)?;
    if used_len < resp_len {
        return Err(VirtioDeviceError::UnsupportedConfig);
    }
    Ok(code)
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
        match queue.pop_used_once_with_min_bytes(CURSOR_COMPLETION_BYTES) {
            Ok(_) => return Ok(()),
            Err(PopUsedError::NotReady) => spin_loop(),
            Err(error) => {
                ostd::error!("invalid virtio-gpu cursor completion: {:?}", error);
                return Err(VirtioDeviceError::UnsupportedConfig);
            }
        }
    }
}

/// Chooses the largest power-of-two queue size up to the driver's cap.
fn cursor_queue_size(device_max: u16) -> Option<u16> {
    queue_size_up_to_cap(device_max, 1)
}

/// Chooses a power-of-two control queue with room for request and response.
fn control_queue_size(device_max: u16) -> Option<u16> {
    queue_size_up_to_cap(device_max, 2)
}

fn queue_size_up_to_cap(device_max: u16, min_size: u16) -> Option<u16> {
    let capped = device_max.min(MAX_QUEUE_SIZE);
    if capped < min_size {
        return None;
    }
    let mut size = 1;
    while size <= capped / 2 {
        size *= 2;
    }
    Some(size)
}

/// Queries the display info and returns scanout 0's dimensions.
fn query_display_info(
    queue: &Arc<ControlQueue>,
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
    if one.enabled == 0 {
        return Err(VirtioDeviceError::UnsupportedConfig);
    }
    Ok((one.r.width, one.r.height))
}

/// Allocates a DMA backing store for a `width`x`height` B8G8R8X8 resource.
fn alloc_framebuffer(width: u32, height: u32) -> Result<(Arc<DmaStream>, u32), VirtioDeviceError> {
    let nbytes = framebuffer_len(width, height).ok_or(VirtioDeviceError::InvalidQueueArgs)?;
    let nframes = (nbytes as usize).div_ceil(PAGE_SIZE);
    let framebuffer = DmaStream::alloc(nframes, false).map_err(VirtioDeviceError::ResourceAlloc)?;
    Ok((Arc::new(framebuffer), nbytes))
}

fn framebuffer_len(width: u32, height: u32) -> Option<u32> {
    (width as usize)
        .checked_mul(height as usize)
        .and_then(|pixels| pixels.checked_mul(BPP))
        .and_then(|bytes| u32::try_from(bytes).ok())
}

fn next_resource_id(current: u32) -> Option<u32> {
    current.checked_add(1)
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
    fn control_queue_requires_request_and_response_descriptors() {
        assert_eq!(control_queue_size(0), None);
        assert_eq!(control_queue_size(1), None);
        assert_eq!(control_queue_size(2), Some(2));
        assert_eq!(control_queue_size(63), Some(32));
        assert_eq!(control_queue_size(256), Some(64));
    }

    #[ktest]
    fn cursor_completion_has_no_response_body() {
        assert_eq!(CURSOR_COMPLETION_BYTES, 0);
    }

    #[ktest]
    fn framebuffer_length_rejects_virtio_backing_overflow() {
        assert_eq!(framebuffer_len(1024, 768), Some(1024 * 768 * 4));
        assert_eq!(
            framebuffer_len(u16::MAX as u32 + 1, u16::MAX as u32 + 1),
            None
        );
    }

    #[ktest]
    fn state_changing_commands_require_nodata_response() {
        assert!(check_ok(VIRTIO_GPU_RESP_OK_NODATA).is_ok());
        assert!(check_ok(VIRTIO_GPU_RESP_OK_DISPLAY_INFO).is_err());
    }

    #[ktest]
    fn resource_ids_do_not_wrap_into_reserved_values() {
        assert_eq!(next_resource_id(2), Some(3));
        assert_eq!(next_resource_id(u32::MAX), None);
    }
}
