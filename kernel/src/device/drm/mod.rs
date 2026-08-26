// SPDX-License-Identifier: MPL-2.0

//! DRM (Direct Rendering Manager) character device support.
//!
//! Provides two device nodes backed by the first discovered virtio-gpu device:
//!
//! - `/dev/dri/card0` (major=226, minor=0) — primary node with full KMS +
//!   dumb-buffer + GEM ioctls.
//! - `/dev/dri/renderD128` (major=226, minor=128) — render node with
//!   GEM + dumb-buffer ioctls only (no KMS).
//!
//! Dumb buffers are carved out of a single physically-contiguous [`Vmo`] pool
//! so that (a) `mmap` can map any buffer via the standard `Mappable::Vmo` path
//! and (b) each buffer is backed by one contiguous guest-physical span that
//! virtio-gpu's `RESOURCE_ATTACH_BACKING` accepts.

mod atomic;
mod cursor;
mod dumb;
mod fence;
mod gem;
mod ioctl;
mod kms;
mod plane;
mod prime;
mod property;
mod virtio_gpu;

use core::sync::atomic::{AtomicU32, AtomicU64, Ordering};

use aster_time::read_monotonic_time;
use aster_virtio::device::gpu::{device::GpuDevice, first_device};
use device_id::{DeviceId, MajorId, MinorId};
use ostd::mm::{Paddr, VmIo};

use self::cursor::{CURSOR_SIZE, CursorState, DrmModeCursor, DrmModeCursor2};
use crate::{
    context::current_userspace,
    device::{Device, DeviceType, DevtmpfsInodeMeta, registry::char},
    events::IoEvents,
    fs::{
        file::{
            Mappable, PerOpenFileOps, StatusFlags,
            file_table::{FileDesc, RawFileDesc, WithFileTable},
        },
        vfs::{inode::FileOps, path::Path},
    },
    prelude::*,
    process::{
        posix_thread::FileTableRefMut,
        signal::{PollHandle, Pollable, Pollee},
    },
    util::ioctl::{RawIoctl, dispatch_ioctl},
    vm::page_cache::{Vmo, VmoFlags, VmoOptions},
};

/// Linux DRM character-device major number.
const DRM_MAJOR: u16 = 226;

/// Kernel driver name reported by `DRM_IOCTL_VERSION`. Must match Mesa's
/// DRI driver file name (`virtio_gpu_dri.so`), which Mesa's loader derives
/// from this string.
const DRIVER_NAME: &str = "virtio_gpu";
const DRIVER_DATE: &str = "20260818";
const DRIVER_DESC: &str = "Asterinas virtio-gpu 2D driver";

/// KMS object ids. The virtio-gpu device exposes a single CRTC/encoder/connector.
const CRTC_ID: u32 = 1;
const ENCODER_ID: u32 = 1;
const CONNECTOR_ID: u32 = 1;

/// `DRM_MODE_CONNECTOR_VIRTUAL`, the connector type Linux's virtio-gpu reports.
const DRM_MODE_CONNECTOR_VIRTUAL: u32 = 15;
/// `DRM_MODE_ENCODER_VIRTUAL`.
const DRM_MODE_ENCODER_VIRTUAL: u32 = 5;
/// `DRM_MODE_CONNECTED`.
const DRM_MODE_CONNECTED: u32 = 1;
/// `DRM_MODE_TYPE_PREFERRED`.
const DRM_MODE_TYPE_PREFERRED: u32 = 8;

/// `DRM_CAP_DUMB_BUFFER` etc. (include/uapi/drm/drm.h).
const DRM_CAP_DUMB_BUFFER: u64 = 1;
const DRM_CAP_DUMB_PREFERRED_DEPTH: u64 = 3;
const DRM_CAP_DUMB_PREFER_SHADOW: u64 = 4;
const DRM_CAP_CURSOR_WIDTH: u64 = 8;
const DRM_CAP_CURSOR_HEIGHT: u64 = 9;
/// `DRM_CAP_PRIME`.
const DRM_CAP_PRIME: u64 = 0x5;
/// `DRM_PRIME_CAP_IMPORT | DRM_PRIME_CAP_EXPORT`.
const DRM_PRIME_CAP_IMPORT_EXPORT: u64 = 0x3;

/// `DRM_CLIENT_CAP_*` values accepted by `SET_CLIENT_CAP`.
const DRM_CLIENT_CAP_STEREO_3D: u64 = 1;
const DRM_CLIENT_CAP_UNIVERSAL_PLANES: u64 = 2;
const DRM_CLIENT_CAP_ATOMIC: u64 = 3;
const DRM_CLIENT_CAP_ASPECT_RATIO: u64 = 4;
const DRM_CLIENT_CAP_WRITEBACK_CONNECTORS: u64 = 5;
const DRM_CLIENT_CAP_CURSOR_PLANE_HOTSPOT: u64 = 6;

/// `DRM_MODE_OBJECT_*` type constants for `MODE_OBJ_GETPROPERTIES` and `MODE_ATOMIC`.
const DRM_MODE_OBJECT_CRTC: u32 = 0xcccccccc;
const DRM_MODE_OBJECT_CONNECTOR: u32 = 0xc0c0c0c0;
const DRM_MODE_OBJECT_PLANE: u32 = 0xeeeeeeee;

/// `DRM_MODE_ATOMIC_*` flags.
const DRM_MODE_ATOMIC_TEST_ONLY: u32 = 0x0100;
const DRM_MODE_ATOMIC_NONBLOCK: u32 = 0x0200;
const DRM_MODE_ATOMIC_ALLOW_MODESET: u32 = 0x0400;

/// `DRM_MODE_PAGE_FLIP_*` flags. `DRM_MODE_PAGE_FLIP_EVENT` is also accepted
/// in `DRM_IOCTL_MODE_ATOMIC` commit flags (as wlroots does).
const DRM_MODE_PAGE_FLIP_EVENT: u32 = 0x01;
const DRM_MODE_PAGE_FLIP_ASYNC: u32 = 0x02;

/// `DRM_EVENT_FLIP_COMPLETE` event type for `drm_event_vblank`.
const DRM_EVENT_FLIP_COMPLETE: u32 = 0x02;

/// `DRM_PLANE_TYPE_PRIMARY` — the plane type enum value.
const DRM_PLANE_TYPE_PRIMARY: u32 = 1;

/// Our single primary plane id.
const PRIMARY_PLANE_ID: u32 = 1;

/// Size of the single contiguous dumb-buffer pool, in bytes.
///
/// Covers framebuffers up to ~2048x2048 at 32 bpp; enough for the QEMU
/// virtio-gpu scanouts (1024x768 by default) and a generous multi-resolution
/// headroom. A single pool is required because the mmap path maps one
/// `Mappable::Vmo` per file and selects a buffer by its byte offset within it.
///
/// 64 MiB holds ~15 1280x800@32bpp buffers — enough for a GBM surface's
/// back/front/shadow buffers plus a few app allocations. Note the allocator
/// is a bump allocator: destroyed buffers are not reclaimed yet.
const DUMB_POOL_SIZE: usize = 64 * 1024 * 1024;

/// Maximum scanout width/height reported by `MODE_GETRESOURCES`.
const MAX_RESOLUTION: u32 = 8192;

// ---------------------------------------------------------------------------
// Shared device-level state
// ---------------------------------------------------------------------------

/// Device-level state shared across all open files (card0 and renderD128).
///
/// The dumb-buffer pool and GEM object table live here so that GEM_FLINK
/// names are global and render-node opens share the same `GpuDevice`.
struct GpuManager {
    gpu: Arc<GpuDevice>,
    /// The contiguous pool all dumb buffers are carved out of.
    pool: SpinLock<Option<Arc<Vmo>>>,
    /// Bump-allocator cursor into the pool (page-aligned).
    next_offset: SpinLock<usize>,
    /// GEM objects by id. `object_id` is a monotonically increasing counter.
    gem_objects: SpinLock<BTreeMap<u32, Arc<GemObject>>>,
    /// Global FLINK name → object_id.
    gem_names: SpinLock<BTreeMap<u32, u32>>,
    /// GEM object_id → virtio-gpu 3D resource id (set by `RESOURCE_CREATE`).
    gem_resources: SpinLock<BTreeMap<u32, u32>>,
    /// Serializes the global GEM-object to host-resource transaction.
    resource_creation: Mutex<()>,
    next_gem_id: AtomicU32,
    /// Monotonic virgl context id allocator (context id 0 is reserved).
    next_context_id: AtomicU32,
    /// Property manager for atomic modesetting.
    property_manager: property::PropertyManager,
    /// Monotonic page-flip sequence number (our "vblank counter").
    flip_sequence: AtomicU32,
    /// Monotonic virtio-gpu fence id allocator (3D SUBMIT_3D fences).
    next_fence_id: AtomicU64,
    /// Primary-node file id that currently owns DRM master, or zero.
    master_id: AtomicU64,
    next_file_id: AtomicU64,
}

impl GpuManager {
    fn new(gpu: Arc<GpuDevice>) -> Self {
        Self {
            gpu,
            pool: SpinLock::new(None),
            next_offset: SpinLock::new(0),
            gem_objects: SpinLock::new(BTreeMap::new()),
            gem_names: SpinLock::new(BTreeMap::new()),
            gem_resources: SpinLock::new(BTreeMap::new()),
            resource_creation: Mutex::new(()),
            next_gem_id: AtomicU32::new(1),
            next_context_id: AtomicU32::new(1),
            property_manager: property::PropertyManager::new(),
            flip_sequence: AtomicU32::new(0),
            next_fence_id: AtomicU64::new(1),
            master_id: AtomicU64::new(0),
            next_file_id: AtomicU64::new(1),
        }
    }

    /// Returns the global GpuManager, initialised on first call.
    fn get_or_init() -> Arc<Self> {
        static INSTANCE: spin::Once<Arc<GpuManager>> = spin::Once::new();
        INSTANCE
            .call_once(|| {
                let gpu = first_device()
                    .expect("GpuManager::get_or_init called without a virtio-gpu device");
                Arc::new(GpuManager::new(gpu))
            })
            .clone()
    }

    /// Returns the dumb-buffer pool, allocating it on first use.
    fn ensure_pool(&self) -> Result<Arc<Vmo>> {
        let mut guard = self.pool.lock();
        if let Some(pool) = guard.as_ref() {
            return Ok(pool.clone());
        }
        let pool = VmoOptions::new(DUMB_POOL_SIZE)
            .flags(VmoFlags::CONTIGUOUS)
            .alloc()?;
        *guard = Some(pool.clone());
        Ok(pool)
    }

    /// Base guest physical address of the pool.
    fn pool_paddr(&self) -> Result<Paddr> {
        self.pool
            .lock()
            .as_ref()
            .and_then(|pool| pool.paddr())
            .ok_or_else(|| Error::with_message(Errno::ENOMEM, "dumb buffer pool has no memory"))
    }
}

// ---------------------------------------------------------------------------
// GEM object (global)
// ---------------------------------------------------------------------------

/// A GEM object wrapping a dumb-buffer allocation.
///
/// GEM objects are reference-counted and may have a global FLINK name.
/// They live in [`GpuManager::gem_objects`] and are looked up by per-file
/// handles through [`DriInner::handles`].
struct GemObject {
    name: AtomicU32, // 0 = not flinked
    ref_count: AtomicU32,
    buffer: DumbBuffer,
}

/// A dumb buffer: a page-aligned sub-range of the shared pool.
#[derive(Debug, Clone, Copy)]
struct DumbBuffer {
    offset: usize,
    size: usize,
    width: u32,
    height: u32,
    bpp: u32,
}

impl DumbBuffer {
    fn mapped_range(self) -> Option<core::ops::Range<usize>> {
        let mapped_size = self
            .size
            .checked_add(PAGE_SIZE - 1)?
            .checked_div(PAGE_SIZE)?
            .checked_mul(PAGE_SIZE)?;
        Some(self.offset..self.offset.checked_add(mapped_size)?)
    }
}

/// A registered framebuffer referencing a dumb buffer.
#[derive(Debug, Clone, Copy)]
struct Framebuffer {
    object_id: u32,
    width: u32,
    height: u32,
    offset: u32,
    pitch: u32,
    pixel_format: u32,
}

// ---------------------------------------------------------------------------
// Node types
// ---------------------------------------------------------------------------

/// Whether this handle was opened from the primary node or the render node.
#[derive(Clone, Copy, PartialEq, Eq)]
enum DriNodeType {
    Primary,
    Render,
}

// ---------------------------------------------------------------------------
// Device implementations
// ---------------------------------------------------------------------------

/// The primary DRM node: `/dev/dri/card0`.
struct DriPrimary {
    gpu_manager: Arc<GpuManager>,
}

/// The render node: `/dev/dri/renderD128`.
struct DriRender {
    gpu_manager: Arc<GpuManager>,
}

impl Device for DriPrimary {
    fn type_(&self) -> DeviceType {
        DeviceType::Char
    }

    fn id(&self) -> DeviceId {
        DeviceId::new(MajorId::new(DRM_MAJOR), MinorId::new(0))
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsInodeMeta<'_>> {
        Some(DevtmpfsInodeMeta::new("dri/card0"))
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        self.gpu_manager.ensure_pool()?;
        let gpu = &self.gpu_manager.gpu;
        Ok(Box::new(DriHandle::new(
            self.gpu_manager.clone(),
            DriNodeType::Primary,
            gpu.width(),
            gpu.height(),
        )))
    }
}

impl Device for DriRender {
    fn type_(&self) -> DeviceType {
        DeviceType::Char
    }

    fn id(&self) -> DeviceId {
        DeviceId::new(MajorId::new(DRM_MAJOR), MinorId::new(128))
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsInodeMeta<'_>> {
        Some(DevtmpfsInodeMeta::new("dri/renderD128"))
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        self.gpu_manager.ensure_pool()?;
        let gpu = &self.gpu_manager.gpu;
        Ok(Box::new(DriHandle::new(
            self.gpu_manager.clone(),
            DriNodeType::Render,
            gpu.width(),
            gpu.height(),
        )))
    }
}

// ---------------------------------------------------------------------------
// Per-open-file handle
// ---------------------------------------------------------------------------

/// Per-open-file DRM state.
///
/// GEM/dumb-buffer handles and framebuffer ids are namespaced per file,
/// matching Linux's per-`drm_file` handle space. The pool and GEM object
/// table are shared across all opens via [`GpuManager`].
struct DriHandle {
    gpu_manager: Arc<GpuManager>,
    node_type: DriNodeType,
    file_id: u64,
    /// Legacy virgl context associated with this open DRM file.
    context: Mutex<VirglContext>,
    /// Serializes validation, device updates, and per-file cursor state.
    cursor_operation: Mutex<()>,
    inner: SpinLock<DriInner>,
    /// Notifies readers/pollers when page-flip events are queued.
    pollee: Pollee,
}

#[derive(Debug)]
struct VirglContext {
    id: u32,
    is_created: bool,
}

#[derive(Debug)]
struct DriInner {
    /// Per-file handle → GEM object_id.
    handles: BTreeMap<u32, u32>,
    next_handle: u32,
    framebuffers: BTreeMap<u32, Framebuffer>,
    next_fb_id: u32,
    current_fb_id: Option<u32>,
    current_width: u32,
    current_height: u32,
    /// Current mode blob id (set via atomic MODE_ID property).
    mode_blob: Option<u32>,
    /// Pending page-flip completion events, readable via `read()`.
    events: VecDeque<DrmEventVblank>,
    /// Cursor resource and position owned by this open DRM file.
    cursor: CursorState,
}

impl DriHandle {
    fn new(
        gpu_manager: Arc<GpuManager>,
        node_type: DriNodeType,
        current_width: u32,
        current_height: u32,
    ) -> Self {
        let context_id = gpu_manager.next_context_id.fetch_add(1, Ordering::Relaxed);
        let file_id = gpu_manager.next_file_id.fetch_add(1, Ordering::Relaxed);
        if matches!(node_type, DriNodeType::Primary) {
            let _ = gpu_manager.master_id.compare_exchange(
                0,
                file_id,
                Ordering::AcqRel,
                Ordering::Acquire,
            );
        }
        Self {
            gpu_manager,
            node_type,
            file_id,
            context: Mutex::new(VirglContext {
                id: context_id,
                is_created: false,
            }),
            cursor_operation: Mutex::new(()),
            inner: SpinLock::new(DriInner {
                handles: BTreeMap::new(),
                next_handle: 1,
                framebuffers: BTreeMap::new(),
                next_fb_id: 1,
                current_fb_id: None,
                current_width,
                current_height,
                mode_blob: None,
                events: VecDeque::new(),
                cursor: CursorState::default(),
            }),
            pollee: Pollee::new(),
        }
    }

    /// Returns true if KMS ioctls are forbidden on this handle.
    fn is_render_node(&self) -> bool {
        matches!(self.node_type, DriNodeType::Render)
    }

    fn is_master(&self) -> bool {
        !self.is_render_node() && self.gpu_manager.master_id.load(Ordering::Acquire) == self.file_id
    }

    fn require_master(&self) -> Result<()> {
        if self.is_render_node() {
            return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
        }
        if !self.is_master() {
            return_errno_with_message!(Errno::EACCES, "DRM master is owned by another file");
        }
        Ok(())
    }

    fn set_master(&self) -> Result<()> {
        if self.is_render_node() {
            return_errno_with_message!(Errno::EOPNOTSUPP, "render nodes cannot become DRM master");
        }
        match self.gpu_manager.master_id.compare_exchange(
            0,
            self.file_id,
            Ordering::AcqRel,
            Ordering::Acquire,
        ) {
            Ok(_) => Ok(()),
            Err(owner) if owner == self.file_id => Ok(()),
            Err(_) => return_errno_with_message!(Errno::EBUSY, "DRM master is already owned"),
        }
    }

    fn drop_master(&self) -> Result<()> {
        self.gpu_manager
            .master_id
            .compare_exchange(self.file_id, 0, Ordering::AcqRel, Ordering::Acquire)
            .map(|_| ())
            .map_err(|_| Error::with_message(Errno::EINVAL, "file does not own DRM master"))
    }

    /// Creates the per-file legacy virgl context on first 3D use.
    fn ensure_virgl_context(&self) -> Result<u32> {
        let mut context = self.context.lock();
        if !context.is_created {
            // `VIRTIO_GPU_F_CONTEXT_INIT` is not negotiated, so the legacy
            // context-create payload must leave `context_init` at zero.
            self.gpu_manager
                .gpu
                .ctx_create(context.id, 0, b"asterinas-drm")
                .map_err(|_| Error::with_message(Errno::EIO, "cannot create virgl context"))?;
            context.is_created = true;
        }
        Ok(context.id)
    }

    /// Queues a page-flip completion event for this file.
    ///
    /// Our present path is synchronous (the virtio-gpu control command has
    /// completed by the time the ioctl returns), so the event is queued
    /// immediately, right after the flip is applied.
    fn queue_flip_event(&self, user_data: u64) {
        let now = read_monotonic_time();
        let sequence = self
            .gpu_manager
            .flip_sequence
            .fetch_add(1, Ordering::Relaxed);
        let event = DrmEventVblank {
            type_: DRM_EVENT_FLIP_COMPLETE,
            length: size_of::<DrmEventVblank>() as u32,
            user_data,
            tv_sec: now.as_secs() as u32,
            tv_usec: now.subsec_micros(),
            sequence,
            crtc_id: CRTC_ID,
        };
        self.inner.lock().events.push_back(event);
        self.pollee.notify(IoEvents::IN);
    }

    /// Pops pending page-flip events into `writer`.
    fn read_events(&self, writer: &mut VmWriter) -> Result<usize> {
        let max_events = writer.avail() / size_of::<DrmEventVblank>();
        if max_events == 0 && writer.avail() != 0 {
            return_errno_with_message!(Errno::EINVAL, "the buffer is too short for an event");
        }

        let mut inner = self.inner.lock();
        let mut bytes = 0;
        while bytes / size_of::<DrmEventVblank>() < max_events {
            let Some(event) = inner.events.front().copied() else {
                break;
            };
            writer.write_val(&event)?;
            inner.events.pop_front();
            bytes += size_of::<DrmEventVblank>();
        }
        if bytes == 0 {
            return_errno_with_message!(Errno::EAGAIN, "no pending DRM events");
        }
        Ok(bytes)
    }
}

impl Drop for DriHandle {
    fn drop(&mut self) {
        let _ = self.gpu_manager.master_id.compare_exchange(
            self.file_id,
            0,
            Ordering::AcqRel,
            Ordering::Acquire,
        );
        let _cursor_operation = self.cursor_operation.lock();
        let (resource_id, position) = {
            let inner = self.inner.lock();
            (inner.cursor.resource_id, inner.cursor.position)
        };
        if let Some(resource_id) = resource_id {
            let _ = self
                .gpu_manager
                .gpu
                .clear_cursor(resource_id, position.x, position.y);
        }

        let context = self.context.get_mut();
        if context.is_created
            && let Err(error) = self.gpu_manager.gpu.ctx_destroy(context.id)
        {
            warn!("cannot destroy virgl context {}: {:?}", context.id, error);
        }
    }
}

// ---------------------------------------------------------------------------
// ioctl dispatch
// ---------------------------------------------------------------------------

impl Pollable for DriHandle {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        self.pollee
            .poll_with(mask, poller, || self.check_io_events())
    }
}

impl DriHandle {
    fn check_io_events(&self) -> IoEvents {
        let mut events = IoEvents::OUT;
        if !self.inner.lock().events.is_empty() {
            events |= IoEvents::IN;
        }
        events
    }
}

impl FileOps for DriHandle {
    fn read_at(
        &self,
        _offset: usize,
        writer: &mut VmWriter,
        status_flags: StatusFlags,
    ) -> Result<usize> {
        if status_flags.contains(StatusFlags::O_NONBLOCK) {
            return self.read_events(writer);
        }
        // Block until a page-flip event arrives.
        self.wait_events(IoEvents::IN, None, || self.read_events(writer))
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        Ok(0)
    }
}

impl PerOpenFileOps for DriHandle {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        false
    }

    fn mappable(&self) -> Result<Mappable> {
        let pool = self
            .gpu_manager
            .pool
            .lock()
            .as_ref()
            .cloned()
            .ok_or_else(|| {
                Error::with_message(Errno::ENODEV, "no dumb buffer has been created yet")
            })?;
        let inner = self.inner.lock();
        let objects = self.gpu_manager.gem_objects.lock();
        let ranges = inner
            .handles
            .values()
            .filter_map(|object_id| objects.get(object_id)?.buffer.mapped_range())
            .collect();
        Ok(Mappable::VmoRanges { vmo: pool, ranges })
    }

    fn ioctl(&self, _path: &Path, raw_ioctl: RawIoctl) -> Result<i32> {
        use ioctl::*;

        // `RMFB` passes its argument by value, so it cannot go through the typed
        // dispatch below.
        if raw_ioctl.cmd() == MODE_RMFB_CMD {
            if self.is_render_node() {
                return_errno_with_message!(
                    Errno::EOPNOTSUPP,
                    "KMS ioctl not available on render node"
                );
            }
            return kms::rm_fb(self, raw_ioctl.arg() as u32).map(|_| 0);
        }

        dispatch_ioctl!(match raw_ioctl {
            cmd @ GetVersion => {
                let mut version = cmd.read()?;
                version.version_major = 0;
                version.version_minor = 1;
                version.version_patchlevel = 0;
                copy_field(version.name, &mut version.name_len, DRIVER_NAME)?;
                copy_field(version.date, &mut version.date_len, DRIVER_DATE)?;
                copy_field(version.desc, &mut version.desc_len, DRIVER_DESC)?;
                cmd.write(&version)?;
                Ok(0)
            }
            cmd @ GetCap => {
                let mut cap = cmd.read()?;
                cap.value = match cap.capability {
                    DRM_CAP_DUMB_BUFFER => 1,
                    DRM_CAP_DUMB_PREFERRED_DEPTH => 24,
                    DRM_CAP_DUMB_PREFER_SHADOW => 0,
                    DRM_CAP_CURSOR_WIDTH => u64::from(CURSOR_SIZE),
                    DRM_CAP_CURSOR_HEIGHT => u64::from(CURSOR_SIZE),
                    DRM_CAP_PRIME => DRM_PRIME_CAP_IMPORT_EXPORT,
                    _ => {
                        return_errno_with_message!(Errno::EINVAL, "unsupported DRM capability")
                    }
                };
                cmd.write(&cap)?;
                Ok(0)
            }
            cmd @ SetClientCap => {
                let cap = cmd.read()?;
                match cap.capability {
                    DRM_CLIENT_CAP_STEREO_3D
                    | DRM_CLIENT_CAP_UNIVERSAL_PLANES
                    | DRM_CLIENT_CAP_ATOMIC
                    | DRM_CLIENT_CAP_ASPECT_RATIO
                    | DRM_CLIENT_CAP_WRITEBACK_CONNECTORS
                    | DRM_CLIENT_CAP_CURSOR_PLANE_HOTSPOT => Ok(0),
                    _ => {
                        return_errno_with_message!(Errno::EINVAL, "unsupported DRM client cap")
                    }
                }
            }
            cmd @ GemClose => {
                let req = cmd.read()?;
                gem::gem_close(self, req.handle).map(|_| 0)
            }
            cmd @ GemFlink => {
                self.require_master()?;
                let mut req = cmd.read()?;
                req.name = gem::gem_flink(self, req.handle)?;
                cmd.write(&req)?;
                Ok(0)
            }
            cmd @ GemOpen => {
                self.require_master()?;
                let mut req = cmd.read()?;
                let (handle, size) = gem::gem_open(self, req.name)?;
                req.handle = handle;
                req.size = size;
                cmd.write(&req)?;
                Ok(0)
            }
            cmd @ ModeGetResources => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                let mut res = cmd.read()?;
                let crtc_capacity = res.count_crtcs;
                let connector_capacity = res.count_connectors;
                let encoder_capacity = res.count_encoders;
                res.count_fbs = 0;
                res.count_crtcs = 1;
                res.count_connectors = 1;
                res.count_encoders = 1;
                res.min_width = 0;
                res.max_width = MAX_RESOLUTION;
                res.min_height = 0;
                res.max_height = MAX_RESOLUTION;
                if res.crtc_id_ptr != 0 && crtc_capacity >= 1 {
                    current_userspace!().write_val(res.crtc_id_ptr as usize, &CRTC_ID)?;
                }
                if res.connector_id_ptr != 0 && connector_capacity >= 1 {
                    current_userspace!().write_val(res.connector_id_ptr as usize, &CONNECTOR_ID)?;
                }
                if res.encoder_id_ptr != 0 && encoder_capacity >= 1 {
                    current_userspace!().write_val(res.encoder_id_ptr as usize, &ENCODER_ID)?;
                }
                cmd.write(&res)?;
                Ok(0)
            }
            cmd @ ModeGetConnector => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                kms::get_connector(self, cmd)
            }
            cmd @ ModeGetEncoder => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                kms::get_encoder(cmd)
            }
            cmd @ ModeGetCrtc => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                kms::get_crtc(self, cmd)
            }
            cmd @ ModeSetCrtc => {
                self.require_master()?;
                let req = cmd.read()?;
                kms::set_crtc(self, &req)?;
                Ok(0)
            }
            cmd @ ModeCursor => {
                self.require_master()?;
                let req = cmd.read()?;
                kms::set_cursor(self, req.into())?;
                Ok(0)
            }
            cmd @ ModeCursor2 => {
                self.require_master()?;
                let req = cmd.read()?;
                kms::set_cursor(self, req)?;
                Ok(0)
            }
            cmd @ ModeCreateDumb => {
                let req = cmd.read()?;
                cmd.write(&dumb::create_dumb(self, &req)?)?;
                Ok(0)
            }
            cmd @ ModeMapDumb => {
                let req = cmd.read()?;
                cmd.write(&dumb::map_dumb(self, &req)?)?;
                Ok(0)
            }
            cmd @ ModeDestroyDumb => {
                let req = cmd.read()?;
                dumb::destroy_dumb(self, &req)?;
                Ok(0)
            }
            cmd @ ModeAddFb => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                let mut req = cmd.read()?;
                req.fb_id = kms::add_fb(self, &req)?;
                cmd.write(&req)?;
                Ok(0)
            }
            _cmd @ SetMaster => {
                self.set_master().map(|_| 0)
            }
            _cmd @ DropMaster => {
                self.drop_master().map(|_| 0)
            }
            cmd @ ModeObjGetProperties => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                property::get_obj_properties(self, cmd)
            }
            cmd @ ModeGetProperty => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                property::get_property(self, cmd)
            }
            cmd @ ModeGetPropertyBlob => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                property::get_property_blob(self, cmd)
            }
            cmd @ ModeGetPlaneRes => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                plane::get_plane_resources(cmd)
            }
            cmd @ ModeGetPlane => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                plane::get_plane(cmd)
            }
            cmd @ ModeAtomic => {
                self.require_master()?;
                atomic::mode_atomic(self, cmd)
            }
            cmd @ ModeCreatePropertyBlob => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                property::create_property_blob(self, cmd)
            }
            cmd @ ModeDestroyPropertyBlob => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                property::destroy_property_blob(self, cmd)
            }
            cmd @ ModeAddFb2 => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                let mut req = cmd.read()?;
                req.fb_id = kms::add_fb2(self, &req)?;
                cmd.write(&req)?;
                Ok(0)
            }
            cmd @ ModePageFlip => {
                self.require_master()?;
                let req = cmd.read()?;
                if req.crtc_id != CRTC_ID {
                    return_errno_with_message!(Errno::EINVAL, "unknown crtc id");
                }
                if req.fb_id == 0 {
                    return_errno_with_message!(Errno::EINVAL, "page flip to no framebuffer");
                }
                if req.flags & !(DRM_MODE_PAGE_FLIP_EVENT | DRM_MODE_PAGE_FLIP_ASYNC) != 0 {
                    return_errno_with_message!(Errno::EINVAL, "unsupported page flip flags");
                }
                kms::present_fb(self, req.fb_id)?;
                if req.flags & DRM_MODE_PAGE_FLIP_EVENT != 0 {
                    self.queue_flip_event(req.user_data);
                }
                Ok(0)
            }
            cmd @ ModeDirtyFb => {
                self.require_master()?;
                let req = cmd.read()?;
                if req.fb_id == 0 {
                    return Ok(0);
                }
                kms::present_fb(self, req.fb_id)?;
                Ok(0)
            }
            cmd @ VirtgpuGetparam => {
                virtio_gpu::virtgpu_getparam(self, cmd)
            }
            cmd @ VirtgpuResourceCreate => {
                virtio_gpu::virtgpu_resource_create(self, cmd)
            }
            cmd @ VirtgpuResourceInfo => {
                virtio_gpu::virtgpu_resource_info(self, cmd)
            }
            cmd @ VirtgpuGetCaps => {
                virtio_gpu::virtgpu_get_caps(self, cmd)
            }
            cmd @ VirtgpuContextInit => {
                virtio_gpu::virtgpu_context_init(self, cmd)
            }
            cmd @ VirtgpuTransferToHost => {
                virtio_gpu::virtgpu_transfer_to_host(self, cmd)
            }
            cmd @ VirtgpuTransferFromHost => {
                virtio_gpu::virtgpu_transfer_from_host(self, cmd)
            }
            cmd @ VirtgpuMap => {
                virtio_gpu::virtgpu_map(self, cmd)
            }
            cmd @ VirtgpuWait => {
                virtio_gpu::virtgpu_wait(self, cmd)
            }
            _ => {
                ostd::debug!(
                    "the ioctl command {:#x} is unknown for DRM devices",
                    raw_ioctl.cmd()
                );
                return_errno_with_message!(Errno::ENOTTY, "the ioctl command is unknown");
            }
        })
    }

    fn ioctl_with_table(
        &self,
        _path: &Path,
        raw_ioctl: RawIoctl,
        file_table: &mut FileTableRefMut,
    ) -> Option<Result<i32>> {
        use ioctl::*;

        dispatch_ioctl!(match raw_ioctl {
            cmd @ VirtgpuExecbuffer => {
                virtio_gpu::virtgpu_execbuffer(self, cmd, file_table)
            }
            cmd @ PrimeHandleToFd => {
                let mut req = match cmd.read() {
                    Ok(req) => req,
                    Err(e) => return Some(Err(e)),
                };
                let (file, fd_flags) = match prime::handle_to_fd(self, req.handle, req.flags) {
                    Ok(v) => v,
                    Err(e) => return Some(Err(e)),
                };
                let fd: FileDesc = file_table.unwrap().write().insert(file, fd_flags);
                req.fd = RawFileDesc::from(fd);
                if let Err(e) = cmd.write(&req) {
                    let closed = file_table.unwrap().write().close_file(fd);
                    drop(closed);
                    return Some(Err(e));
                }
                Some(Ok(0))
            }
            cmd @ PrimeFdToHandle => {
                let req = match cmd.read() {
                    Ok(req) => req,
                    Err(e) => return Some(Err(e)),
                };
                let fd = match FileDesc::try_from(req.fd) {
                    Ok(fd) => fd,
                    Err(_) => return Some(Err(Error::new(Errno::EBADF))),
                };
                let file = match file_table.read_with(|t| t.get_file(fd).cloned()) {
                    Ok(file) => file,
                    Err(_) => return Some(Err(Error::new(Errno::EBADF))),
                };
                let dma_buf = match file.downcast_ref::<prime::DmaBufFile>() {
                    Some(f) => f,
                    None => {
                        return Some(Err(Error::with_message(
                            Errno::EINVAL,
                            "the fd is not a dma-buf",
                        )));
                    }
                };
                let (handle, _size) = match prime::fd_to_handle(self, dma_buf) {
                    Ok(v) => v,
                    Err(e) => return Some(Err(e)),
                };
                let mut resp = req;
                resp.handle = handle;
                if let Err(e) = cmd.write(&resp) {
                    prime::rollback_fd_to_handle(self, handle);
                    return Some(Err(e));
                }
                Some(Ok(0))
            }
            _ => None,
        })
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Builds a `DRM_MODE_TYPE_PREFERRED` mode for the given resolution.
fn build_mode(width: u32, height: u32) -> DrmModeModeInfo {
    let mut name = [0u8; 32];
    let name_bytes = alloc::format!("{}x{}", width, height).into_bytes();
    let n = name_bytes.len().min(name.len() - 1);
    name[..n].copy_from_slice(&name_bytes[..n]);

    DrmModeModeInfo {
        clock: width.saturating_mul(height).saturating_mul(60) / 1000,
        hdisplay: width as u16,
        hsync_start: (width + 16) as u16,
        hsync_end: (width + 32) as u16,
        htotal: (width + 48) as u16,
        hskew: 0,
        vdisplay: height as u16,
        vsync_start: (height + 1) as u16,
        vsync_end: (height + 2) as u16,
        vtotal: (height + 4) as u16,
        vscan: 0,
        vrefresh: 60,
        flags: 0,
        type_: DRM_MODE_TYPE_PREFERRED,
        name,
    }
}

/// Copies a driver string into a userspace buffer and updates the length field.
///
/// If the buffer is null (or has zero length), only the required length is
/// reported. Otherwise the string is null-terminated and truncated as needed.
fn copy_field(dst: usize, len: &mut usize, src: &str) -> Result<()> {
    let src_bytes = src.as_bytes();
    if dst != 0 && *len > 0 {
        // Match Linux's `drm_version`: copy `min(name_len, strlen + 1)` bytes,
        // i.e. the full name plus a NUL terminator when the caller's buffer has
        // room. Do not reserve a byte for the terminator up front, otherwise a
        // buffer sized exactly to the name truncates the last character
        // (e.g. "virtio_gpu" becomes "virtio_gp"), which breaks Mesa's driver
        // lookup of `virtio_gpu_dri.so`.
        let copy = src_bytes.len().min(*len);
        current_userspace!().write_bytes(dst, &src_bytes[..copy])?;
        if copy < *len {
            current_userspace!().write_val(dst + copy, &0u8)?;
        }
    }
    *len = src_bytes.len();
    Ok(())
}

// ---------------------------------------------------------------------------
// Wire types (structs matching Linux UAPI)
// ---------------------------------------------------------------------------

/// `struct drm_version`; `size_t` is 8 bytes on RISC-V.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h#L634>.
#[padding_struct]
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVersion {
    version_major: i32,
    version_minor: i32,
    version_patchlevel: i32,
    name_len: usize,
    name: usize,
    date_len: usize,
    date: usize,
    desc_len: usize,
    desc: usize,
}

/// `struct drm_get_cap` (also used for `drm_set_client_cap`, same layout).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmGetCap {
    capability: u64,
    value: u64,
}

/// `struct drm_set_client_cap`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmSetClientCap {
    capability: u64,
    value: u64,
}

/// `struct drm_gem_close`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmGemClose {
    handle: u32,
    pad: u32,
}

/// `struct drm_gem_flink`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmGemFlink {
    handle: u32,
    name: u32,
}

/// `struct drm_gem_open`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmGemOpen {
    name: u32,
    handle: u32,
    size: u64,
}

/// `struct drm_prime_handle` — argument for `DRM_IOCTL_PRIME_{HANDLE_TO_FD,FD_TO_HANDLE}`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmPrimeHandle {
    handle: u32,
    /// Only meaningful for HANDLE_TO_FD: `DRM_CLOEXEC`.
    flags: u32,
    /// Returned dmabuf fd (HANDLE_TO_FD) or input fd (FD_TO_HANDLE).
    fd: i32,
}

/// `struct drm_mode_card_res`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeCardRes {
    fb_id_ptr: u64,
    crtc_id_ptr: u64,
    connector_id_ptr: u64,
    encoder_id_ptr: u64,
    count_fbs: u32,
    count_crtcs: u32,
    count_connectors: u32,
    count_encoders: u32,
    min_width: u32,
    max_width: u32,
    min_height: u32,
    max_height: u32,
}

/// `struct drm_mode_modeinfo`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeModeInfo {
    clock: u32,
    hdisplay: u16,
    hsync_start: u16,
    hsync_end: u16,
    htotal: u16,
    hskew: u16,
    vdisplay: u16,
    vsync_start: u16,
    vsync_end: u16,
    vtotal: u16,
    vscan: u16,
    vrefresh: u32,
    flags: u32,
    type_: u32,
    name: [u8; 32],
}

/// `struct drm_mode_crtc`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeCrtc {
    set_connectors_ptr: u64,
    count_connectors: u32,
    crtc_id: u32,
    fb_id: u32,
    x: u32,
    y: u32,
    gamma_size: u32,
    mode_valid: u32,
    mode: DrmModeModeInfo,
}

/// `struct drm_mode_get_encoder`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeGetEncoder {
    encoder_id: u32,
    encoder_type: u32,
    crtc_id: u32,
    possible_crtcs: u32,
    possible_clones: u32,
}

/// `struct drm_mode_get_connector`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeGetConnector {
    encoders_ptr: u64,
    modes_ptr: u64,
    props_ptr: u64,
    prop_values_ptr: u64,
    count_modes: u32,
    count_props: u32,
    count_encoders: u32,
    encoder_id: u32,
    connector_id: u32,
    connector_type: u32,
    connector_type_id: u32,
    connection: u32,
    mm_width: u32,
    mm_height: u32,
    subpixel: u32,
    pad: u32,
}

/// `struct drm_mode_fb_cmd`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeFbCmd {
    fb_id: u32,
    width: u32,
    height: u32,
    pitch: u32,
    bpp: u32,
    depth: u32,
    handle: u32,
}

/// `struct drm_mode_create_dumb`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeCreateDumb {
    height: u32,
    width: u32,
    bpp: u32,
    flags: u32,
    handle: u32,
    pitch: u32,
    size: u64,
}

/// `struct drm_mode_map_dumb`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeMapDumb {
    handle: u32,
    pad: u32,
    offset: u64,
}

/// `struct drm_mode_destroy_dumb`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeDestroyDumb {
    handle: u32,
}

/// `struct drm_mode_obj_get_properties`.
///
/// The three `__u32` fields after two `__u64`s leave 4 bytes of implicit
/// trailing padding (the C `sizeof` is 32, not 28); model that explicitly so the
/// struct stays `Pod`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1069>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeObjGetProperties {
    props_ptr: u64,
    prop_values_ptr: u64,
    count_props: u32,
    obj_id: u32,
    obj_type: u32,
    pad: u32,
}

/// `struct drm_mode_get_property`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L963>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeGetProperty {
    values_ptr: u64,
    enum_blob_ptr: u64,
    prop_id: u32,
    flags: u32,
    name: [u8; 32],
    count_values: u32,
    count_enum_blobs: u32,
}

/// `struct drm_mode_property_enum`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L952>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModePropertyEnum {
    value: u64,
    name: [u8; 32],
}

/// `struct drm_mode_get_blob`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1084>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeGetBlob {
    blob_id: u32,
    length: u32,
    data: u64,
}

/// `struct drm_event_vblank` — the payload delivered by `read()` on the DRM
/// file for `DRM_EVENT_FLIP_COMPLETE` events.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h#L937>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmEventVblank {
    type_: u32,
    length: u32,
    user_data: u64,
    tv_sec: u32,
    tv_usec: u32,
    sequence: u32,
    crtc_id: u32,
}

/// `struct drm_mode_crtc_page_flip`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1424>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeCrtcPageFlip {
    crtc_id: u32,
    fb_id: u32,
    flags: u32,
    reserved: u32,
    user_data: u64,
}

/// `struct drm_mode_fb_dirty_cmd`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1439>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeFbDirtyCmd {
    fb_id: u32,
    flags: u32,
    color: u32,
    num_clips: u32,
    clips_ptr: u64,
}

/// `struct drm_mode_atomic`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1430>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeAtomic {
    flags: u32,
    count_props: u32,
    objs_ptr: u64,
    count_props_ptr: u64,
    props_ptr: u64,
    prop_values_ptr: u64,
    blob_id: u64,
    user_data: u64,
    reserved: u64,
    reserved_ptr: u64,
}

/// `struct drm_mode_create_blob`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1400>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeCreatePropertyBlob {
    data_ptr: u64,
    length: u32,
    blob_id: u32,
}

/// `struct drm_mode_destroy_blob`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1407>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeDestroyPropertyBlob {
    blob_id: u32,
    pad: u32,
}

/// `struct drm_mode_get_plane_res`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1120>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeGetPlaneRes {
    plane_id_ptr: u64,
    count_planes: u32,
    pad: u32,
}

/// `struct drm_mode_get_plane`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1130>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeGetPlane {
    plane_id: u32,
    crtc_id: u32,
    fb_id: u32,
    possible_crtcs: u32,
    gamma_size: u32,
    count_format_types: u32,
    format_type_ptr: u64,
}

/// `struct drm_mode_fb_cmd2`.
///
/// The `__u32` fields before `__u64 modifier[4]` leave 4 bytes of implicit
/// padding on 64-bit architectures (the C `sizeof` is 104, not 100); model
/// that explicitly so the struct stays `Pod`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L699>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeFbCmd2 {
    fb_id: u32,
    width: u32,
    height: u32,
    pixel_format: u32,
    flags: u32,
    handles: [u32; 4],
    pitches: [u32; 4],
    offsets: [u32; 4],
    pad: u32,
    modifier: [u64; 4],
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

pub(super) fn init_in_first_kthread() {
    if first_device().is_none() {
        return;
    }
    let gpu_manager = GpuManager::get_or_init();
    char::register(Arc::new(DriPrimary {
        gpu_manager: gpu_manager.clone(),
    }))
    .expect("failed to register DRM primary char device");
    char::register(Arc::new(DriRender { gpu_manager }))
        .expect("failed to register DRM render char device");
}
