// SPDX-License-Identifier: MPL-2.0

//! DRM (Direct Rendering Manager) character device support.
//!
//! Exposes a single `/dev/dri/card0` node backed by the first discovered
//! virtio-gpu device. Beyond the M1 bring-up surface (`DRM_IOCTL_VERSION`), this
//! milestone adds the KMS ioctl set a real modesetting client needs:
//!
//! - capability discovery (`GET_CAP` / `SET_CLIENT_CAP`);
//! - resource and object enumeration (`MODE_GETRESOURCES`, `MODE_GETCONNECTOR`,
//!   `MODE_GETENCODER`, `MODE_GETCRTC`);
//! - mode setting (`MODE_SETCRTC`);
//! - dumb buffers for software rendering (`MODE_CREATE_DUMB`, `MODE_MAP_DUMB`,
//!   `MODE_DESTROY_DUMB`) plus `mmap`, and framebuffer registration
//!   (`MODE_ADDFB`, `MODE_RMFB`).
//!
//! Dumb buffers are carved out of a single physically-contiguous [`Vmo`] pool
//! so that (a) `mmap` can map any buffer via the standard `Mappable::Vmo` path
//! and (b) each buffer is backed by one contiguous guest-physical span that
//! virtio-gpu's `RESOURCE_ATTACH_BACKING` accepts.

use align_ext::AlignExt;
use aster_virtio::device::gpu::{device::GpuDevice, first_device};
use device_id::{DeviceId, MajorId, MinorId};
use ostd::mm::{Paddr, VmIo};

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
    util::ioctl::{NoData, RawIoctl, dispatch_ioctl},
    vm::page_cache::{Vmo, VmoFlags, VmoOptions},
};

/// Linux DRM character-device major number.
const DRM_MAJOR: u16 = 226;

const DRIVER_NAME: &str = "virtio-gpu";
const DRIVER_DATE: &str = "20260815";
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

#[derive(Debug)]
struct Dri;

/// Per-open-file DRM state.
///
/// GEM/dumb-buffer handles and framebuffer ids are namespaced per file, matching
/// Linux's per-`drm_file` handle space. Each handle owns its own dumb-buffer
/// pool (allocated lazily on the first `CREATE_DUMB`).
struct DriHandle {
    gpu: Arc<GpuDevice>,
    inner: SpinLock<DriInner>,
}

#[derive(Debug)]
struct DriInner {
    /// The contiguous pool all dumb buffers are carved out of.
    pool: Option<Arc<Vmo>>,
    /// Bump-allocator cursor into the pool (page-aligned).
    next_offset: usize,
    dumb_buffers: BTreeMap<u32, DumbBuffer>,
    next_dumb_handle: u32,
    framebuffers: BTreeMap<u32, Framebuffer>,
    next_fb_id: u32,
    current_fb_id: Option<u32>,
    current_width: u32,
    current_height: u32,
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
    dumb_handle: u32,
    width: u32,
    height: u32,
}

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

mod ioctl_defs {
    use super::{
        DrmGetCap, DrmModeCardRes, DrmModeCreateDumb, DrmModeCrtc, DrmModeCrtcPageFlip,
        DrmModeDestroyDumb, DrmModeFbCmd, DrmModeFbDirtyCmd, DrmModeGetConnector,
        DrmModeGetEncoder, DrmModeMapDumb, DrmModeObjGetProperties, DrmSetClientCap, DrmVersion,
    };
    use crate::util::ioctl::{InData, InOutData, NoData, ioc};

    // Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h>.
    pub(super) type GetVersion = ioc!(DRM_IOCTL_VERSION, b'd', 0x00, InOutData<DrmVersion>);
    pub(super) type GetCap = ioc!(DRM_IOCTL_GET_CAP, b'd', 0x0c, InOutData<DrmGetCap>);
    pub(super) type SetClientCap = ioc!(DRM_IOCTL_SET_CLIENT_CAP, b'd', 0x0d, InData<DrmSetClientCap>);
    pub(super) type SetMaster = ioc!(DRM_IOCTL_SET_MASTER, b'd', 0x1e, NoData);
    pub(super) type DropMaster = ioc!(DRM_IOCTL_DROP_MASTER, b'd', 0x1f, NoData);

    // Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h>.
    pub(super) type ModeGetResources = ioc!(DRM_IOCTL_MODE_GETRESOURCES, b'd', 0xa0, InOutData<DrmModeCardRes>);
    pub(super) type ModeGetCrtc = ioc!(DRM_IOCTL_MODE_GETCRTC, b'd', 0xa1, InOutData<DrmModeCrtc>);
    pub(super) type ModeSetCrtc = ioc!(DRM_IOCTL_MODE_SETCRTC, b'd', 0xa2, InOutData<DrmModeCrtc>);
    pub(super) type ModeGetEncoder = ioc!(DRM_IOCTL_MODE_GETENCODER, b'd', 0xa6, InOutData<DrmModeGetEncoder>);
    pub(super) type ModeGetConnector = ioc!(DRM_IOCTL_MODE_GETCONNECTOR, b'd', 0xa7, InOutData<DrmModeGetConnector>);
    pub(super) type ModeAddFb = ioc!(DRM_IOCTL_MODE_ADDFB, b'd', 0xae, InOutData<DrmModeFbCmd>);
    pub(super) type ModePageFlip = ioc!(DRM_IOCTL_MODE_PAGE_FLIP, b'd', 0xb0, InOutData<DrmModeCrtcPageFlip>);
    pub(super) type ModeDirtyFb = ioc!(DRM_IOCTL_MODE_DIRTYFB, b'd', 0xb1, InOutData<DrmModeFbDirtyCmd>);
    pub(super) type ModeCreateDumb = ioc!(DRM_IOCTL_MODE_CREATE_DUMB, b'd', 0xb2, InOutData<DrmModeCreateDumb>);
    pub(super) type ModeMapDumb = ioc!(DRM_IOCTL_MODE_MAP_DUMB, b'd', 0xb3, InOutData<DrmModeMapDumb>);
    pub(super) type ModeDestroyDumb = ioc!(DRM_IOCTL_MODE_DESTROY_DUMB, b'd', 0xb4, InOutData<DrmModeDestroyDumb>);
    pub(super) type ModeObjGetProperties = ioc!(DRM_IOCTL_MODE_OBJ_GETPROPERTIES, b'd', 0xb9, InOutData<DrmModeObjGetProperties>);
}

/// `DRM_IOCTL_MODE_RMFB` (`_IOWR('d', 0xaf, unsigned int)`).
///
/// Unlike the other mode ioctls, `RMFB` passes its argument by value rather than
/// by pointer, so it is dispatched by raw command instead of a typed `ioc!`.
const MODE_RMFB_CMD: u32 = 0xc00464af;

impl Device for Dri {
    fn type_(&self) -> DeviceType {
        DeviceType::Char
    }

    fn id(&self) -> DeviceId {
        // Linux: major 226 (DRM), minor 0 (the first card).
        DeviceId::new(MajorId::new(DRM_MAJOR), MinorId::new(0))
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsInodeMeta<'_>> {
        Some(DevtmpfsInodeMeta::new("dri/card0"))
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        let gpu = first_device()
            .ok_or_else(|| Error::with_message(Errno::ENODEV, "no virtio-gpu device"))?;
        let current_width = gpu.width();
        let current_height = gpu.height();

        Ok(Box::new(DriHandle {
            gpu,
            inner: SpinLock::new(DriInner {
                pool: None,
                next_offset: 0,
                dumb_buffers: BTreeMap::new(),
                next_dumb_handle: 1,
                framebuffers: BTreeMap::new(),
                next_fb_id: 1,
                current_fb_id: None,
                current_width,
                current_height,
            }),
        }))
    }
}

impl DriHandle {
    /// Returns the dumb-buffer pool, allocating it on first use.
    fn ensure_pool(&self) -> Result<Arc<Vmo>> {
        let mut inner = self.inner.lock();
        if let Some(pool) = inner.pool.as_ref() {
            return Ok(pool.clone());
        }

        let pool = VmoOptions::new(DUMB_POOL_SIZE)
            .flags(VmoFlags::CONTIGUOUS)
            .alloc()?;
        inner.pool = Some(pool.clone());
        Ok(pool)
    }

    /// Base guest physical address of the pool.
    fn pool_paddr(&self, inner: &DriInner) -> Result<Paddr> {
        inner
            .pool
            .as_ref()
            .and_then(|pool| pool.paddr())
            .ok_or_else(|| Error::with_message(Errno::ENOMEM, "dumb buffer pool has no memory"))
    }

    fn create_dumb(&self, req: &DrmModeCreateDumb) -> Result<DrmModeCreateDumb> {
        if req.flags != 0 {
            return_errno_with_message!(Errno::EINVAL, "unsupported dumb buffer flags");
        }
        let bytes_per_pixel = (req.bpp + 7) / 8;
        let pitch = req
            .width
            .checked_mul(bytes_per_pixel)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "dumb buffer width overflows"))?;
        let size = (pitch as usize)
            .checked_mul(req.height as usize)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "dumb buffer size overflows"))?;
        if size == 0 {
            return_errno_with_message!(Errno::EINVAL, "dumb buffer has zero size");
        }

        self.ensure_pool()?;

        let mut inner = self.inner.lock();
        let offset = inner.next_offset.align_up(PAGE_SIZE);
        let end = offset
            .checked_add(size)
            .ok_or_else(|| Error::with_message(Errno::ENOMEM, "dumb buffer size overflows"))?;
        if end > DUMB_POOL_SIZE {
            return_errno_with_message!(Errno::ENOMEM, "dumb buffer pool is exhausted");
        }

        let handle = inner.next_dumb_handle;
        inner.next_dumb_handle += 1;
        inner.dumb_buffers.insert(
            handle,
            DumbBuffer {
                offset,
                size,
                pitch,
                width: req.width,
                height: req.height,
                bpp: req.bpp,
            },
        );
        inner.next_offset = end.align_up(PAGE_SIZE);

        Ok(DrmModeCreateDumb {
            handle,
            pitch,
            size: size as u64,
            ..*req
        })
    }

    fn map_dumb(&self, req: &DrmModeMapDumb) -> Result<DrmModeMapDumb> {
        let inner = self.inner.lock();
        let dumb = inner
            .dumb_buffers
            .get(&req.handle)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown dumb buffer handle"))?;
        Ok(DrmModeMapDumb {
            offset: dumb.offset as u64,
            ..*req
        })
    }

    fn destroy_dumb(&self, req: &DrmModeDestroyDumb) -> Result<()> {
        let mut inner = self.inner.lock();
        if inner.dumb_buffers.remove(&req.handle).is_none() {
            return_errno_with_message!(Errno::EINVAL, "unknown dumb buffer handle");
        }
        // The freed pool space is intentionally not reclaimed: the pool is a
        // bump allocator, so a destroyed buffer's span is simply leaked within
        // the pool. Fine for the handful of buffers a client allocates.
        Ok(())
    }

    fn add_fb(&self, req: &DrmModeFbCmd) -> Result<u32> {
        let mut inner = self.inner.lock();
        let dumb = inner
            .dumb_buffers
            .get(&req.handle)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown dumb buffer handle"))?;
        if req.width != dumb.width
            || req.height != dumb.height
            || req.pitch != dumb.pitch
            || req.bpp != dumb.bpp
        {
            return_errno_with_message!(Errno::EINVAL, "framebuffer does not match dumb buffer");
        }
        let fb_id = inner.next_fb_id;
        inner.next_fb_id += 1;
        inner.framebuffers.insert(
            fb_id,
            Framebuffer {
                dumb_handle: req.handle,
                width: req.width,
                height: req.height,
            },
        );
        Ok(fb_id)
    }

    fn rm_fb(&self, fb_id: u32) -> Result<()> {
        let mut inner = self.inner.lock();
        if inner.framebuffers.remove(&fb_id).is_none() {
            return_errno_with_message!(Errno::EINVAL, "unknown framebuffer id");
        }
        if inner.current_fb_id == Some(fb_id) {
            inner.current_fb_id = None;
        }
        Ok(())
    }

    fn set_crtc(&self, req: &DrmModeCrtc) -> Result<()> {
        if req.crtc_id != CRTC_ID {
            return_errno_with_message!(Errno::EINVAL, "unknown crtc id");
        }

        // fb_id == 0 disables the CRTC; we keep the current scanout.
        if req.fb_id == 0 {
            return Ok(());
        }

        self.present_fb(req.fb_id)
    }

    /// Presents a framebuffer on the scanout, copying its pixels to the host.
    ///
    /// Shared by `MODE_SETCRTC`, `MODE_PAGE_FLIP`, and `MODE_DIRTYFB`: all three
    /// ultimately make a framebuffer visible, and virtio-gpu only pulls fresh
    /// pixels during `TRANSFER_TO_HOST_2D` + `FLUSH`, so every present must
    /// re-run that transfer (a guest-side mmap write alone is never seen by the
    /// host display).
    fn present_fb(&self, fb_id: u32) -> Result<()> {
        let (addr, size, width, height) = {
            let inner = self.inner.lock();
            let fb = inner
                .framebuffers
                .get(&fb_id)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown framebuffer id"))?;
            let dumb = inner
                .dumb_buffers
                .get(&fb.dumb_handle)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown dumb buffer handle"))?;
            let base = self.pool_paddr(&inner)?;
            (base + dumb.offset, dumb.size, fb.width, fb.height)
        };

        self.gpu
            .present_framebuffer(addr as u64, size as u32, width, height)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu present failed"))?;

        let mut inner = self.inner.lock();
        inner.current_fb_id = Some(fb_id);
        inner.current_width = width;
        inner.current_height = height;
        Ok(())
    }

    fn get_crtc(&self, req: &DrmModeCrtc) -> Result<DrmModeCrtc> {
        if req.crtc_id != CRTC_ID {
            return_errno_with_message!(Errno::EINVAL, "unknown crtc id");
        }
        let inner = self.inner.lock();
        Ok(DrmModeCrtc {
            crtc_id: CRTC_ID,
            fb_id: inner.current_fb_id.unwrap_or(0),
            mode_valid: 1,
            mode: build_mode(inner.current_width, inner.current_height),
            ..Default::default()
        })
    }
}

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
        let inner = self.inner.lock();
        let pool = inner.pool.as_ref().ok_or_else(|| {
            Error::with_message(Errno::ENODEV, "no dumb buffer has been created yet")
        })?;
        Ok(Mappable::Vmo(pool.clone()))
    }

    fn ioctl(&self, _path: &Path, raw_ioctl: RawIoctl) -> Result<i32> {
        use ioctl_defs::*;

        // `RMFB` passes its argument by value, so it cannot go through the typed
        // dispatch below.
        if raw_ioctl.cmd() == MODE_RMFB_CMD {
            self.rm_fb(raw_ioctl.arg() as u32)?;
            return Ok(0);
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
                    _ => {
                        return_errno_with_message!(Errno::EINVAL, "unsupported DRM capability")
                    }
                };
                cmd.write(&cap)?;
                Ok(0)
            }
            cmd @ SetClientCap => {
                let cap = cmd.read()?;
                // Accept the client caps a modesetting client enables and ignore
                // the on/off value; the corresponding features are simply absent.
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
            cmd @ ModeGetResources => {
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
                let mut conn = cmd.read()?;
                if conn.connector_id != CONNECTOR_ID {
                    return_errno_with_message!(Errno::EINVAL, "unknown connector id");
                }
                let capacity = conn.count_modes;
                conn.count_modes = 1;
                conn.count_props = 0;
                conn.count_encoders = 1;
                conn.encoder_id = ENCODER_ID;
                conn.connector_type = DRM_MODE_CONNECTOR_VIRTUAL;
                conn.connector_type_id = 1;
                conn.connection = DRM_MODE_CONNECTED;
                conn.mm_width = 0;
                conn.mm_height = 0;
                conn.subpixel = 0;
                conn.pad = 0;
                if conn.modes_ptr != 0 && capacity >= 1 {
                    let mode = build_mode(self.gpu.width(), self.gpu.height());
                    current_userspace!().write_val(conn.modes_ptr as usize, &mode)?;
                }
                if conn.encoders_ptr != 0 {
                    current_userspace!().write_val(conn.encoders_ptr as usize, &ENCODER_ID)?;
                }
                cmd.write(&conn)?;
                Ok(0)
            }
            cmd @ ModeGetEncoder => {
                let mut enc = cmd.read()?;
                if enc.encoder_id != ENCODER_ID {
                    return_errno_with_message!(Errno::EINVAL, "unknown encoder id");
                }
                enc.encoder_type = DRM_MODE_ENCODER_VIRTUAL;
                enc.crtc_id = CRTC_ID;
                enc.possible_crtcs = 1;
                enc.possible_clones = 0;
                cmd.write(&enc)?;
                Ok(0)
            }
            cmd @ ModeGetCrtc => {
                let req = cmd.read()?;
                cmd.write(&self.get_crtc(&req)?)?;
                Ok(0)
            }
            cmd @ ModeSetCrtc => {
                let req = cmd.read()?;
                self.set_crtc(&req)?;
                Ok(0)
            }
            cmd @ ModeCreateDumb => {
                let req = cmd.read()?;
                cmd.write(&self.create_dumb(&req)?)?;
                Ok(0)
            }
            cmd @ ModeMapDumb => {
                let req = cmd.read()?;
                cmd.write(&self.map_dumb(&req)?)?;
                Ok(0)
            }
            cmd @ ModeDestroyDumb => {
                let req = cmd.read()?;
                self.destroy_dumb(&req)?;
                Ok(0)
            }
            cmd @ ModeAddFb => {
                let mut req = cmd.read()?;
                req.fb_id = self.add_fb(&req)?;
                cmd.write(&req)?;
                Ok(0)
            }
            _cmd @ SetMaster => {
                // We always grant DRM master to the first opener. There is no
                // legacy DRI authentication to gate, so the only observable
                // effect of master is that `SET_MASTER` succeeds, which the
                // modesetting driver requires at startup.
                Ok(0)
            }
            _cmd @ DropMaster => {
                Ok(0)
            }
            cmd @ ModeObjGetProperties => {
                let mut props = cmd.read()?;
                // We advertise no KMS properties. The modesetting driver probes
                // them to decide whether to use atomic/gamma/CTM paths; an empty
                // set is valid and keeps it on the plain `SETCRTC`/`DIRTYFB`
                // path. Return the count, leaving the (zero-length) arrays alone.
                props.count_props = 0;
                cmd.write(&props)?;
                Ok(0)
            }
            cmd @ ModePageFlip => {
                let req = cmd.read()?;
                if req.crtc_id != CRTC_ID {
                    return_errno_with_message!(Errno::EINVAL, "unknown crtc id");
                }
                if req.fb_id == 0 {
                    return_errno_with_message!(Errno::EINVAL, "page flip to no framebuffer");
                }
                self.present_fb(req.fb_id)?;
                Ok(0)
            }
            cmd @ ModeDirtyFb => {
                let req = cmd.read()?;
                // The dirty clip rects are ignored: the framebuffer's pixels are
                // already in guest memory, so we re-present the whole buffer to
                // push the latest content to the host.
                //
                // `fb_id == 0` is the modesetting driver's *capability probe*
                // (it calls `drmModeDirtyFB(fd, fb_id, NULL, 0)` before the first
                // framebuffer exists). Returning success there keeps it on the
                // dirty-update path; the real presents carry a valid id.
                if req.fb_id == 0 {
                    return Ok(0);
                }
                self.present_fb(req.fb_id)?;
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

pub(super) fn init_in_first_kthread() {
    if first_device().is_none() {
        return;
    }

    char::register(Arc::new(Dri)).expect("failed to register DRM char device");
}
