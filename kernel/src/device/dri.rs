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

mod backend;
mod cursor;
mod fence;
mod prime;

/// The dma-buf descriptor, reachable so that the SCM_RIGHTS classifier can
/// recognize one. See [`prime::DmaBufFile`].
pub(crate) use prime::DmaBufFile;

use core::sync::atomic::{AtomicBool, AtomicU32, Ordering};

use align_ext::AlignExt;
use aster_virtio::device::gpu::{VirtioGpuBox, device::GpuDevice, first_device};
use device_id::{DeviceId, MajorId, MinorId};
use ostd::{
    mm::{Paddr, VmIo},
    task::Task,
};

use self::{
    backend::{
        CursorBackend, CursorGeometry, CursorScanoutBuffer, DamageRect,
        FirmwareFramebufferBackend, ScanoutBackend, ScanoutBuffer,
    },
    cursor::{
        CursorBuffer, CursorImage, CursorState, DrmModeCursor, DrmModeCursor2, MODE_CURSOR_BO,
        validate_cursor,
    },
};
use aster_framebuffer::framebuffer::{FRAMEBUFFER, FrameBuffer};
use crate::{
    context::current_userspace,
    device::{Device, DeviceType, DevtmpfsInodeMeta, registry::char},
    events::IoEvents,
    fs::{
        file::{FileLike, Mappable, PerOpenFileOps, StatusFlags, file_table::FdFlags},
        vfs::{inode::FileOps, path::Path},
    },
    prelude::*,
    process::signal::{PollHandle, Pollable, Pollee},
    util::ioctl::{RawIoctl, dispatch_ioctl},
    vm::page_cache::{Vmo, VmoFlags, VmoOptions},
};

/// Linux DRM character-device major number.
pub(super) const DRM_MAJOR: u16 = 226;

/// The DRM driver name, which is load-bearing rather than descriptive.
///
/// Mesa picks the DRI driver for a device that is not on the PCI bus from
/// this string and nothing else: `loader_get_kernel_driver_name()` reads it
/// back out of `DRM_IOCTL_VERSION`, the pipe loader matches it with `strcmp`
/// against the `virtio_gpu` descriptor, and a miss there does not fail loudly
/// -- it quietly selects the `kmsro` descriptor, which cannot create a screen,
/// and Mesa then falls back to `kms_swrast` (llvmpipe).
///
/// So the underscore is the ABI: Linux's virtio-gpu driver reports
/// `"virtio_gpu"`, and anything else makes accelerated rendering silently
/// unavailable.
pub(super) const DRIVER_NAME: &str = "virtio_gpu";

/// The driver name reported when nothing but a firmware framebuffer is present.
///
/// `simpledrm` is what Linux calls its DRM driver for exactly this situation —
/// a display a bootloader programmed and left running, which the kernel can
/// only copy into. Matching that name is deliberate: a client that recognizes
/// it is looking at the same hardware situation and will do the right thing
/// for it.
///
/// Deliberately not `virtio_gpu`, for the reason above: this name is how a
/// client decides which driver to use, and claiming a device that is not there
/// would send it to a 3D path that cannot exist on this machine.
const FIRMWARE_DRIVER_NAME: &str = "simpledrm";
const DRIVER_DATE: &str = "20260815";
const DRIVER_DESC: &str = "Asterinas virtio-gpu driver";

/// KMS object ids. The virtio-gpu device exposes a single CRTC/encoder/connector.
const CRTC_ID: u32 = 1;
const ENCODER_ID: u32 = 1;
const CONNECTOR_ID: u32 = 1;
/// The one overlay/primary plane the single scanout is driven through.
const PLANE_ID: u32 = 1;

/// `DRM_MODE_CONNECTOR_VIRTUAL`, the connector type Linux's virtio-gpu reports.
const DRM_MODE_CONNECTOR_VIRTUAL: u32 = 15;
/// `DRM_MODE_CONNECTOR_Unknown`, for a display whose connector cannot be named.
const DRM_MODE_CONNECTOR_UNKNOWN: u32 = 0;
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
const DRM_CAP_PRIME: u64 = 5;

/// `DRM_PRIME_CAP_*`: which directions of buffer sharing this device offers.
///
/// This is not a free-standing claim — Mesa reads it as the answer to "can
/// this device hand a buffer out at all", and takes its `create_dumb()` path
/// for every buffer when the answer is no. The two bits are backed by
/// `PRIME_HANDLE_TO_FD` and `PRIME_FD_TO_HANDLE` below; advertising a bit
/// whose ioctl is missing would move a client onto a path that then fails
/// further along, which is harder to diagnose than never being offered it.
const DRM_PRIME_CAP_IMPORT: u64 = 0x1;
const DRM_PRIME_CAP_EXPORT: u64 = 0x2;

/// `DRM_CLIENT_CAP_*` values accepted by `SET_CLIENT_CAP`.
const DRM_CLIENT_CAP_STEREO_3D: u64 = 1;
const DRM_CLIENT_CAP_UNIVERSAL_PLANES: u64 = 2;
const DRM_CLIENT_CAP_ATOMIC: u64 = 3;
const DRM_CLIENT_CAP_ASPECT_RATIO: u64 = 4;
const DRM_CLIENT_CAP_WRITEBACK_CONNECTORS: u64 = 5;
const DRM_CLIENT_CAP_CURSOR_PLANE_HOTSPOT: u64 = 6;

/// Size of the single contiguous dumb-buffer pool, in bytes.
///
/// A single pool is required because the mmap path maps one `Mappable::Vmo`
/// per file and selects a buffer by its byte offset within it.
///
/// The size is set by the 3D path, not the 2D one. A single scanout is 4 MiB
/// at 1280x800, but a client that renders allocates several buffers at once —
/// glamor alone holds more than one — and the pool is a bump allocator, so a
/// freed buffer's span is not reused (see `release_object`). 16 MiB ran out
/// during a probe that asked for five buffers in a row, which surfaces as
/// `ENOMEM` from `gbm_bo_create` and, at the desktop, as a renderer that never
/// appears. 64 MiB is the size the frozen branch ran virgl with.
const DUMB_POOL_SIZE: usize = 64 * 1024 * 1024;

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

    /// The device-node name by itself, without the `/dev` directory it lives
    /// in. sysfs names the same node this way under `device/drm/`.
    const fn node_name(self) -> &'static str {
        match self {
            DriNode::Card => "card0",
            DriNode::Render => "renderD128",
        }
    }
}

/// Every DRM node this kernel exposes, as `(name, minor)` pairs, or an empty
/// slice when this machine has no display to drive.
///
/// The sysfs view of these nodes is built from this list, so it appears only
/// once the character devices themselves do.
pub(super) fn exposed_nodes() -> &'static [(&'static str, u32)] {
    const NODES: [(&str, u32); 2] = [
        (DriNode::Card.node_name(), DriNode::Card.minor()),
        (DriNode::Render.node_name(), DriNode::Render.minor()),
    ];

    // A firmware framebuffer is a display like any other: a machine that has
    // only that one still gets a card node, which is the whole point of the
    // firmware backend. Only a machine with neither has nothing to expose.
    if display_source().is_none() {
        return &[];
    }
    &NODES
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
    /// The id the host's renderer knows this object by, once it has been
    /// created as a 3D resource. `None` for 2D-only buffers, which the host
    /// never receives as resources.
    resource_id: Option<u32>,
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

/// Diagnostic switch, on with `asterinas.dri_trace=1`: print every DRM ioctl
/// command as it is dispatched.
///
/// A client that gives up on this device before issuing anything is a
/// different problem from one that is refused part-way, and nothing else
/// distinguishes them: Mesa's own debug output is compiled out of Debian's
/// release build, and `LD_DEBUG` only shows the objects it ended up
/// loading, not the ioctls it chose not to send.
static DRI_TRACE: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.dri_trace", DRI_TRACE);

/// The one object space. Lock ordering: take a file's `DriInner` first, then
/// this, never the other way round.
static GEM_OBJECTS: SpinLock<GemObjects> = SpinLock::new(GemObjects::new());

#[derive(Debug)]
struct Dri {
    node: DriNode,
    /// What presents pixels, chosen once and shared by every open file.
    display: Arc<DisplayDevice>,
}

/// Everything that turns pixels into a visible image on this machine.
///
/// Selected once, when the nodes are registered, rather than per open file.
/// The choice cannot change while the machine runs — a virtio-gpu device does
/// not appear later, and the firmware framebuffer is fixed at boot — and every
/// open file has to reach the same scanout regardless.
struct DisplayDevice {
    /// The virtio-gpu device, when the machine has one.
    ///
    /// Absent on a machine whose only display is the firmware framebuffer.
    /// The ioctls specific to virtio-gpu have nothing to talk to there, and
    /// report so, rather than the driver refusing to exist.
    gpu: Option<Arc<GpuDevice>>,
    /// Presents framebuffers on the active scanout.
    scanout: Arc<dyn ScanoutBackend>,
    /// Hardware-cursor operations, absent for a backend that has no hardware
    /// cursor to program.
    cursor: Option<Arc<dyn CursorBackend>>,
    /// The driver name `DRM_IOCTL_VERSION` reports.
    ///
    /// Per backend rather than constant, because it names the driver a client
    /// should use, and "virtio_gpu" is a false answer on a machine that has no
    /// virtio-gpu at all.
    name: &'static str,
    /// The connector type `MODE_GETCONNECTOR` reports.
    connector_type: u32,
}

impl DisplayDevice {
    /// The largest mode a client may ask for, for `MODE_GETRESOURCES`.
    ///
    /// A backend that only copies into a fixed framebuffer accepts exactly the
    /// mode firmware chose, so advertising the range a programmable display
    /// allows would offer modes that later fail to set. A backend that can
    /// program the display keeps the permissive limit its host imposes.
    fn max_mode(&self) -> (u32, u32) {
        self.scanout
            .fixed_mode()
            .unwrap_or((MAX_RESOLUTION, MAX_RESOLUTION))
    }
}

impl Debug for DisplayDevice {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        // Only which devices are present is worth printing: the backends are
        // trait objects and have nothing to say about themselves.
        let (virtio_gpu, hardware_cursor) = (self.gpu.is_some(), self.cursor.is_some());
        f.debug_struct("DisplayDevice")
            .field("driver", &self.name)
            .field("virtio_gpu", &virtio_gpu)
            .field("hardware_cursor", &hardware_cursor)
            .finish()
    }
}

/// What can present pixels on this machine, before anything is built from it.
///
/// Separated from construction so that the question "should a DRM node exist at
/// all" can be asked cheaply, and so that the answer and the construction read
/// the same check.
enum DisplaySource {
    Virtio(Arc<GpuDevice>),
    Firmware(Arc<FrameBuffer>),
}

impl DisplaySource {
    /// The driver name `DRM_IOCTL_VERSION` and sysfs report for this source.
    fn driver_name(&self) -> &'static str {
        match self {
            DisplaySource::Virtio(_) => DRIVER_NAME,
            DisplaySource::Firmware(_) => FIRMWARE_DRIVER_NAME,
        }
    }
}

/// The display this machine has, if it has one this driver can present through.
///
/// virtio-gpu wins when both are present: it is the device with a 3D path and a
/// hardware cursor, and on a machine that has one the firmware framebuffer is
/// the console's, not the client's.
fn display_source() -> Option<DisplaySource> {
    if let Some(gpu) = first_device() {
        return Some(DisplaySource::Virtio(gpu));
    }
    let framebuffer = FRAMEBUFFER.get()?;
    FirmwareFramebufferBackend::accepts(framebuffer).then(|| {
        DisplaySource::Firmware(Arc::clone(framebuffer))
    })
}

/// The name of the driver that is actually presenting, for sysfs to report.
///
/// Read from the selected display rather than from `DRIVER_NAME`, which is a
/// virtio-gpu constant: a machine whose only display is the firmware
/// framebuffer has no virtio-gpu, and its `uevent` said so anyway. libdrm
/// reads that file to describe the device, and a guest checking it would have
/// been reading a constant -- which is the same defect as a guest that prints
/// a constant, one layer down.
pub(super) fn driver_name() -> &'static str {
    display_source().map_or(DRIVER_NAME, |source| source.driver_name())
}

/// Builds the presentation devices for a source.
fn display_device(source: DisplaySource) -> Result<DisplayDevice> {
    let name = source.driver_name();
    Ok(match source {
        DisplaySource::Virtio(gpu) => DisplayDevice {
            scanout: Arc::clone(&gpu) as Arc<dyn ScanoutBackend>,
            cursor: Some(Arc::clone(&gpu) as Arc<dyn CursorBackend>),
            gpu: Some(gpu),
            name,
            connector_type: DRM_MODE_CONNECTOR_VIRTUAL,
        },
        DisplaySource::Firmware(framebuffer) => DisplayDevice {
            scanout: Arc::new(FirmwareFramebufferBackend::new(framebuffer)?),
            // The firmware backend owns no display hardware, so it cannot
            // place a cursor. `None` is what makes the driver refuse cursor
            // requests outright, which is the answer that lets a client draw
            // the pointer itself instead of waiting for one that never comes.
            cursor: None,
            gpu: None,
            name,
            // Not `DRM_MODE_CONNECTOR_VIRTUAL`: there is a real connector here,
            // firmware is driving it, and this driver has no way to ask what
            // kind it is. "Unknown" is what the uapi provides for exactly that.
            connector_type: DRM_MODE_CONNECTOR_UNKNOWN,
        },
    })
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
    /// What presents pixels, shared with every other open file of this node.
    display: Arc<DisplayDevice>,
    /// Serializes implicit context creation for this file.
    context_operation: Mutex<()>,
    cursor_operation: Mutex<()>,
    inner: SpinLock<DriInner>,
    /// Readiness for `poll` and `epoll`.
    ///
    /// A DRM descriptor becomes readable when the driver has an event queued
    /// for this file, such as a completed page flip. This driver queues none,
    /// so it is never readable — but the poller still has to be registered, so
    /// that adding one later does not mean revisiting every call site.
    events: Pollee,
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
    /// The magic token this file was given by `DRM_IOCTL_GET_MAGIC`.
    ///
    /// Linux allocates these per master file for the legacy authentication
    /// handshake. Nothing on this device consults authentication, so the value
    /// only has to be unique across the device and stable for the file that
    /// asked, which is what the counter it is drawn from provides.
    magic: Option<u32>,
    /// The 3D context this file created, if it has created one.
    ///
    /// Linux allows one context per file and refuses a second, so the field
    /// doubles as the "already created" flag.
    context_id: Option<u32>,
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

/// `struct drm_auth`; the magic token alone.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmAuth {
    magic: u32,
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

/// `struct drm_prime_handle`, shared by both PRIME ioctls.
///
/// The same layout carries a handle out (`HANDLE_TO_FD`) and in
/// (`FD_TO_HANDLE`); which field the caller fills in is what the command
/// selects.
///
/// Twelve bytes, and there is no padding field to add: the struct's size is
/// part of the ioctl's command number, so a fourth field makes this a
/// definition of a different command. A client's request then arrives as an
/// unknown ioctl and is refused with `ENOTTY`, which reads like a permission
/// problem and is not one. The guest confirmed the size directly — the trace
/// shows the client sending `0xc00c642d` (size `0x0c`).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmPrimeHandle {
    handle: u32,
    flags: u32,
    fd: i32,
}

/// `struct drm_mode_get_plane_res`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmModeGetPlaneRes {
    plane_id_ptr: u64,
    count_planes: u32,
    pad: u32,
}

/// `struct drm_mode_get_plane`.
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

/// `struct drm_virtgpu_get_caps`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVirtgpuGetCaps {
    cap_set_id: u32,
    cap_set_ver: u32,
    /// Userspace address the capability blob is copied out to.
    addr: u64,
    size: u32,
    pad: u32,
}

/// `struct drm_virtgpu_context_set_param`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVirtgpuContextSetParam {
    param: u64,
    value: u64,
}

/// `struct drm_virtgpu_context_init`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVirtgpuContextInit {
    num_params: u32,
    pad: u32,
    /// Userspace address of a `DrmVirtgpuContextSetParam` array.
    ctx_set_params: u64,
}

/// `struct drm_virtgpu_map`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVirtgpuMap {
    /// Pool offset to pass to `mmap`, in bytes.
    offset: u64,
    handle: u32,
    pad: u32,
}

/// `struct drm_virtgpu_resource_create`.
///
/// `bo_handle` and `res_handle` are outputs: the first names the buffer in this
/// file, the second names it to the host's renderer.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVirtgpuResourceCreate {
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
    bo_handle: u32,
    res_handle: u32,
    size: u32,
    stride: u32,
}

/// `struct drm_virtgpu_resource_info`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVirtgpuResourceInfo {
    bo_handle: u32,
    res_handle: u32,
    size: u32,
    blob_mem: u32,
}

/// `struct drm_virtgpu_execbuffer`.
///
/// The trailing fields describe sync objects this driver does not implement,
/// and are present for a reason that is easy to miss: they are part of the
/// struct, and the struct's *size* is part of the ioctl's command number. A
/// definition that stops at `fence_fd` is 32 bytes where a client sends 64, so
/// the request arrives as a different command and is refused as unknown before
/// it is ever decoded — which is what Mesa reports as "got error from kernel -
/// expect bad rendering".
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVirtgpuExecbuffer {
    flags: u32,
    size: u32,
    /// Userspace address of the virgl command buffer.
    command: u64,
    /// Userspace address of a `u32` array of buffer handles.
    bo_handles: u64,
    num_bo_handles: u32,
    /// Out-fence, filled in when `VIRTGPU_EXECBUF_FENCE_FD_OUT` is set.
    fence_fd: i32,
    ring_idx: u32,
    syncobj_stride: u32,
    num_in_syncobjs: u32,
    num_out_syncobjs: u32,
    in_syncobjs: u64,
    out_syncobjs: u64,
}

/// `struct drm_virtgpu_3d_wait`, the argument of `VIRTGPU_WAIT`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVirtgpuWait {
    handle: u32,
    flags: u32,
}

/// `struct drm_virtgpu_3d_box`: the region a 3D transfer covers.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVirtgpuBox {
    x: u32,
    y: u32,
    z: u32,
    w: u32,
    h: u32,
    d: u32,
}

/// `struct drm_virtgpu_3d_transfer_to_host` and its `from_host` twin, which
/// have the same layout. 44 bytes; the size is part of the ioctl's command
/// number, so it has to match the client's definition exactly.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVirtgpuTransfer3d {
    bo_handle: u32,
    box_: DrmVirtgpuBox,
    level: u32,
    offset: u32,
    stride: u32,
    layer_stride: u32,
}

/// `VIRTGPU_EXECBUF_*` flags.
const VIRTGPU_EXECBUF_FENCE_FD_OUT: u32 = 0x02;

/// Fence ids issued to the host, which it echoes back when the work retires.
static NEXT_FENCE_ID: AtomicU32 = AtomicU32::new(1);

/// `VIRTGPU_PARAM_*` query ids (include/uapi/drm/virtgpu_drm.h).
const VIRTGPU_PARAM_3D_FEATURES: u64 = 1;
const VIRTGPU_PARAM_CAPSET_QUERY_FIX: u64 = 2;
const VIRTGPU_PARAM_SUPPORTED_CAPSET_IDS: u64 = 7;

/// `VIRTGPU_CONTEXT_PARAM_*` ids accepted by `CONTEXT_INIT`.
const VIRTGPU_CONTEXT_PARAM_CAPSET_ID: u64 = 1;
const VIRTGPU_CONTEXT_PARAM_NUM_RINGS: u64 = 2;
const VIRTGPU_CONTEXT_PARAM_POLL_RINGS_MASK: u64 = 3;
const VIRTGPU_CONTEXT_PARAM_DEBUG_NAME: u64 = 4;

/// How many context parameters `CONTEXT_INIT` accepts, matching Linux's
/// `VIRTGPU_MAX_CTX_PARAMS`.
const VIRTGPU_MAX_CTX_PARAMS: u32 = 4;

/// The number of rings this driver supports on a context.
///
/// A context is created with the one default ring: the multi-ring submission
/// the parameter exists to enable is not implemented, so asking for more is
/// refused rather than accepted and quietly ignored.
const SUPPORTED_RING_COUNT: u64 = 1;

/// Ids for the 3D contexts this driver hands out.
///
/// The host keys contexts by id across every open file, so these cannot be
/// per-file numbers: two clients each starting at 1 would collide.
static NEXT_CONTEXT_ID: AtomicU32 = AtomicU32::new(1);

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

/// `struct drm_clip_rect`: one damaged region in half-open coordinates.
///
/// Half-open as the kernel uses it — a rect from `(x1, y1)` to `(x2, y2)`
/// covers `x1` up to but not including `x2` — even though the X11 protocol
/// these usually come from draws both ends. A client that sends the closed
/// form damages one extra row and column, which is a repaint it did not need,
/// never a pixel it missed.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmClipRect {
    x1: u16,
    y1: u16,
    x2: u16,
    y2: u16,
}

/// The most damage regions one `MODE_DIRTYFB` may describe.
///
/// The count comes from userspace and names memory the kernel is about to
/// walk, so it is bounded rather than trusted. A client wanting more than this
/// is describing the whole frame, and an empty list already means that without
/// the copy.
const MAX_DIRTY_CLIPS: u32 = 256;

/// Reads the damage list a `MODE_DIRTYFB` names, or an empty one if it named none.
///
/// An empty list is not "nothing was damaged": it is the whole frame, which is
/// what a client passes when it cannot say. Returning an empty `Vec` here and
/// letting the backend read it that way keeps that meaning in one place.
fn read_dirty_clips(req: &DrmModeFbDirtyCmd) -> Result<Vec<DrmClipRect>> {
    if req.num_clips == 0 || req.clips_ptr == 0 {
        return Ok(Vec::new());
    }
    if req.num_clips > MAX_DIRTY_CLIPS {
        return_errno_with_message!(Errno::EINVAL, "too many damage clip rectangles");
    }

    let mut clips = Vec::with_capacity(req.num_clips as usize);
    for index in 0..req.num_clips as usize {
        let offset = req.clips_ptr as usize + index * size_of::<DrmClipRect>();
        clips.push(
            current_userspace!()
                .read_val(offset)
                .map_err(|_| Error::with_message(Errno::EFAULT, "bad damage clip rectangle"))?,
        );
    }
    Ok(clips)
}

/// The next magic token `DRM_IOCTL_GET_MAGIC` will hand out.
///
/// Device-wide and monotonic, so two files never share one. `DRM_IOCTL_AUTH_MAGIC`
/// only has to recognise a token this device issued, and a counter is enough to
/// answer that -- which is honest only because nothing here checks the result.
static NEXT_MAGIC: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(1);

mod ioctl_defs {
    use super::{
        DrmGetCap, DrmModeCardRes, DrmModeCreateDumb, DrmModeCrtc, DrmModeCrtcPageFlip,
        DrmModeCursor, DrmModeCursor2, DrmModeDestroyDumb, DrmModeFbCmd, DrmModeFbDirtyCmd,
        DrmGemClose, DrmGemFlink, DrmGemOpen, DrmModeGetConnector, DrmModeGetEncoder,
        DrmPrimeHandle,
        DrmVirtgpuContextInit, DrmVirtgpuExecbuffer, DrmVirtgpuGetCaps, DrmVirtgpuGetparam,
        DrmVirtgpuMap,
        DrmVirtgpuResourceCreate, DrmVirtgpuResourceInfo, DrmVirtgpuTransfer3d, DrmVirtgpuWait,
        DrmModeGetPlane, DrmModeGetPlaneRes, DrmModeMapDumb, DrmModeObjGetProperties,
        DrmSetClientCap, DrmVersion, DrmAuth,
    };
    use crate::util::ioctl::{InData, InOutData, NoData, OutData, ioc};

    // Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h>.
    pub(super) type GetVersion = ioc!(DRM_IOCTL_VERSION, b'd', 0x00, InOutData<DrmVersion>);
    pub(super) type GetCap = ioc!(DRM_IOCTL_GET_CAP, b'd', 0x0c, InOutData<DrmGetCap>);
    // The legacy authentication pair. Their absence was not harmless: glamor
    // answers `DRI3Open` by opening the device and then calling `drmGetMagic`
    // on it, and it treats exactly one errno as good news --
    //
    //     if (drmGetMagic(fd, &magic) < 0) {
    //         if (errno == EACCES) { *fdp = fd; return Success; }
    //         else { close(fd); return BadMatch; }
    //     }
    //
    // `EACCES` is not a failure there, it is how a caller learns the node
    // needs no authentication. Linux produces it by design: a render node
    // refuses any ioctl whose descriptor lacks `DRM_RENDER_ALLOW`, and
    // `GET_MAGIC` is one of those. An unimplemented request produces `ENOTTY`
    // instead, which glamor reads as a real error, closes the fd and answers
    // the client with `BadMatch`.
    // The directions are not incidental -- they are two of the four fields
    // that make up the command number. `GET_MAGIC` is `_IOR` and `AUTH_MAGIC`
    // is `_IOW`, and writing either as `InOutData` produces a *different
    // ioctl*: `0xc0046402` where libdrm sends `0x80046402`. The kernel then
    // answers `ENOTTY`, which is indistinguishable from not having implemented
    // the request at all -- the same failure this pair was added to fix, one
    // layer down. A struct's size and an ioctl's direction are both part of its
    // name.
    pub(super) type GetMagic = ioc!(DRM_IOCTL_GET_MAGIC, b'd', 0x02, OutData<DrmAuth>);
    pub(super) type AuthMagic = ioc!(DRM_IOCTL_AUTH_MAGIC, b'd', 0x11, InData<DrmAuth>);
    pub(super) type SetClientCap = ioc!(DRM_IOCTL_SET_CLIENT_CAP, b'd', 0x0d, InData<DrmSetClientCap>);
    // Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h>.
    pub(super) type GemClose = ioc!(DRM_IOCTL_GEM_CLOSE, b'd', 0x09, InData<DrmGemClose>);
    pub(super) type GemFlink = ioc!(DRM_IOCTL_GEM_FLINK, b'd', 0x0a, InOutData<DrmGemFlink>);
    pub(super) type GemOpen = ioc!(DRM_IOCTL_GEM_OPEN, b'd', 0x0b, InOutData<DrmGemOpen>);
    // Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h>.
    pub(super) type PrimeHandleToFd = ioc!(DRM_IOCTL_PRIME_HANDLE_TO_FD, b'd', 0x2d, InOutData<DrmPrimeHandle>);
    pub(super) type PrimeFdToHandle = ioc!(DRM_IOCTL_PRIME_FD_TO_HANDLE, b'd', 0x2e, InOutData<DrmPrimeHandle>);
    // The virtgpu ioctls live at `DRM_COMMAND_BASE` (0x40) plus their number.
    pub(super) type VirtgpuMap = ioc!(DRM_IOCTL_VIRTGPU_MAP, b'd', 0x41, InOutData<DrmVirtgpuMap>);
    pub(super) type VirtgpuExecbuffer = ioc!(DRM_IOCTL_VIRTGPU_EXECBUFFER, b'd', 0x42, InOutData<DrmVirtgpuExecbuffer>);
    pub(super) type VirtgpuGetparam = ioc!(DRM_IOCTL_VIRTGPU_GETPARAM, b'd', 0x43, InOutData<DrmVirtgpuGetparam>);
    pub(super) type VirtgpuResourceCreate = ioc!(DRM_IOCTL_VIRTGPU_RESOURCE_CREATE, b'd', 0x44, InOutData<DrmVirtgpuResourceCreate>);
    pub(super) type VirtgpuResourceInfo = ioc!(DRM_IOCTL_VIRTGPU_RESOURCE_INFO, b'd', 0x45, InOutData<DrmVirtgpuResourceInfo>);
    pub(super) type VirtgpuGetCaps = ioc!(DRM_IOCTL_VIRTGPU_GET_CAPS, b'd', 0x49, InOutData<DrmVirtgpuGetCaps>);
    pub(super) type VirtgpuTransferFromHost = ioc!(DRM_IOCTL_VIRTGPU_TRANSFER_FROM_HOST, b'd', 0x46, InOutData<DrmVirtgpuTransfer3d>);
    pub(super) type VirtgpuTransferToHost = ioc!(DRM_IOCTL_VIRTGPU_TRANSFER_TO_HOST, b'd', 0x47, InOutData<DrmVirtgpuTransfer3d>);
    pub(super) type VirtgpuWait = ioc!(DRM_IOCTL_VIRTGPU_WAIT, b'd', 0x48, InOutData<DrmVirtgpuWait>);
    pub(super) type VirtgpuContextInit = ioc!(DRM_IOCTL_VIRTGPU_CONTEXT_INIT, b'd', 0x4b, InOutData<DrmVirtgpuContextInit>);
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
    pub(super) type ModeGetPlaneResources = ioc!(DRM_IOCTL_MODE_GETPLANERESOURCES, b'd', 0xb5, InOutData<DrmModeGetPlaneRes>);
    pub(super) type ModeGetPlane = ioc!(DRM_IOCTL_MODE_GETPLANE, b'd', 0xb6, InOutData<DrmModeGetPlane>);
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
        // The two PRIME ioctls are the pair the paragraph above says a render
        // client uses in place of a name; Linux marks them
        // `DRM_AUTH|DRM_RENDER_ALLOW`, so withholding them here would leave the
        // render node with no way to share a buffer at all.
        || PrimeHandleToFd::try_from_raw(raw_ioctl).is_some()
        || PrimeFdToHandle::try_from_raw(raw_ioctl).is_some()
        // A 3D client reaches the device through the render node, so the whole
        // sequence it runs to get rendering — ask what the host supports, read
        // the capability set, create a context — has to get through.
        || VirtgpuGetparam::try_from_raw(raw_ioctl).is_some()
        || VirtgpuGetCaps::try_from_raw(raw_ioctl).is_some()
        || VirtgpuContextInit::try_from_raw(raw_ioctl).is_some()
        || VirtgpuMap::try_from_raw(raw_ioctl).is_some()
        || VirtgpuExecbuffer::try_from_raw(raw_ioctl).is_some()
        || VirtgpuTransferFromHost::try_from_raw(raw_ioctl).is_some()
        || VirtgpuTransferToHost::try_from_raw(raw_ioctl).is_some()
        || VirtgpuWait::try_from_raw(raw_ioctl).is_some()
        || VirtgpuResourceCreate::try_from_raw(raw_ioctl).is_some()
        || VirtgpuResourceInfo::try_from_raw(raw_ioctl).is_some()
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
        // The scanout was already chosen when the node was registered, so the
        // mode a fresh file starts at is the one the display actually has.
        let (current_width, current_height) = self.display.scanout.dimensions();

        Ok(Box::new(DriHandle {
            node: self.node,
            display: Arc::clone(&self.display),
            context_operation: Mutex::new(()),
            cursor_operation: Mutex::new(()),
            events: Pollee::new(),
            inner: SpinLock::new(DriInner {
                handles: BTreeMap::new(),
                next_handle: 1,
                framebuffers: BTreeMap::new(),
                next_fb_id: 1,
                current_fb_id: None,
                current_width,
                current_height,
                cursor: CursorState::default(),
                magic: None,
                context_id: None,
            }),
        }))
    }
}

/// Installs a file the driver synthesized into the calling thread's file
/// table, returning the descriptor that names it.
fn install_file(file: Arc<dyn FileLike>, fd_flags: FdFlags) -> Result<i32> {
    let current_task = Task::current().ok_or_else(|| Error::with_message(Errno::EAGAIN, "no current task"))?;
    let thread_local = current_task
        .as_thread_local()
        .ok_or_else(|| Error::with_message(Errno::EAGAIN, "no thread-local storage"))?;
    // A thread executing an ioctl always has a file table — it reached the
    // syscall through a descriptor in one — so this cannot be absent.
    let file_table = thread_local.borrow_file_table();
    let mut file_table_locked = file_table.unwrap().write();
    Ok(file_table_locked.insert(file, fd_flags).into())
}

/// Creates a completed-fence descriptor in the calling thread's file table.
fn install_fence_file() -> Result<i32> {
    install_file(Arc::new(fence::FenceFile::new_signalled()), FdFlags::empty())
}

/// Reads a NUL-terminated debug name from userspace.
///
/// Bounded to the length the host's `CTX_CREATE` field can hold, so a client
/// cannot make the kernel walk an unbounded string.
fn read_user_debug_name(address: usize) -> Result<String> {
    const MAX_DEBUG_NAME: usize = 64;

    let mut buffer = [0u8; MAX_DEBUG_NAME];
    current_userspace!()
        .read_bytes(address, &mut buffer)
        .map_err(|_| Error::with_message(Errno::EFAULT, "bad debug name pointer"))?;
    let length = buffer
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(MAX_DEBUG_NAME);
    Ok(String::from_utf8_lossy(&buffer[..length]).into_owned())
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

/// Carves a new object out of the device-wide pool, returning its id.
///
/// Shared by the 2D and 3D allocation paths so both reach the same pool and the
/// same id space. Handle numbering stays with the caller: a handle is a name
/// one open file gives an object, not a property of the object.
fn alloc_object(
    objects: &mut GemObjects,
    size: usize,
    pitch: u32,
    width: u32,
    height: u32,
    bpp: u32,
) -> Result<u32> {
    ensure_pool(objects)?;
    let offset = objects.next_offset.align_up(PAGE_SIZE);
    let end = offset
        .checked_add(size)
        .ok_or_else(|| Error::with_message(Errno::ENOMEM, "buffer size overflows"))?;
    if end > DUMB_POOL_SIZE {
        return_errno_with_message!(Errno::ENOMEM, "buffer pool is exhausted");
    }

    let object_id = objects.next_object_id;
    objects.next_object_id += 1;
    objects.objects.insert(
        object_id,
        GemObject {
            offset,
            size,
            pitch,
            width,
            height,
            bpp,
            resource_id: None,
            refs: 1,
        },
    );
    objects.next_offset = end.align_up(PAGE_SIZE);
    Ok(object_id)
}

/// Gives `object_id` a handle in this file, returning the handle.
fn name_object(inner: &mut DriInner, object_id: u32) -> u32 {
    let handle = inner.next_handle;
    inner.next_handle += 1;
    inner.handles.insert(handle, object_id);
    handle
}

impl DriHandle {
    /// The virtio-gpu device, for the ioctls only it can serve.
    ///
    /// A machine whose only display is the firmware framebuffer has no 3D path
    /// and no virtio-gpu control channel, and those ioctls say so rather than
    /// being served by something that is not there.
    fn gpu(&self) -> Result<&Arc<GpuDevice>> {
        let gpu = self.display.gpu.as_ref();
        gpu.ok_or_else(|| Error::with_message(Errno::ENODEV, "this DRM device has no virtio-gpu"))
    }

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
        let object_id = alloc_object(
            &mut objects,
            size,
            pitch,
            req.width,
            req.height,
            req.bpp,
        )?;
        let handle = name_object(&mut inner, object_id);

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
            VIRTGPU_PARAM_3D_FEATURES => u64::from(self.gpu()?.supports_virgl()),
            // The query-fix flag means the driver returns the capset the real
            // driver would, rather than a fixed stub.
            VIRTGPU_PARAM_CAPSET_QUERY_FIX => u64::from(self.gpu()?.supports_virgl()),
            VIRTGPU_PARAM_SUPPORTED_CAPSET_IDS => {
                if !self.gpu()?.supports_virgl() {
                    0
                } else {
                    // Read the host's first capset rather than assuming virgl,
                    // so the mask describes this host and not our expectation.
                    let info = self
                        .gpu()?
                        .capset_info(0)
                        .map_err(|_| Error::with_message(Errno::EIO, "capset query failed"))?;
                    1u64 << info.id
                }
            }
            _ => {
                if DRI_TRACE.load(Ordering::Relaxed) {
                    ostd::error!("DRI_GETPARAM param={} -> EINVAL", req.param);
                }
                return_errno_with_message!(Errno::EINVAL, "unknown virtgpu parameter")
            }
        };

        if DRI_TRACE.load(Ordering::Relaxed) {
            ostd::error!("DRI_GETPARAM param={} -> {:#x}", req.param, value);
        }
        current_userspace!()
            .write_val(req.value as usize, &value)
            .map_err(|_| Error::with_message(Errno::EFAULT, "bad virtgpu parameter pointer"))?;
        Ok(())
    }

    /// Copies the host's capability blob for a capability set to the caller.
    ///
    /// The blob is what tells a client how to build command streams for this
    /// renderer, so it has to come from the host rather than be invented here.
    fn virtgpu_get_caps(&self, req: &DrmVirtgpuGetCaps) -> Result<()> {
        let trace = DRI_TRACE.load(Ordering::Relaxed);
        if trace {
            ostd::error!(
                "DRI_GET_CAPS id={} ver={} size={} virgl={}",
                req.cap_set_id,
                req.cap_set_ver,
                req.size,
                self.gpu()?.supports_virgl()
            );
        }
        if !self.gpu()?.supports_virgl() {
            return_errno_with_message!(Errno::EINVAL, "3D is not available");
        }
        if req.size == 0 {
            return_errno_with_message!(Errno::EINVAL, "capability request has zero size");
        }
        let info = self
            .gpu()?
            .capset_info(0)
            .map_err(|_| Error::with_message(Errno::EIO, "capset query failed"))?;
        if trace {
            ostd::error!(
                "DRI_GET_CAPS host id={} max_ver={} max_size={}",
                info.id,
                info.max_version,
                info.max_size
            );
        }
        if req.cap_set_id != info.id {
            return_errno_with_message!(Errno::EINVAL, "unknown capability set id");
        }
        if req.cap_set_ver > info.max_version {
            return_errno_with_message!(Errno::EINVAL, "unsupported capability set version");
        }

        // Fetch the whole blob and hand back only what was asked for, as Linux
        // does: the host answers for one version, not for one length.
        let blob = self
            .gpu()?
            .capset(info.id, req.cap_set_ver, info.max_size)
            .map_err(|_| Error::with_message(Errno::EIO, "capability set query failed"))?;
        if trace {
            ostd::error!("DRI_GET_CAPS blob_len={} copy={}", blob.len(), req.size);
        }
        let copy = (req.size as usize).min(blob.len());
        current_userspace!()
            .write_bytes(req.addr as usize, &blob[..copy])
            .map_err(|_| Error::with_message(Errno::EFAULT, "bad capability set pointer"))?;
        Ok(())
    }

    /// Returns this file's 3D context, creating one if it has none.
    ///
    /// A client does not have to ask for a context to need one. Mesa queries
    /// the `CONTEXT_INIT` parameter, finds this driver does not offer it, and
    /// submits without ever calling `VIRTGPU_CONTEXT_INIT` — it expects the
    /// driver to have a context ready, as Linux's does from the moment a
    /// client that can render opens the node. Creating it on first use rather
    /// than at open keeps clients that never render from paying for a host
    /// round-trip.
    fn ensure_context(&self) -> Result<u32> {
        let _operation = self.context_operation.lock();
        if let Some(context_id) = self.inner.lock().context_id {
            return Ok(context_id);
        }
        let info = self
            .gpu()?
            .capset_info(0)
            .map_err(|_| Error::with_message(Errno::EIO, "capset query failed"))?;
        let context_id = NEXT_CONTEXT_ID.fetch_add(1, Ordering::Relaxed);
        self.gpu()?
            .context_create(context_id, info.id, "asterinas")
            .map_err(|_| Error::with_message(Errno::EIO, "context creation failed"))?;
        self.inner.lock().context_id = Some(context_id);
        Ok(context_id)
    }

    /// Creates this file's 3D context, against the capability set it names.
    fn virtgpu_context_init(&self, req: &DrmVirtgpuContextInit) -> Result<()> {
        if !self.gpu()?.supports_virgl() {
            return_errno_with_message!(Errno::EINVAL, "3D is not available");
        }
        if req.num_params > VIRTGPU_MAX_CTX_PARAMS {
            return_errno_with_message!(Errno::EINVAL, "too many context parameters");
        }
        if self.inner.lock().context_id.is_some() {
            return_errno_with_message!(Errno::EEXIST, "context already created");
        }

        let mut capset_id = None;
        let mut debug_name = String::new();
        for index in 0..req.num_params as usize {
            let offset = req.ctx_set_params as usize
                + index * size_of::<DrmVirtgpuContextSetParam>();
            let entry: DrmVirtgpuContextSetParam = current_userspace!()
                .read_val(offset)
                .map_err(|_| Error::with_message(Errno::EFAULT, "bad context parameters"))?;
            match entry.param {
                VIRTGPU_CONTEXT_PARAM_CAPSET_ID => {
                    if capset_id.is_some() {
                        return_errno_with_message!(Errno::EINVAL, "capset id given twice");
                    }
                    capset_id = Some(
                        u32::try_from(entry.value)
                            .map_err(|_| Error::with_message(Errno::EINVAL, "capset id out of range"))?,
                    );
                }
                VIRTGPU_CONTEXT_PARAM_DEBUG_NAME => {
                    debug_name = read_user_debug_name(entry.value as usize)?;
                }
                VIRTGPU_CONTEXT_PARAM_NUM_RINGS => {
                    if entry.value != SUPPORTED_RING_COUNT {
                        return_errno_with_message!(
                            Errno::EINVAL,
                            "this driver creates contexts with one ring"
                        );
                    }
                }
                VIRTGPU_CONTEXT_PARAM_POLL_RINGS_MASK => {
                    if entry.value != 0 {
                        return_errno_with_message!(
                            Errno::EINVAL,
                            "ring polling is not implemented"
                        );
                    }
                }
                _ => return_errno_with_message!(Errno::EINVAL, "unknown context parameter"),
            }
        }

        // A context is created against a renderer; without a capability set
        // there is nothing to create it against.
        let capset_id =
            capset_id.ok_or_else(|| Error::with_message(Errno::EINVAL, "no capset id given"))?;
        let info = self
            .gpu()?
            .capset_info(0)
            .map_err(|_| Error::with_message(Errno::EIO, "capset query failed"))?;
        if capset_id != info.id {
            return_errno_with_message!(Errno::EINVAL, "unknown capability set id");
        }

        let context_id = NEXT_CONTEXT_ID.fetch_add(1, Ordering::Relaxed);
        self.gpu()?
            .context_create(context_id, capset_id, &debug_name)
            .map_err(|_| Error::with_message(Errno::EIO, "context creation failed"))?;
        self.inner.lock().context_id = Some(context_id);
        Ok(())
    }

    /// Reports the pool offset a client passes to `mmap` for a handle.
    ///
    /// The same answer `MODE_MAP_DUMB` gives; 3D clients reach it through this
    /// ioctl instead, which knows only about handles.
    fn virtgpu_map(&self, req: &DrmVirtgpuMap) -> Result<DrmVirtgpuMap> {
        let object_id = object_for_handle(&self.inner.lock(), req.handle)?;
        let object = object_by_id(&GEM_OBJECTS.lock(), object_id)?;
        Ok(DrmVirtgpuMap {
            offset: object.offset as u64,
            ..*req
        })
    }

    /// Creates a 3D resource and the guest buffer backing it.
    ///
    /// This is where a 3D buffer comes from: the renderer is told the
    /// resource's shape, the guest memory it reads through is attached as its
    /// backing, and the resource is attached to this file's context so a
    /// command buffer submitted to that context may name it.
    fn virtgpu_resource_create(
        &self,
        req: &DrmVirtgpuResourceCreate,
    ) -> Result<DrmVirtgpuResourceCreate> {
        if !self.gpu()?.supports_virgl() {
            return_errno_with_message!(Errno::EINVAL, "3D is not available");
        }
        if req.size == 0 {
            return_errno_with_message!(Errno::EINVAL, "resource has zero size");
        }
        let context_id = self.ensure_context()?;

        // The file's handle table and the device-wide pool are both touched, in
        // the order `GEM_OBJECTS` documents.
        let mut inner = self.inner.lock();
        let (handle, object_id, object) = {
            let mut objects = GEM_OBJECTS.lock();
            let object_id = alloc_object(
                &mut objects,
                req.size as usize,
                req.stride,
                req.width,
                req.height,
                0,
            )?;
            let handle = name_object(&mut inner, object_id);
            let object = object_by_id(&objects, object_id)?;
            (handle, object_id, object)
        };
        drop(inner);

        // From the device's counter, not one of this module's own: the scanout
        // resource already holds id 1, and the host refuses a duplicate.
        let resource_id = self.gpu()?.reserve_resource_id();
        let base = {
            let mut objects = GEM_OBJECTS.lock();
            let base = pool_paddr(&objects)?;
            // Recorded before the host is told, so a failure below cannot
            // leave an object claiming a resource that was never created.
            if let Some(entry) = objects.objects.get_mut(&object_id) {
                entry.resource_id = Some(resource_id);
            }
            base
        };

        // A resource the host never heard of, or one whose memory it cannot
        // reach, is not usable: both steps have to succeed for the handle to be
        // worth returning.
        let create = self.gpu()?.resource_create_3d(
            resource_id,
            req.target,
            req.format,
            req.bind,
            req.width,
            req.height,
            req.depth,
            req.array_size,
            req.last_level,
            req.nr_samples,
            req.flags,
        );
        if create.is_err() {
            self.destroy_dumb(&DrmModeDestroyDumb { handle })?;
            return_errno_with_message!(Errno::EIO, "3D resource creation failed");
        }
        if self
            .gpu()?
            .attach_backing(resource_id, (base + object.offset) as u64, object.size as u32)
            .is_err()
        {
            self.destroy_dumb(&DrmModeDestroyDumb { handle })?;
            return_errno_with_message!(Errno::EIO, "3D resource backing could not be attached");
        }
        if self
            .gpu()?
            .attach_resource_to_context(context_id, resource_id)
            .is_err()
        {
            self.destroy_dumb(&DrmModeDestroyDumb { handle })?;
            return_errno_with_message!(Errno::EIO, "3D resource could not join the context");
        }

        Ok(DrmVirtgpuResourceCreate {
            bo_handle: handle,
            res_handle: resource_id,
            size: object.size as u32,
            ..*req
        })
    }

    /// Reports which of this file's handles names a given resource.
    ///
    /// The query is by resource id, which is the name the host knows, so the
    /// answer is whichever of this file's handles stands for it.
    fn virtgpu_resource_info(
        &self,
        req: &DrmVirtgpuResourceInfo,
    ) -> Result<DrmVirtgpuResourceInfo> {
        let inner = self.inner.lock();
        let objects = GEM_OBJECTS.lock();
        for (handle, object_id) in inner.handles.iter() {
            let Ok(object) = object_by_id(&objects, *object_id) else {
                continue;
            };
            if object.resource_id == Some(req.res_handle) {
                return Ok(DrmVirtgpuResourceInfo {
                    bo_handle: *handle,
                    res_handle: req.res_handle,
                    size: object.size as u32,
                    // Resources this driver creates are guest-backed, so there
                    // is no blob to name.
                    blob_mem: 0,
                });
            }
        }
        return_errno_with_message!(Errno::EINVAL, "unknown 3D resource");
    }

    /// Submits a virgl command buffer to this file's context.
    ///
    /// Nothing here renders: the commands are the client's, and what they draw
    /// is the renderer's business. What this has to get right is that the
    /// buffer arrives whole and that every buffer it names is one the renderer
    /// can actually reach.
    /// Moves a region of a 3D resource between guest memory and the host.
    ///
    /// A client uploads a texture before drawing with it and reads one back
    /// afterwards. Refusing either direction does not fail loudly: the
    /// renderer keeps working on whatever the buffer held before, so the
    /// picture is wrong rather than absent — which is why both are served.
    fn virtgpu_transfer_3d(&self, to_host: bool, req: &DrmVirtgpuTransfer3d) -> Result<()> {
        if !self.gpu()?.supports_virgl() {
            return_errno_with_message!(Errno::EINVAL, "3D is not available");
        }
        let context_id = self.ensure_context()?;
        let object_id = object_for_handle(&self.inner.lock(), req.bo_handle)?;
        let object = object_by_id(&GEM_OBJECTS.lock(), object_id)?;
        // Only a buffer the host already knows as a resource can be moved. A
        // plain 2D dumb buffer has no resource behind it for a transfer to
        // name, and inventing one would upload a span the host never received.
        let resource_id = object
            .resource_id
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "buffer is not a 3D resource"))?;

        let gpu = self.gpu()?;
        let transfer = if to_host {
            GpuDevice::transfer_to_host_3d
        } else {
            GpuDevice::transfer_from_host_3d
        };
        transfer(
            gpu,
            context_id,
            resource_id,
            VirtioGpuBox {
                x: req.box_.x,
                y: req.box_.y,
                z: req.box_.z,
                w: req.box_.w,
                h: req.box_.h,
                d: req.box_.d,
            },
            u64::from(req.offset),
            req.level,
            req.stride,
            req.layer_stride,
        )
        .map_err(|_| Error::with_message(Errno::EIO, "3D transfer failed"))
    }

    fn virtgpu_execbuffer(&self, req: &DrmVirtgpuExecbuffer) -> Result<DrmVirtgpuExecbuffer> {
        if !self.gpu()?.supports_virgl() {
            return_errno_with_message!(Errno::EINVAL, "3D is not available");
        }
        if req.size == 0 {
            return_errno_with_message!(Errno::EINVAL, "empty command buffer");
        }
        let context_id = self.ensure_context()?;

        let mut commands = Vec::new();
        commands.resize(req.size as usize, 0u8);
        current_userspace!()
            .read_bytes(req.command as usize, &mut commands)
            .map_err(|_| Error::with_message(Errno::EFAULT, "bad command buffer pointer"))?;

        // Every buffer the command stream names has to be one this file holds.
        // A handle the renderer was never given would leave it reading memory
        // nothing stands behind.
        for index in 0..req.num_bo_handles as usize {
            let offset = req.bo_handles as usize + index * size_of::<u32>();
            let handle: u32 = current_userspace!()
                .read_val(offset)
                .map_err(|_| Error::with_message(Errno::EFAULT, "bad buffer handle array"))?;
            object_for_handle(&self.inner.lock(), handle)?;
        }

        let fence_id = u64::from(NEXT_FENCE_ID.fetch_add(1, Ordering::Relaxed));
        self.gpu()?
            .submit_3d(context_id, &commands, fence_id)
            .map_err(|_| Error::with_message(Errno::EIO, "3D submission failed"))?;

        // The submission has completed by the time this returns — `submit_3d`
        // waits for the host — so a fence handed back now stands for work that
        // is already done, which is why it can be created signalled.
        let fence_fd = if req.flags & VIRTGPU_EXECBUF_FENCE_FD_OUT != 0 {
            install_fence_file()?
        } else {
            -1
        };

        Ok(DrmVirtgpuExecbuffer { fence_fd, ..*req })
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

    /// Imports the dma-buf a descriptor names as a handle in this file.
    ///
    /// A descriptor that names anything else — a regular file, a socket, a
    /// dma-buf exported by some other device — is refused rather than
    /// reinterpreted. The only handle this driver can mint is one onto its own
    /// pool, so accepting a stranger's descriptor would produce a handle that
    /// resolves to bytes the caller never had.
    fn prime_fd_to_handle(&self, fd: i32) -> Result<u32> {
        let current_task = Task::current().ok_or_else(|| Error::with_message(Errno::EAGAIN, "no current task"))?;
        let thread_local = current_task
            .as_thread_local()
            .ok_or_else(|| Error::with_message(Errno::EAGAIN, "no thread-local storage"))?;
        let file_table = thread_local.borrow_file_table();
        let file_table_locked = file_table.unwrap().read();
        let file = file_table_locked.get_file(fd.try_into()?)?;
        let dma_buf = (**file).downcast_ref::<prime::DmaBufFile>().ok_or_else(|| {
            Error::with_message(Errno::EINVAL, "descriptor is not a dma-buf from this device")
        })?;
        prime::fd_to_handle(self, dma_buf)
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

    /// Registers a framebuffer, which is a *view* of a buffer rather than a
    /// restatement of how that buffer was allocated.
    ///
    /// This used to require the request's geometry to equal the buffer's, which
    /// only held while every buffer came from `MODE_CREATE_DUMB`. A buffer now
    /// also arrives through `VIRTGPU_RESOURCE_CREATE`, where there is no bpp to
    /// record — a 3D resource is described by a format, not by bits per pixel —
    /// so that object carries `bpp == 0` and an exact comparison rejects Xorg's
    /// `drmModeAddFB(..., depth 24, bpp 32, ...)` for a buffer that is in fact
    /// the right one. The client then retries forever: `glxinfo` issues the same
    /// `ADDFB` with the same argument hundreds of times and never reaches a
    /// renderer. Linux does not compare against the allocation either; it
    /// checks that the format is one the device can scan out and that the
    /// buffer is large enough for the geometry asked for. This is that check.
    fn add_fb(&self, req: &DrmModeFbCmd) -> Result<u32> {
        let mut inner = self.inner.lock();
        let object_id = object_for_handle(&inner, req.handle)?;
        let object = object_by_id(&GEM_OBJECTS.lock(), object_id)?;
        // A row has to fit in the pitch, and the rows have to fit in the buffer.
        let bytes_per_pixel = u64::from(req.bpp).div_ceil(8);
        let minimum_pitch = u64::from(req.width)
            .checked_mul(bytes_per_pixel)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "framebuffer width overflows"))?;
        let span = u64::from(req.pitch)
            .checked_mul(u64::from(req.height))
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "framebuffer height overflows"))?;
        if u64::from(req.pitch) < minimum_pitch || span > object.size as u64 {
            return_errno_with_message!(Errno::EINVAL, "framebuffer does not fit its buffer");
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

    /// Presents a framebuffer on the scanout, making its pixels visible.
    ///
    /// Shared by `MODE_SETCRTC`, `MODE_PAGE_FLIP`, and `MODE_DIRTYFB`: all three
    /// ultimately make a framebuffer visible, and none of them can assume the
    /// display already has the pixels. On virtio-gpu the host only pulls fresh
    /// pixels during `TRANSFER_TO_HOST_2D` + `FLUSH`, so a guest-side mmap write
    /// alone is never seen; on the firmware framebuffer nothing reads the pool
    /// until it is copied into the scanout. Presenting in full is therefore the
    /// only correct default, and each backend decides what that costs.
    /// Builds the scanout buffer that a registered framebuffer names.
    fn scanout_buffer(&self, fb_id: u32) -> Result<(ScanoutBuffer, u32, u32)> {
        let inner = self.inner.lock();
        let fb = inner
            .framebuffers
            .get(&fb_id)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown framebuffer id"))?;
        let objects = GEM_OBJECTS.lock();
        let object = object_by_id(&objects, fb.object_id)?;
        // The pool itself is handed over, not its address: a backend that
        // copies pixels needs the memory, and only the virtio one can make do
        // with where it physically is.
        let pool = objects
            .pool
            .as_ref()
            .ok_or_else(|| Error::with_message(Errno::ENOMEM, "no dumb buffer pool"))?;
        let buffer = ScanoutBuffer::new(
            Arc::clone(pool),
            object.offset,
            object.pitch as usize,
            object.size as u32,
            fb.width,
            fb.height,
        );
        Ok((buffer, fb.width, fb.height))
    }

    /// Records which framebuffer the scanout is now showing.
    fn note_current_scanout(&self, fb_id: u32, width: u32, height: u32) {
        let mut inner = self.inner.lock();
        inner.current_fb_id = Some(fb_id);
        inner.current_width = width;
        inner.current_height = height;
    }

    fn present_fb(&self, fb_id: u32) -> Result<()> {
        let (buffer, width, height) = self.scanout_buffer(fb_id)?;
        self.display.scanout.present_framebuffer(buffer)?;
        self.note_current_scanout(fb_id, width, height);
        Ok(())
    }

    /// Re-presents only the regions the client says it changed.
    ///
    /// `MODE_DIRTYFB` is how a client that has already presented a frame says
    /// "this part of it moved". On virtio-gpu that is the same full transfer
    /// either way — the host command has no sub-rectangle form — so the
    /// backend's default re-presents everything. On the firmware backend the
    /// copy is the CPU's, and one cursor-sized rectangle against a whole
    /// 1920x1080 frame is the difference between a pointer that moves freely
    /// and one that visibly lags.
    fn dirty_fb(&self, fb_id: u32, clips: &[DrmClipRect]) -> Result<()> {
        let (buffer, width, height) = self.scanout_buffer(fb_id)?;
        // Validated against the framebuffer they address, because the backends
        // index with these: a rectangle reaching past the buffer would be an
        // out-of-range slice, not a wrong picture.
        let mut damage = Vec::with_capacity(clips.len());
        for clip in clips {
            damage.push(DamageRect::new(
                u32::from(clip.x1),
                u32::from(clip.y1),
                u32::from(clip.x2),
                u32::from(clip.y2),
                width,
                height,
            )?);
        }

        self.display.scanout.dirty_framebuffer(buffer, &damage)?;
        self.note_current_scanout(fb_id, width, height);
        Ok(())
    }

    fn update_cursor(&self, request: DrmModeCursor2) -> Result<()> {
        // Refused before anything else, because there is no cursor to program
        // and no honest way to report success. A client that is told the
        // hardware accepted a cursor will not draw one itself, so the pointer
        // would simply vanish; an error is what lets it fall back to a software
        // cursor. The firmware backend is the case this exists for.
        let Some(cursor) = self.display.cursor.as_ref() else {
            return_errno_with_message!(Errno::EOPNOTSUPP, "this display has no hardware cursor");
        };

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
                Some(CursorImage::Buffer {
                    handle,
                    width,
                    height,
                    hot_x,
                    hot_y,
                }) => {
                    let object_id = object_for_handle(&inner, handle)?;
                    let objects = GEM_OBJECTS.lock();
                    let object = object_by_id(&objects, object_id)?;
                    let pool = objects
                        .pool
                        .as_ref()
                        .ok_or_else(|| Error::with_message(Errno::ENOMEM, "no dumb buffer pool"))?;
                    let size = u32::try_from(object.size).map_err(|_| {
                        Error::with_message(Errno::EINVAL, "cursor buffer is too large")
                    })?;
                    Some(CursorScanoutBuffer::new(
                        Arc::clone(pool),
                        object.offset,
                        size,
                        CursorGeometry {
                            width,
                            height,
                            hot_x,
                            hot_y,
                        },
                        position,
                    ))
                }
                _ => None,
            };
            (update, position, backing)
        };

        let resource_id = match update.image {
            Some(CursorImage::Buffer { .. }) => {
                let buffer = backing.ok_or_else(|| {
                    Error::with_message(Errno::EINVAL, "cursor buffer has no backing")
                })?;
                Some(cursor.update_cursor(buffer)?)
            }
            Some(CursorImage::Hide) => {
                cursor.hide_cursor(position.x, position.y)?;
                None
            }
            None => {
                cursor.move_cursor(position.x, position.y)?;
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
        // The host keys 3D contexts by id for the device's lifetime, so one a
        // closed file left behind would outlive everything able to reach it.
        // Guarded rather than unwrapped: `drop` cannot report a failure, and a
        // file that never had a 3D context — which is every file on a machine
        // with only a firmware framebuffer — has nothing to clean up.
        let context_id = self.inner.lock().context_id.take();
        if let (Some(context_id), Some(gpu)) = (context_id, self.display.gpu.as_ref()) {
            let _ = gpu.context_destroy(context_id);
        }

        let _cursor_operation = self.cursor_operation.lock();
        let (resource_id, position) = {
            let inner = self.inner.lock();
            (inner.cursor.resource_id, inner.cursor.position)
        };
        if let (Some(resource_id), Some(cursor)) = (resource_id, self.display.cursor.as_ref()) {
            let _ = cursor.clear_cursor(resource_id, position.x, position.y);
        }
    }
}

impl Pollable for DriHandle {
    /// Reports this descriptor ready only when the driver has an event for it.
    ///
    /// Linux's `drm_poll` returns `EPOLLIN | EPOLLRDNORM` for a file with a
    /// queued event and nothing otherwise — never `EPOLLOUT`. Saying
    /// `mask & IoEvents::OUT` instead, as this did, claims the descriptor is
    /// writable at all times, and `epoll` always requests `EPOLLOUT` when the
    /// caller asks for it. A level-triggered readiness that can never be
    /// cleared makes `epoll_wait` return immediately, every time: the caller
    /// is not waiting, it is spinning, and it never gets to the work it was
    /// woken to do.
    ///
    /// That is what the X server does here. It watches this descriptor for
    /// display events, spins on the always-writable answer, and never services
    /// its clients again — a `glxinfo` left blocked on the X socket with the
    /// server apparently alive.
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        self.events.poll_with(mask, poller, || IoEvents::empty())
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
    /// Declares that this per-open object is an audited SCM_RIGHTS leaf.
    ///
    /// The default is `false` and must stay that way: a per-open device object
    /// may retain another [`FileLike`], and passing its descriptor to another
    /// process would then keep a file description alive behind the sender's
    /// back. So the default is overridden only where the fields have been
    /// checked, and here they have:
    ///
    /// - `node`, `DriNode` -- an enum, no ownership
    /// - `gpu`, `Arc<GpuDevice>` -- the device, not a file
    /// - `context_operation`, `cursor_operation`, `Mutex<()>`
    /// - `events`, `Pollee`
    /// - `inner`, `SpinLock<DriInner>`, whose fields are `BTreeMap<u32, u32>`,
    ///   `BTreeMap<u32, Framebuffer>` (`Framebuffer` is three `u32`s), two
    ///   `Option<u32>` and a `CursorState` (`Option<u32>`, `Option<u32>`,
    ///   `CursorPosition` -- the type is `Copy`).
    ///
    /// Nothing can hold a file description, so a client that receives this
    /// descriptor holds exactly what it was handed.
    ///
    /// This matters because it is how a GL client is given the DRM device at
    /// all: `glamor_dri3_open_client` opens the node and the reply carries the
    /// descriptor over the X socket. A DRM *dma-buf* was given the same
    /// treatment for the same reason; the device node itself was not, so a
    /// descriptor for it classified as `Unsupported` and the whole `sendmsg`
    /// was refused.
    fn is_scm_rights_proven_leaf(&self) -> bool {
        true
    }

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

        let traced_command = raw_ioctl.cmd();
        if DRI_TRACE.load(Ordering::Relaxed) {
            ostd::error!("DRI_IOCTL cmd={:#010x} arg={:#x}", raw_ioctl.cmd(), raw_ioctl.arg());
        }

        let result: Result<i32> = dispatch_ioctl!(match raw_ioctl {
            cmd @ GetVersion => {
                let mut version = cmd.read()?;
                version.version_major = 0;
                version.version_minor = 1;
                version.version_patchlevel = 0;
                copy_field(version.name, &mut version.name_len, self.display.name)?;
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
                    DRM_CAP_PRIME => DRM_PRIME_CAP_IMPORT | DRM_PRIME_CAP_EXPORT,
                    _ => {
                        return_errno_with_message!(Errno::EINVAL, "unsupported DRM capability")
                    }
                };
                cmd.write(&cap)?;
                Ok(0)
            }
            cmd @ GetMagic => {
                // `_IOR`: nothing comes in, the token goes out.
                let magic = {
                    let mut inner = self.inner.lock();
                    *inner.magic.get_or_insert_with(|| {
                        NEXT_MAGIC.fetch_add(1, core::sync::atomic::Ordering::Relaxed)
                    })
                };
                cmd.write(&DrmAuth { magic })?;
                Ok(0)
            }
            cmd @ AuthMagic => {
                let auth = cmd.read()?;
                // Linux looks the token up in the master's map and marks that
                // file authenticated. No ioctl on this device reads an
                // authenticated flag, so the half that does work is the
                // recognition -- a token this device issued is accepted and
                // anything else is refused, rather than the request quietly
                // succeeding on a value nothing produced.
                if auth.magic == 0
                    || auth.magic >= NEXT_MAGIC.load(core::sync::atomic::Ordering::Relaxed)
                {
                    return_errno_with_message!(Errno::EINVAL, "unknown magic");
                }
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
                let (max_width, max_height) = self.display.max_mode();
                res.min_width = 0;
                res.max_width = max_width;
                res.min_height = 0;
                res.max_height = max_height;
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
            cmd @ ModeGetPlaneResources => {
                // A single scanout is presented through a single plane, so a
                // client that enumerates planes finds exactly one. Reporting
                // none would be equally true of the hardware and useless to a
                // client trying to find out how it may drive it.
                let mut res = cmd.read()?;
                res.count_planes = 1;
                if res.plane_id_ptr != 0 {
                    current_userspace!().write_val(res.plane_id_ptr as usize, &PLANE_ID)?;
                }
                cmd.write(&res)?;
                Ok(0)
            }
            cmd @ ModeGetPlane => {
                let mut plane = cmd.read()?;
                if plane.plane_id != PLANE_ID {
                    return_errno_with_message!(Errno::EINVAL, "unknown plane id");
                }
                let inner = self.inner.lock();
                plane.crtc_id = CRTC_ID;
                plane.fb_id = inner.current_fb_id.unwrap_or(0);
                plane.possible_crtcs = 1;
                plane.gamma_size = 0;
                plane.count_format_types = 0;
                cmd.write(&plane)?;
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
                conn.connector_type = self.display.connector_type;
                conn.connector_type_id = 1;
                conn.connection = DRM_MODE_CONNECTED;
                conn.mm_width = 0;
                conn.mm_height = 0;
                conn.subpixel = 0;
                conn.pad = 0;
                if conn.modes_ptr != 0 && capacity >= 1 {
                    // The mode comes from the scanout, not from the GPU: a
                    // firmware framebuffer has a mode the GPU knows nothing
                    // about, and on a virtio-gpu machine the two agree.
                    let (width, height) = self.display.scanout.dimensions();
                    let mode = build_mode(width, height);
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
            cmd @ PrimeHandleToFd => {
                let mut req = cmd.read()?;
                let (file, fd_flags) = prime::handle_to_fd(self, req.handle, req.flags)?;
                req.fd = install_file(file, fd_flags)?;
                cmd.write(&req)?;
                Ok(0)
            }
            cmd @ PrimeFdToHandle => {
                let mut req = cmd.read()?;
                // The command carries no size field: Linux reports the handle
                // and leaves the rest as the caller sent it.
                req.handle = self.prime_fd_to_handle(req.fd)?;
                cmd.write(&req)?;
                Ok(0)
            }
            cmd @ VirtgpuGetparam => {
                let req = cmd.read()?;
                self.virtgpu_getparam(&req)?;
                Ok(0)
            }
            cmd @ VirtgpuGetCaps => {
                let req = cmd.read()?;
                self.virtgpu_get_caps(&req)?;
                Ok(0)
            }
            cmd @ VirtgpuContextInit => {
                let req = cmd.read()?;
                self.virtgpu_context_init(&req)?;
                Ok(0)
            }
            cmd @ VirtgpuExecbuffer => {
                let req = cmd.read()?;
                cmd.write(&self.virtgpu_execbuffer(&req)?)?;
                Ok(0)
            }
            cmd @ VirtgpuTransferToHost => {
                let req = cmd.read()?;
                self.virtgpu_transfer_3d(true, &req)?;
                Ok(0)
            }
            cmd @ VirtgpuTransferFromHost => {
                let req = cmd.read()?;
                self.virtgpu_transfer_3d(false, &req)?;
                Ok(0)
            }
            cmd @ VirtgpuWait => {
                let _req = cmd.read()?;
                // Nothing to wait for: `submit_3d` does not return until the
                // host has retired the work, so a resource is never still in
                // flight by the time a client can ask about it. Reporting that
                // as a no-op is the honest answer — the alternative is to
                // refuse a call that Linux serves, and Mesa prints the refusal
                // as "slow gpu or hang?" on every submission.
                Ok(0)
            }
            cmd @ VirtgpuMap => {
                let req = cmd.read()?;
                cmd.write(&self.virtgpu_map(&req)?)?;
                Ok(0)
            }
            cmd @ VirtgpuResourceCreate => {
                let req = cmd.read()?;
                cmd.write(&self.virtgpu_resource_create(&req)?)?;
                Ok(0)
            }
            cmd @ VirtgpuResourceInfo => {
                let req = cmd.read()?;
                cmd.write(&self.virtgpu_resource_info(&req)?)?;
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
                // `fb_id == 0` is the modesetting driver's *capability probe*
                // (it calls `drmModeDirtyFB(fd, fb_id, NULL, 0)` before the first
                // framebuffer exists). Returning success there keeps it on the
                // dirty-update path; the real presents carry a valid id.
                if req.fb_id == 0 {
                    return Ok(0);
                }
                // The clip rectangles decide how much is re-presented, and they
                // are read before the framebuffer is looked up so that a client
                // naming one that does not exist still gets EFAULT for its bad
                // pointer rather than EINVAL for the id it cannot fix first.
                let clips = read_dirty_clips(&req)?;
                self.dirty_fb(req.fb_id, &clips)?;
                Ok(0)
            }
            _ => {
                ostd::debug!(
                    "the ioctl command {:#x} is unknown for DRM devices",
                    raw_ioctl.cmd()
                );
                return_errno_with_message!(Errno::ENOTTY, "the ioctl command is unknown");
            }
        });

        // Only the refusals, and only after the fact: the line above already
        // records the sequence, and a client that stops early stops because of
        // one call the driver would not serve. `Errno` alone does not say
        // which.
        if DRI_TRACE.load(Ordering::Relaxed) && let Err(error) = &result {
            ostd::error!(
                "DRI_IOCTL REFUSED cmd={:#010x} errno={:?}",
                traced_command,
                error.error()
            );
        }
        result
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

/// Copies a driver string into a userspace buffer and updates the length field,
/// following Linux's `DRM_COPY` in `drm_version()`.
///
/// Two phases, driven by the caller. A null buffer (or a zero length) only
/// reports the length the string needs. A real buffer receives
/// `min(strlen + 1, len)` bytes of the string: the whole thing *and* its
/// terminator when there is room for both, and a bare truncation when there is
/// not.
///
/// The detail that matters is what happens when the buffer is exactly
/// `strlen` bytes. libdrm's `drmGetVersion()` allocates `name_len + 1` bytes
/// and writes `name[name_len] = '\0'` itself, so it hands the kernel a length
/// one *less* than the buffer. A kernel that treats `len` as the buffer size
/// and reserves a byte for the terminator therefore hands back a name one
/// character short: libdrm asks for ten bytes and gets `"virtio_gp"`. Mesa
/// reads that with `strndup(version->name, version->name_len)`, tries to load
/// `virtio_gp_dri.so`, fails, and falls back to software rendering -- which is
/// exactly what an accelerated guest looks like when the name loses its last
/// letter.
fn copy_field(dst: usize, len: &mut usize, src: &str) -> Result<()> {
    let src_bytes = src.as_bytes();
    if dst != 0 && *len > 0 {
        let copy = (src_bytes.len() + 1).min(*len);
        if copy > src_bytes.len() {
            current_userspace!().write_bytes(dst, src_bytes)?;
            current_userspace!().write_val(dst + src_bytes.len(), &0u8)?;
        } else {
            current_userspace!().write_bytes(dst, &src_bytes[..copy])?;
        }
    }
    *len = src_bytes.len();
    Ok(())
}

pub(super) fn init_in_first_kthread() {
    let Some(source) = display_source() else {
        return;
    };
    let display = match display_device(source) {
        Ok(display) => Arc::new(display),
        // A display was found but could not be driven. Registering a node that
        // cannot present would only move the failure to the first client that
        // tried to use it, with less to go on.
        Err(error) => {
            ostd::error!("Not registering DRM nodes: {:?}", error);
            return;
        }
    };

    char::register(Arc::new(Dri {
        node: DriNode::Card,
        display: Arc::clone(&display),
    }))
    .expect("failed to register the DRM card device");
    char::register(Arc::new(Dri {
        node: DriNode::Render,
        display,
    }))
    .expect("failed to register the DRM render device");
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::ioctl_defs::*;
    use super::*;
    use crate::util::ioctl::NoData;

    /// The command number and argument size the Linux uapi gives each ioctl
    /// this driver serves.
    ///
    /// These are not transcribed by hand. They are the output of
    /// `tools/riscv/perf/drm-uapi-contract.c`, which obtains them by including
    /// the real `<drm/drm.h>` / `<drm/virtgpu_drm.h>` and evaluating the
    /// `DRM_IOCTL_*` and `sizeof()` expressions those headers define -- so the
    /// expected values come from the uapi, not from the driver, and the test
    /// is not a mirror of the thing it checks.
    ///
    /// `tools/riscv/tests/test_drm_uapi_contract.py` re-derives the same table
    /// from the same headers and compares it against this one, so a constant
    /// here cannot drift from the uapi without a test saying so.
    ///
    /// The reason this matters is a defect class that has cost this tree four
    /// separate debugging cycles. An ioctl command number is not a name: it is
    /// `direction | size | type | number` packed into 32 bits. `ioc!`'s first
    /// argument is a label the macro never reads, so a declaration whose label
    /// says one ioctl and whose number says another compiles cleanly and then
    /// answers `ENOTTY` -- indistinguishable from not implementing it. That is
    /// how `DRM_IOCTL_GET_MAGIC` was silently missing for a whole cycle, and
    /// how `DRM_IOCTL_GEM_OPEN` came to be declared as `DRM_IOCTL_RM_MAP`.
    macro_rules! uapi_contract {
        ($( $name:ident : $number:expr, $argument:ty, $size:expr; )*) => {
            /// `(ioctl, command number, argument size)`.
            const CONTRACT: &[(&str, u32, u32)] = &[
                $( (stringify!($name), $number, $size), )*
            ];

            /// The declaration answers to the number libdrm actually sends.
            /// This is the whole contract: a caller does
            /// `ioctl(fd, DRM_IOCTL_X, ...)`, and the number in that call is
            /// built from the uapi's own definition of `DRM_IOCTL_X`.
            #[ktest]
            fn every_ioctl_answers_to_the_command_number_the_uapi_gives_it() {
                $(
                    assert!(
                        $name::try_from_raw(RawIoctl::new($number, 0)).is_some(),
                        concat!(
                            stringify!($name),
                            " does not answer to its uapi command number"
                        ),
                    );
                )*
            }

            /// The check above would also pass if the encoding ignored fields.
            /// Flipping the low bit of the number lands on a different `nr`
            /// (or, for the master pair, on its twin), and every one of those
            /// must be refused -- which is what makes the assertion above mean
            /// something.
            #[ktest]
            fn no_ioctl_answers_to_a_neighbouring_command_number() {
                $(
                    assert!(
                        $name::try_from_raw(RawIoctl::new($number ^ 0x01, 0)).is_none(),
                        concat!(
                            stringify!($name),
                            " also answers to a command number it does not own"
                        ),
                    );
                )*
            }

            /// The number encodes the argument size, so a struct that is not
            /// the uapi's size is not a cosmetic difference -- it changes the
            /// number the driver accepts. Asserting the size separately names
            /// the cause when that happens.
            #[ktest]
            fn every_argument_struct_is_the_size_the_uapi_declares() {
                $(
                    assert_eq!(
                        size_of::<$argument>(),
                        $size,
                        concat!(
                            stringify!($argument),
                            " does not match the uapi layout"
                        ),
                    );
                )*
            }
        };
    }

    uapi_contract! {
        GetVersion: 0xc0406400, DrmVersion, 64;
        GetCap: 0xc010640c, DrmGetCap, 16;
        GetMagic: 0x80046402, DrmAuth, 4;
        AuthMagic: 0x40046411, DrmAuth, 4;
        SetClientCap: 0x4010640d, DrmSetClientCap, 16;
        GemClose: 0x40086409, DrmGemClose, 8;
        GemFlink: 0xc008640a, DrmGemFlink, 8;
        GemOpen: 0xc010640b, DrmGemOpen, 16;
        PrimeHandleToFd: 0xc00c642d, DrmPrimeHandle, 12;
        PrimeFdToHandle: 0xc00c642e, DrmPrimeHandle, 12;
        SetMaster: 0x0000641e, NoData, 0;
        DropMaster: 0x0000641f, NoData, 0;
        VirtgpuMap: 0xc0106441, DrmVirtgpuMap, 16;
        VirtgpuExecbuffer: 0xc0406442, DrmVirtgpuExecbuffer, 64;
        VirtgpuGetparam: 0xc0106443, DrmVirtgpuGetparam, 16;
        VirtgpuResourceCreate: 0xc0386444, DrmVirtgpuResourceCreate, 56;
        VirtgpuResourceInfo: 0xc0106445, DrmVirtgpuResourceInfo, 16;
        VirtgpuGetCaps: 0xc0186449, DrmVirtgpuGetCaps, 24;
        VirtgpuTransferFromHost: 0xc02c6446, DrmVirtgpuTransfer3d, 44;
        VirtgpuTransferToHost: 0xc02c6447, DrmVirtgpuTransfer3d, 44;
        VirtgpuWait: 0xc0086448, DrmVirtgpuWait, 8;
        VirtgpuContextInit: 0xc010644b, DrmVirtgpuContextInit, 16;
        ModeGetResources: 0xc04064a0, DrmModeCardRes, 64;
        ModeGetCrtc: 0xc06864a1, DrmModeCrtc, 104;
        ModeSetCrtc: 0xc06864a2, DrmModeCrtc, 104;
        ModeCursor: 0xc01c64a3, DrmModeCursor, 28;
        ModeGetEncoder: 0xc01464a6, DrmModeGetEncoder, 20;
        ModeGetConnector: 0xc05064a7, DrmModeGetConnector, 80;
        ModeAddFb: 0xc01c64ae, DrmModeFbCmd, 28;
        ModePageFlip: 0xc01864b0, DrmModeCrtcPageFlip, 24;
        ModeDirtyFb: 0xc01864b1, DrmModeFbDirtyCmd, 24;
        ModeCreateDumb: 0xc02064b2, DrmModeCreateDumb, 32;
        ModeMapDumb: 0xc01064b3, DrmModeMapDumb, 16;
        ModeDestroyDumb: 0xc00464b4, DrmModeDestroyDumb, 4;
        ModeObjGetProperties: 0xc02064b9, DrmModeObjGetProperties, 32;
        ModeGetPlaneResources: 0xc01064b5, DrmModeGetPlaneRes, 16;
        ModeGetPlane: 0xc02064b6, DrmModeGetPlane, 32;
        ModeCursor2: 0xc02464bb, DrmModeCursor2, 36;
    }

    /// Two ioctls sharing a number would mean one of them is unreachable, and
    /// which one depends on dispatch order.
    #[ktest]
    fn no_two_ioctls_share_a_command_number() {
        for (index, (name, number, _)) in CONTRACT.iter().enumerate() {
            for (other_name, other_number, _) in &CONTRACT[index + 1..] {
                assert_ne!(
                    number, other_number,
                    "{name} and {other_name} declare the same command number"
                );
            }
        }
    }

    /// The four command numbers this driver has actually got wrong, pinned so
    /// they cannot come back.
    ///
    /// Each is a real defect that shipped, and each is a number that differs
    /// from the correct one by a single field. They are asserted here rather
    /// than described in a comment because the failures were all silent:
    /// libdrm's call returned `ENOTTY`, which reads as "not implemented"
    /// rather than "declared wrong".
    #[ktest]
    fn historically_mistaken_command_numbers_stay_rejected() {
        // Declared with `InOutData` where the uapi says `DRM_IOR`: the
        // direction is part of the number, so libdrm's GET_MAGIC did not
        // match. This is what made glamor answer DRI3Open with BadMatch.
        assert!(GetMagic::try_from_raw(RawIoctl::new(0xc0046402, 0)).is_none());
        // The same, for `DRM_IOW`.
        assert!(AuthMagic::try_from_raw(RawIoctl::new(0xc0046411, 0)).is_none());
        // Declared as `0x1b`, which is `DRM_IOCTL_RM_MAP` -- a removed legacy
        // ioctl -- rather than GEM_OPEN's `0x0b`, leaving `gem_open` and its
        // dispatch arm unreachable from userspace.
        assert!(GemOpen::try_from_raw(RawIoctl::new(0xc010641b, 0)).is_none());
        // Declared with the 16-byte size where `struct drm_prime_handle` is
        // 12 bytes.
        assert!(PrimeHandleToFd::try_from_raw(RawIoctl::new(0xc010642d, 0)).is_none());
    }
}
