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
//!   (`MODE_ADDFB`, `MODE_RMFB`);
//! - legacy hardware-cursor set, move, and hide operations.
//!
//! Dumb buffers are carved out of a single physically-contiguous [`Vmo`] pool
//! so that (a) `mmap` can map any buffer via the standard `Mappable::Vmo` path
//! and (b) each buffer is backed by one contiguous guest-physical span that
//! virtio-gpu's `RESOURCE_ATTACH_BACKING` accepts.

mod cursor;

use align_ext::AlignExt;
use aster_virtio::device::gpu::{device::GpuDevice, first_device};
use device_id::{DeviceId, MajorId, MinorId};
use ostd::mm::{Paddr, VmIo};

use self::cursor::{
    CursorBuffer, CursorImage, CursorState, DrmModeCursor, DrmModeCursor2, MODE_CURSOR_BO,
    validate_cursor,
};
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

/// Which node an open file reached the device through.
///
/// Linux exposes one GPU twice: `card0` for clients that modeset, and
/// `renderD128` for clients that only render. The two differ in what they
/// permit, not in what they reach — both resolve handles to the same
/// device-wide GEM objects.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DriNode {
    Card,
    Render,
}

impl DriNode {
    /// The minor number Linux assigns this node.
    const fn minor(self) -> u32 {
        match self {
            DriNode::Card => 0,
            DriNode::Render => 128,
        }
    }

    const fn devtmpfs_name(self) -> &'static str {
        match self {
            DriNode::Card => "dri/card0",
            DriNode::Render => "dri/renderD128",
        }
    }
}

/// A GEM object: a page-aligned span of the device-wide buffer pool.
#[derive(Debug, Clone, Copy)]
struct GemObject {
    offset: usize,
    size: usize,
    pitch: u32,
    width: u32,
    height: u32,
    bpp: u32,
    /// Open handles naming this object, counted across every file. The object
    /// is dropped when the last one goes away.
    refs: u32,
}

/// The device-wide GEM object space, shared by every open file.
///
/// Objects cannot belong to a single file: `GEM_FLINK` gives one a name so
/// that another file can open it by name, and the render node shares buffers
/// with the card node. Handles stay per-file — each open file maps its own
/// handle numbers onto these objects.
#[derive(Debug)]
struct GemObjects {
    /// The contiguous pool every object is carved out of.
    pool: Option<Arc<Vmo>>,
    /// Bump-allocator cursor into the pool (page-aligned).
    next_offset: usize,
    objects: BTreeMap<u32, GemObject>,
    next_object_id: u32,
    /// Names created by `GEM_FLINK`, each mapping to the object it names.
    names: BTreeMap<u32, u32>,
    next_name: u32,
}

impl GemObjects {
    const fn new() -> Self {
        Self {
            pool: None,
            next_offset: 0,
            objects: BTreeMap::new(),
            next_object_id: 1,
            names: BTreeMap::new(),
            next_name: 1,
        }
    }
}

/// The one object space. Lock ordering: take a file's `DriInner` first, then
/// this, never the other way round.
static GEM_OBJECTS: SpinLock<GemObjects> = SpinLock::new(GemObjects::new());

#[derive(Debug)]
struct Dri {
    node: DriNode,
}

/// Per-open-file DRM state.
///
/// Handles and framebuffer ids are namespaced per file, matching Linux's
/// per-`drm_file` handle space. The objects those handles name live in the
/// device-wide [`GEM_OBJECTS`] space instead, so a buffer can outlive the file
/// that created it and be reached from another one.
struct DriHandle {
    /// The node this file was opened through, which decides what it may do.
    node: DriNode,
    gpu: Arc<GpuDevice>,
    cursor_operation: Mutex<()>,
    inner: SpinLock<DriInner>,
}

#[derive(Debug)]
struct DriInner {
    /// This file's handles, each naming a device-wide GEM object.
    handles: BTreeMap<u32, u32>,
    next_handle: u32,
    framebuffers: BTreeMap<u32, Framebuffer>,
    next_fb_id: u32,
    current_fb_id: Option<u32>,
    current_width: u32,
    current_height: u32,
    cursor: CursorState,
}

/// A registered framebuffer referencing a GEM object.
#[derive(Debug, Clone, Copy)]
struct Framebuffer {
    object_id: u32,
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

/// `struct drm_virtgpu_getparam`.
///
/// `value` is a userspace pointer the kernel writes one `u64` to, matching
/// Linux's `copy_to_user` of the answer.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVirtgpuGetparam {
    param: u64,
    value: u64,
}

/// `VIRTGPU_PARAM_*` query ids (include/uapi/drm/virtgpu_drm.h).
const VIRTGPU_PARAM_3D_FEATURES: u64 = 1;
const VIRTGPU_PARAM_CAPSET_QUERY_FIX: u64 = 2;
const VIRTGPU_PARAM_SUPPORTED_CAPSET_IDS: u64 = 7;

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
        DrmModeCursor, DrmModeCursor2, DrmModeDestroyDumb, DrmModeFbCmd, DrmModeFbDirtyCmd,
        DrmGemClose, DrmGemFlink, DrmGemOpen, DrmModeGetConnector, DrmModeGetEncoder,
        DrmVirtgpuGetparam,
        DrmModeMapDumb, DrmModeObjGetProperties, DrmSetClientCap, DrmVersion,
    };
    use crate::util::ioctl::{InData, InOutData, NoData, ioc};

    // Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h>.
    pub(super) type GetVersion = ioc!(DRM_IOCTL_VERSION, b'd', 0x00, InOutData<DrmVersion>);
    pub(super) type GetCap = ioc!(DRM_IOCTL_GET_CAP, b'd', 0x0c, InOutData<DrmGetCap>);
    pub(super) type SetClientCap = ioc!(DRM_IOCTL_SET_CLIENT_CAP, b'd', 0x0d, InData<DrmSetClientCap>);
    // Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h>.
    pub(super) type GemClose = ioc!(DRM_IOCTL_GEM_CLOSE, b'd', 0x09, InData<DrmGemClose>);
    pub(super) type GemFlink = ioc!(DRM_IOCTL_GEM_FLINK, b'd', 0x0a, InOutData<DrmGemFlink>);
    pub(super) type GemOpen = ioc!(DRM_IOCTL_GEM_OPEN, b'd', 0x1b, InOutData<DrmGemOpen>);
    // The virtgpu ioctls live at `DRM_COMMAND_BASE` (0x40) plus their number.
    pub(super) type VirtgpuGetparam = ioc!(DRM_IOCTL_VIRTGPU_GETPARAM, b'd', 0x43, InOutData<DrmVirtgpuGetparam>);
    pub(super) type SetMaster = ioc!(DRM_IOCTL_SET_MASTER, b'd', 0x1e, NoData);
    pub(super) type DropMaster = ioc!(DRM_IOCTL_DROP_MASTER, b'd', 0x1f, NoData);

    // Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h>.
    pub(super) type ModeGetResources = ioc!(DRM_IOCTL_MODE_GETRESOURCES, b'd', 0xa0, InOutData<DrmModeCardRes>);
    pub(super) type ModeGetCrtc = ioc!(DRM_IOCTL_MODE_GETCRTC, b'd', 0xa1, InOutData<DrmModeCrtc>);
    pub(super) type ModeSetCrtc = ioc!(DRM_IOCTL_MODE_SETCRTC, b'd', 0xa2, InOutData<DrmModeCrtc>);
    pub(super) type ModeCursor = ioc!(DRM_IOCTL_MODE_CURSOR, b'd', 0xa3, InOutData<DrmModeCursor>);
    pub(super) type ModeGetEncoder = ioc!(DRM_IOCTL_MODE_GETENCODER, b'd', 0xa6, InOutData<DrmModeGetEncoder>);
    pub(super) type ModeGetConnector = ioc!(DRM_IOCTL_MODE_GETCONNECTOR, b'd', 0xa7, InOutData<DrmModeGetConnector>);
    pub(super) type ModeAddFb = ioc!(DRM_IOCTL_MODE_ADDFB, b'd', 0xae, InOutData<DrmModeFbCmd>);
    pub(super) type ModePageFlip = ioc!(DRM_IOCTL_MODE_PAGE_FLIP, b'd', 0xb0, InOutData<DrmModeCrtcPageFlip>);
    pub(super) type ModeDirtyFb = ioc!(DRM_IOCTL_MODE_DIRTYFB, b'd', 0xb1, InOutData<DrmModeFbDirtyCmd>);
    pub(super) type ModeCreateDumb = ioc!(DRM_IOCTL_MODE_CREATE_DUMB, b'd', 0xb2, InOutData<DrmModeCreateDumb>);
    pub(super) type ModeMapDumb = ioc!(DRM_IOCTL_MODE_MAP_DUMB, b'd', 0xb3, InOutData<DrmModeMapDumb>);
    pub(super) type ModeDestroyDumb = ioc!(DRM_IOCTL_MODE_DESTROY_DUMB, b'd', 0xb4, InOutData<DrmModeDestroyDumb>);
    pub(super) type ModeObjGetProperties = ioc!(DRM_IOCTL_MODE_OBJ_GETPROPERTIES, b'd', 0xb9, InOutData<DrmModeObjGetProperties>);
    pub(super) type ModeCursor2 = ioc!(DRM_IOCTL_MODE_CURSOR2, b'd', 0xbb, InOutData<DrmModeCursor2>);
}

/// `DRM_IOCTL_MODE_RMFB` (`_IOWR('d', 0xaf, unsigned int)`).
///
/// Unlike the other mode ioctls, `RMFB` passes its argument by value rather than
/// by pointer, so it is dispatched by raw command instead of a typed `ioc!`.
const MODE_RMFB_CMD: u32 = 0xc00464af;

/// Whether an ioctl is reachable through the render node.
///
/// Mirrors the `DRM_RENDER_ALLOW` entries of Linux's `drm_ioctls[]`. A render
/// node exists for clients that only render, so it withholds everything that
/// acts on the display — modesetting, cursor, page flips — along with master.
///
/// `GEM_FLINK` and `GEM_OPEN` are withheld too, which is less obvious: Linux
/// marks them `DRM_AUTH` *without* `DRM_RENDER_ALLOW`, because a name is how a
/// legacy client hands a buffer to another file, and the render node serves
/// clients that have no use for that. Buffers reach a render client through
/// PRIME instead.
///
/// Comparing the typed ioctl rather than a raw command number keeps this table
/// from drifting away from the definitions above.
fn is_render_allowed(raw_ioctl: RawIoctl) -> bool {
    use ioctl_defs::*;

    GetVersion::try_from_raw(raw_ioctl).is_some()
        || GetCap::try_from_raw(raw_ioctl).is_some()
        || GemClose::try_from_raw(raw_ioctl).is_some()
        // A 3D client reaches the device through the render node, so the
        // parameter query it runs before anything else has to get through.
        || VirtgpuGetparam::try_from_raw(raw_ioctl).is_some()
}

impl Device for Dri {
    fn type_(&self) -> DeviceType {
        DeviceType::Char
    }

    fn id(&self) -> DeviceId {
        // Linux: major 226 (DRM), minor 0 for the first card and 128 for the
        // first render node.
        DeviceId::new(MajorId::new(DRM_MAJOR), MinorId::new(self.node.minor()))
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsInodeMeta<'_>> {
        Some(DevtmpfsInodeMeta::new(self.node.devtmpfs_name()))
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        let gpu = first_device()
            .ok_or_else(|| Error::with_message(Errno::ENODEV, "no virtio-gpu device"))?;
        let current_width = gpu.width();
        let current_height = gpu.height();

        Ok(Box::new(DriHandle {
            node: self.node,
            gpu,
            cursor_operation: Mutex::new(()),
            inner: SpinLock::new(DriInner {
                handles: BTreeMap::new(),
                next_handle: 1,
                framebuffers: BTreeMap::new(),
                next_fb_id: 1,
                current_fb_id: None,
                current_width,
                current_height,
                cursor: CursorState::default(),
            }),
        }))
    }
}

/// Returns the device-wide buffer pool, allocating it on first use.
///
/// The pool outlives any single file: buffers created through one open file
/// must stay valid when another file reaches them by name.
fn ensure_pool(objects: &mut GemObjects) -> Result<Arc<Vmo>> {
    if let Some(pool) = objects.pool.as_ref() {
        return Ok(pool.clone());
    }
    let pool = VmoOptions::new(DUMB_POOL_SIZE)
        .flags(VmoFlags::CONTIGUOUS)
        .alloc()?;
    objects.pool = Some(pool.clone());
    Ok(pool)
}

/// Base guest physical address of the device-wide pool.
fn pool_paddr(objects: &GemObjects) -> Result<Paddr> {
    objects
        .pool
        .as_ref()
        .and_then(|pool| pool.paddr())
        .ok_or_else(|| Error::with_message(Errno::ENOMEM, "dumb buffer pool has no memory"))
}

/// Returns the object a handle in this file names.
fn object_for_handle(inner: &DriInner, handle: u32) -> Result<u32> {
    inner
        .handles
        .get(&handle)
        .copied()
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown buffer handle"))
}

/// Drops one reference to an object, freeing it when the last one goes.
///
/// The pool space is deliberately not reclaimed: the pool is a bump allocator,
/// so a freed buffer's span is simply leaked within it. Fine for the handful of
/// buffers a client allocates.
fn release_object(object_id: u32) {
    let mut objects = GEM_OBJECTS.lock();
    let Some(object) = objects.objects.get_mut(&object_id) else {
        return;
    };
    object.refs = object.refs.saturating_sub(1);
    if object.refs == 0 {
        objects.objects.remove(&object_id);
        // A name is only a handle on an object that still exists.
        objects.names.retain(|_, named| *named != object_id);
    }
}

/// Returns a copy of a GEM object by its device-wide id.
fn object_by_id(objects: &GemObjects, object_id: u32) -> Result<GemObject> {
    objects
        .objects
        .get(&object_id)
        .copied()
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM object"))
}

impl DriHandle {

    fn create_dumb(&self, req: &DrmModeCreateDumb) -> Result<DrmModeCreateDumb> {
        if req.flags != 0 {
            return_errno_with_message!(Errno::EINVAL, "unsupported dumb buffer flags");
        }
        let bytes_per_pixel = req.bpp.div_ceil(8);
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

        // Take the file's lock before the device-wide one, as `GEM_OBJECTS`
        // documents; the reverse order would deadlock against `gem_flink`.
        let mut inner = self.inner.lock();
        let mut objects = GEM_OBJECTS.lock();
        ensure_pool(&mut objects)?;
        let offset = objects.next_offset.align_up(PAGE_SIZE);
        let end = offset
            .checked_add(size)
            .ok_or_else(|| Error::with_message(Errno::ENOMEM, "dumb buffer size overflows"))?;
        if end > DUMB_POOL_SIZE {
            return_errno_with_message!(Errno::ENOMEM, "dumb buffer pool is exhausted");
        }

        let object_id = objects.next_object_id;
        objects.next_object_id += 1;
        objects.objects.insert(
            object_id,
            GemObject {
                offset,
                size,
                pitch,
                width: req.width,
                height: req.height,
                bpp: req.bpp,
                refs: 1,
            },
        );
        objects.next_offset = end.align_up(PAGE_SIZE);

        let handle = inner.next_handle;
        inner.next_handle += 1;
        inner.handles.insert(handle, object_id);

        Ok(DrmModeCreateDumb {
            handle,
            pitch,
            size: size as u64,
            ..*req
        })
    }

    /// Drops this file's handle on an object.
    fn gem_close(&self, req: &DrmGemClose) -> Result<()> {
        let mut inner = self.inner.lock();
        if inner.cursor.uses_handle(req.handle) {
            return_errno_with_message!(Errno::EBUSY, "buffer is active as the cursor");
        }
        let Some(object_id) = inner.handles.remove(&req.handle) else {
            return_errno_with_message!(Errno::EINVAL, "unknown GEM handle");
        };
        drop(inner);
        release_object(object_id);
        Ok(())
    }

    /// Answers a `VIRTGPU_GETPARAM` query into the caller's `value` pointer.
    ///
    /// This is how a 3D client asks whether it is worth opening a render path
    /// at all: Mesa calls it before anything else and falls back to software
    /// rendering when the answer says the host offers no 3D.
    fn virtgpu_getparam(&self, req: &DrmVirtgpuGetparam) -> Result<()> {
        let value = match req.param {
            VIRTGPU_PARAM_3D_FEATURES => u64::from(self.gpu.supports_virgl()),
            // The query-fix flag means the driver returns the capset the real
            // driver would, rather than a fixed stub.
            VIRTGPU_PARAM_CAPSET_QUERY_FIX => u64::from(self.gpu.supports_virgl()),
            VIRTGPU_PARAM_SUPPORTED_CAPSET_IDS => {
                if !self.gpu.supports_virgl() {
                    0
                } else {
                    // Read the host's first capset rather than assuming virgl,
                    // so the mask describes this host and not our expectation.
                    let info = self
                        .gpu
                        .capset_info(0)
                        .map_err(|_| Error::with_message(Errno::EIO, "capset query failed"))?;
                    1u64 << info.id
                }
            }
            _ => return_errno_with_message!(Errno::EINVAL, "unknown virtgpu parameter"),
        };

        current_userspace!()
            .write_val(req.value as usize, &value)
            .map_err(|_| Error::with_message(Errno::EFAULT, "bad virtgpu parameter pointer"))?;
        Ok(())
    }

    /// Names an object so another file can open it.
    fn gem_flink(&self, req: &DrmGemFlink) -> Result<DrmGemFlink> {
        let inner = self.inner.lock();
        let object_id = object_for_handle(&inner, req.handle)?;
        let mut objects = GEM_OBJECTS.lock();
        // An object already carrying a name keeps it, as in Linux: repeated
        // flinks of the same handle observe the same name.
        if let Some((name, _)) = objects.names.iter().find(|(_, named)| **named == object_id) {
            return Ok(DrmGemFlink {
                handle: req.handle,
                name: *name,
            });
        }
        let name = objects.next_name;
        objects.next_name = objects.next_name.saturating_add(1);
        objects.names.insert(name, object_id);
        Ok(DrmGemFlink {
            handle: req.handle,
            name,
        })
    }

    /// Takes a handle on a named object, which may have been created by
    /// another file.
    fn gem_open(&self, req: &DrmGemOpen) -> Result<DrmGemOpen> {
        let mut inner = self.inner.lock();
        let mut objects = GEM_OBJECTS.lock();
        let object_id = objects
            .names
            .get(&req.name)
            .copied()
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM name"))?;
        let object = objects
            .objects
            .get_mut(&object_id)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM object"))?;
        object.refs = object.refs.saturating_add(1);
        let size = object.size;
        let handle = inner.next_handle;
        inner.next_handle = inner.next_handle.saturating_add(1);
        inner.handles.insert(handle, object_id);
        Ok(DrmGemOpen {
            name: req.name,
            handle,
            size: size as u64,
        })
    }

    fn map_dumb(&self, req: &DrmModeMapDumb) -> Result<DrmModeMapDumb> {
        let object_id = object_for_handle(&self.inner.lock(), req.handle)?;
        let object = object_by_id(&GEM_OBJECTS.lock(), object_id)?;
        Ok(DrmModeMapDumb {
            offset: object.offset as u64,
            ..*req
        })
    }

    fn destroy_dumb(&self, req: &DrmModeDestroyDumb) -> Result<()> {
        let _cursor_operation = self.cursor_operation.lock();
        let mut inner = self.inner.lock();
        if inner.cursor.uses_handle(req.handle) {
            return_errno_with_message!(Errno::EBUSY, "dumb buffer is active as the cursor");
        }
        let Some(object_id) = inner.handles.remove(&req.handle) else {
            return_errno_with_message!(Errno::EINVAL, "unknown dumb buffer handle");
        };
        drop(inner);
        release_object(object_id);
        Ok(())
    }

    fn add_fb(&self, req: &DrmModeFbCmd) -> Result<u32> {
        let mut inner = self.inner.lock();
        let object_id = object_for_handle(&inner, req.handle)?;
        let object = object_by_id(&GEM_OBJECTS.lock(), object_id)?;
        if req.width != object.width
            || req.height != object.height
            || req.pitch != object.pitch
            || req.bpp != object.bpp
        {
            return_errno_with_message!(Errno::EINVAL, "framebuffer does not match dumb buffer");
        }
        let fb_id = inner.next_fb_id;
        inner.next_fb_id += 1;
        inner.framebuffers.insert(
            fb_id,
            Framebuffer {
                object_id,
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
            let objects = GEM_OBJECTS.lock();
            let object = object_by_id(&objects, fb.object_id)?;
            let base = pool_paddr(&objects)?;
            (base + object.offset, object.size, fb.width, fb.height)
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

    fn update_cursor(&self, request: DrmModeCursor2) -> Result<()> {
        let _cursor_operation = self.cursor_operation.lock();
        let (update, position, backing) = {
            let inner = self.inner.lock();
            let buffer = if request.flags & MODE_CURSOR_BO != 0 && request.handle != 0 {
                object_for_handle(&inner, request.handle)
                    .ok()
                    .and_then(|object_id| object_by_id(&GEM_OBJECTS.lock(), object_id).ok())
                    .map(|object| CursorBuffer {
                        width: object.width,
                        height: object.height,
                        pitch: object.pitch,
                        bpp: object.bpp,
                        size: object.size,
                    })
            } else {
                None
            };
            let update = validate_cursor(request, buffer, CRTC_ID)
                .map_err(|_| Error::with_message(Errno::EINVAL, "invalid cursor request"))?;
            let position = inner.cursor.position_for(update);
            let backing = match update.image {
                Some(CursorImage::Buffer { handle, .. }) => {
                    let object_id = object_for_handle(&inner, handle)?;
                    let objects = GEM_OBJECTS.lock();
                    let object = object_by_id(&objects, object_id)?;
                    let base = pool_paddr(&objects)?;
                    Some((
                        (base + object.offset) as u64,
                        u32::try_from(object.size).map_err(|_| {
                            Error::with_message(Errno::EINVAL, "cursor buffer is too large")
                        })?,
                    ))
                }
                _ => None,
            };
            (update, position, backing)
        };

        let resource_id = match update.image {
            Some(CursorImage::Buffer {
                width,
                height,
                hot_x,
                hot_y,
                ..
            }) => {
                let (addr, size) = backing.ok_or_else(|| {
                    Error::with_message(Errno::EINVAL, "cursor buffer has no backing")
                })?;
                Some(
                    self.gpu
                        .update_cursor(
                            addr, size, width, height, hot_x, hot_y, position.x, position.y,
                        )
                        .map_err(|_| {
                            Error::with_message(Errno::EIO, "virtio-gpu cursor update failed")
                        })?,
                )
            }
            Some(CursorImage::Hide) => {
                self.gpu.hide_cursor(position.x, position.y).map_err(|_| {
                    Error::with_message(Errno::EIO, "virtio-gpu cursor hide failed")
                })?;
                None
            }
            None => {
                self.gpu.move_cursor(position.x, position.y).map_err(|_| {
                    Error::with_message(Errno::EIO, "virtio-gpu cursor move failed")
                })?;
                None
            }
        };

        self.inner.lock().cursor.commit(update, resource_id);
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

impl Drop for DriHandle {
    fn drop(&mut self) {
        let _cursor_operation = self.cursor_operation.lock();
        let (resource_id, position) = {
            let inner = self.inner.lock();
            (inner.cursor.resource_id, inner.cursor.position)
        };
        if let Some(resource_id) = resource_id {
            let _ = self.gpu.clear_cursor(resource_id, position.x, position.y);
        }
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
        let objects = GEM_OBJECTS.lock();
        let pool = objects.pool.as_ref().ok_or_else(|| {
            Error::with_message(Errno::ENODEV, "no dumb buffer has been created yet")
        })?;
        Ok(Mappable::Vmo(pool.clone()))
    }

    fn ioctl(&self, _path: &Path, raw_ioctl: RawIoctl) -> Result<i32> {
        use ioctl_defs::*;

        // A render node withholds everything that acts on the display, so the
        // check comes before the request is decoded: a refused ioctl must not
        // be able to reach the handlers at all.
        if self.node == DriNode::Render && !is_render_allowed(raw_ioctl) {
            return_errno_with_message!(
                Errno::EACCES,
                "ioctl is not permitted on the render node"
            );
        }

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
            cmd @ ModeCursor => {
                let req = cmd.read()?;
                self.update_cursor(req.into())?;
                Ok(0)
            }
            cmd @ ModeCursor2 => {
                let req = cmd.read()?;
                self.update_cursor(req)?;
                Ok(0)
            }
            cmd @ GemClose => {
                let req = cmd.read()?;
                self.gem_close(&req)?;
                Ok(0)
            }
            cmd @ GemFlink => {
                let req = cmd.read()?;
                cmd.write(&self.gem_flink(&req)?)?;
                Ok(0)
            }
            cmd @ GemOpen => {
                let req = cmd.read()?;
                cmd.write(&self.gem_open(&req)?)?;
                Ok(0)
            }
            cmd @ VirtgpuGetparam => {
                let req = cmd.read()?;
                self.virtgpu_getparam(&req)?;
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

    char::register(Arc::new(Dri {
        node: DriNode::Card,
    }))
    .expect("failed to register the DRM card device");
    char::register(Arc::new(Dri {
        node: DriNode::Render,
    }))
    .expect("failed to register the DRM render device");
}
