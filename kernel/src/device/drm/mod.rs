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

mod dumb;
mod gem;
mod ioctl;
mod kms;

use alloc::sync::Arc;
use core::sync::atomic::{AtomicU32, Ordering};

use align_ext::AlignExt;
use aster_virtio::device::gpu::{device::GpuDevice, first_device};
use device_id::{DeviceId, MajorId, MinorId};
use ostd::mm::{Paddr, VmIo};
use ostd::sync::SpinLock;

use crate::{
    context::current_userspace,
    device::{Device, DeviceType, DevtmpfsInodeMeta, registry::char},
    events::IoEvents,
    fs::{
        file::{Mappable, PerOpenFileOps, StatusFlags},
        vfs::{inode::FileOps, path::Path},
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
    util::ioctl::{RawIoctl, dispatch_ioctl},
    vm::page_cache::{Vmo, VmoFlags, VmoOptions},
};

/// Linux DRM character-device major number.
const DRM_MAJOR: u16 = 226;

const DRIVER_NAME: &str = "virtio-gpu";
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

/// `DRM_MODE_CURSOR_BO` — set the cursor buffer (a GEM/dumb-buffer handle).
const DRM_MODE_CURSOR_BO: u32 = 0x01;
/// `DRM_MODE_CURSOR_MOVE` — reposition the cursor to (`x`, `y`).
const DRM_MODE_CURSOR_MOVE: u32 = 0x02;

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

/// Hardware cursor dimensions reported via `DRM_CAP_CURSOR_WIDTH`/`HEIGHT`.
/// 64x64 matches virtio-gpu's cursor resource limit and the X server default.
const CURSOR_SIZE: u64 = 64;

/// `DRM_CLIENT_CAP_*` values accepted by `SET_CLIENT_CAP`.
const DRM_CLIENT_CAP_STEREO_3D: u64 = 1;
const DRM_CLIENT_CAP_UNIVERSAL_PLANES: u64 = 2;
const DRM_CLIENT_CAP_ATOMIC: u64 = 3;
const DRM_CLIENT_CAP_ASPECT_RATIO: u64 = 4;
const DRM_CLIENT_CAP_WRITEBACK_CONNECTORS: u64 = 5;
const DRM_CLIENT_CAP_CURSOR_PLANE_HOTSPOT: u64 = 6;

/// Size of the single contiguous dumb-buffer pool, in bytes.
///
/// Covers framebuffers up to ~2048x2048 at 32 bpp; enough for the QEMU
/// virtio-gpu scanouts (1024x768 by default) and a generous multi-resolution
/// headroom. A single pool is required because the mmap path maps one
/// `Mappable::Vmo` per file and selects a buffer by its byte offset within it.
const DUMB_POOL_SIZE: usize = 16 * 1024 * 1024;

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
    next_gem_id: AtomicU32,
    next_gem_name: AtomicU32,
}

impl GpuManager {
    fn new(gpu: Arc<GpuDevice>) -> Self {
        Self {
            gpu,
            pool: SpinLock::new(None),
            next_offset: SpinLock::new(0),
            gem_objects: SpinLock::new(BTreeMap::new()),
            gem_names: SpinLock::new(BTreeMap::new()),
            next_gem_id: AtomicU32::new(1),
            next_gem_name: AtomicU32::new(1),
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
    pitch: u32,
    width: u32,
    height: u32,
    bpp: u32,
}

/// A registered framebuffer referencing a dumb buffer.
#[derive(Debug, Clone, Copy)]
struct Framebuffer {
    object_id: u32,
    width: u32,
    height: u32,
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
    inner: SpinLock<DriInner>,
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
}

impl DriHandle {
    fn new(
        gpu_manager: Arc<GpuManager>,
        node_type: DriNodeType,
        current_width: u32,
        current_height: u32,
    ) -> Self {
        Self {
            gpu_manager,
            node_type,
            inner: SpinLock::new(DriInner {
                handles: BTreeMap::new(),
                next_handle: 1,
                framebuffers: BTreeMap::new(),
                next_fb_id: 1,
                current_fb_id: None,
                current_width,
                current_height,
            }),
        }
    }

    /// Returns true if KMS ioctls are forbidden on this handle.
    fn is_render_node(&self) -> bool {
        matches!(self.node_type, DriNodeType::Render)
    }
}

// ---------------------------------------------------------------------------
// ioctl dispatch
// ---------------------------------------------------------------------------

impl Pollable for DriHandle {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        mask & IoEvents::OUT
    }
}

impl FileOps for DriHandle {
    fn read_at(
        &self,
        _offset: usize,
        _writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        Ok(0)
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
        let guard = self.gpu_manager.pool.lock();
        let pool = guard
            .as_ref()
            .ok_or_else(|| Error::with_message(Errno::ENODEV, "no dumb buffer has been created yet"))?;
        Ok(Mappable::Vmo(pool.clone()))
    }

    fn ioctl(&self, _path: &Path, raw_ioctl: RawIoctl) -> Result<i32> {
        use ioctl::*;

        // `RMFB` passes its argument by value, so it cannot go through the typed
        // dispatch below.
        if raw_ioctl.cmd() == MODE_RMFB_CMD {
            if self.is_render_node() {
                return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
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
                    DRM_CAP_CURSOR_WIDTH => CURSOR_SIZE,
                    DRM_CAP_CURSOR_HEIGHT => CURSOR_SIZE,
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
                let mut req = cmd.read()?;
                req.name = gem::gem_flink(self, req.handle)?;
                cmd.write(&req)?;
                Ok(0)
            }
            cmd @ GemOpen => {
                let mut req = cmd.read()?;
                let (handle, size) = gem::gem_open(self, req.name)?;
                req.handle = handle;
                req.size = size;
                cmd.write(&req)?;
                Ok(0)
            }
            cmd @ ModeGetResources => {
                if self.is_render_node() {
                    return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
                }
                let mut res = cmd.read()?;
                res.count_fbs = 0;
                res.count_crtcs = 1;
                res.count_connectors = 1;
                res.count_encoders = 1;
                res.min_width = 0;
                res.max_width = MAX_RESOLUTION;
                res.min_height = 0;
                res.max_height = MAX_RESOLUTION;
                if res.crtc_id_ptr != 0 {
                    current_userspace!().write_val(res.crtc_id_ptr as usize, &CRTC_ID)?;
                }
                if res.connector_id_ptr != 0 {
                    current_userspace!().write_val(res.connector_id_ptr as usize, &CONNECTOR_ID)?;
                }
                if res.encoder_id_ptr != 0 {
                    current_userspace!().write_val(res.encoder_id_ptr as usize, &ENCODER_ID)?;
                }
                cmd.write(&res)?;
                Ok(0)
            }
            cmd @ ModeGetConnector => {
                if self.is_render_node() {
                    return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
                }
                kms::get_connector(self, cmd)
            }
            cmd @ ModeGetEncoder => {
                if self.is_render_node() {
                    return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
                }
                kms::get_encoder(cmd)
            }
            cmd @ ModeGetCrtc => {
                if self.is_render_node() {
                    return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
                }
                kms::get_crtc(self, cmd)
            }
            cmd @ ModeSetCrtc => {
                if self.is_render_node() {
                    return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
                }
                let req = cmd.read()?;
                kms::set_crtc(self, &req)?;
                Ok(0)
            }
            cmd @ ModeCursor => {
                if self.is_render_node() {
                    return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
                }
                let req = cmd.read()?;
                kms::set_cursor(self, req.flags, req.crtc_id, req.x, req.y, req.handle, 0, 0)?;
                Ok(0)
            }
            cmd @ ModeCursor2 => {
                if self.is_render_node() {
                    return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
                }
                let req = cmd.read()?;
                kms::set_cursor(
                    self, req.flags, req.crtc_id, req.x, req.y, req.handle, req.hot_x, req.hot_y,
                )?;
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
                    return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
                }
                let mut req = cmd.read()?;
                req.fb_id = kms::add_fb(self, &req)?;
                cmd.write(&req)?;
                Ok(0)
            }
            _cmd @ SetMaster => {
                Ok(0)
            }
            _cmd @ DropMaster => {
                Ok(0)
            }
            cmd @ ModeObjGetProperties => {
                if self.is_render_node() {
                    return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
                }
                let mut props = cmd.read()?;
                props.count_props = 0;
                cmd.write(&props)?;
                Ok(0)
            }
            cmd @ ModePageFlip => {
                if self.is_render_node() {
                    return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
                }
                let req = cmd.read()?;
                if req.crtc_id != CRTC_ID {
                    return_errno_with_message!(Errno::EINVAL, "unknown crtc id");
                }
                if req.fb_id == 0 {
                    return_errno_with_message!(Errno::EINVAL, "page flip to no framebuffer");
                }
                kms::present_fb(self, req.fb_id)?;
                Ok(0)
            }
            cmd @ ModeDirtyFb => {
                if self.is_render_node() {
                    return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
                }
                let req = cmd.read()?;
                if req.fb_id == 0 {
                    return Ok(0);
                }
                kms::present_fb(self, req.fb_id)?;
                Ok(0)
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
        let copy = src_bytes.len().min(*len - 1);
        current_userspace!().write_bytes(dst, &src_bytes[..copy])?;
        current_userspace!().write_val(dst + copy, &0u8)?;
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

/// `struct drm_mode_cursor` (the legacy cursor ioctl).
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1193>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeCursor {
    flags: u32,
    crtc_id: u32,
    x: i32,
    y: i32,
    width: u32,
    height: u32,
    handle: u32,
}

/// `struct drm_mode_cursor2` (adds a hotspot to the legacy cursor ioctl).
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1205>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeCursor2 {
    flags: u32,
    crtc_id: u32,
    x: i32,
    y: i32,
    width: u32,
    height: u32,
    handle: u32,
    hot_x: i32,
    hot_y: i32,
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