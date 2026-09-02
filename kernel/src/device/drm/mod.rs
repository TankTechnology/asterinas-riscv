// SPDX-License-Identifier: MPL-2.0

//! DRM (Direct Rendering Manager) character device support.
//!
//! Provides two device nodes backed by the first discovered virtio-gpu device:
//!
//! - `/dev/dri/card0` (major=226, minor=0) — primary node with full KMS +
//!   dumb-buffer + GEM ioctls.
//! - `/dev/dri/renderD128` (major=226, minor=128) — render node with GEM,
//!   PRIME, virgl 3D, transfer, execution, and fence ioctls (no KMS).
//!
//! Dumb buffers are carved out of a single physically-contiguous [`Vmo`] pool
//! so that (a) `mmap` can map any buffer via the standard `Mappable::Vmo` path
//! and (b) each buffer is backed by one contiguous guest-physical span that
//! virtio-gpu's `RESOURCE_ATTACH_BACKING` accepts.

mod atomic;
mod cursor;
mod dumb;
mod fdinfo;
mod fence;
mod gem;
mod ioctl;
mod kms;
mod plane;
mod prime;
mod property;
mod queue;
mod resource_tracking;
mod syncobj;
mod uapi;
mod vblank;
mod virgl_resource;
mod virtio_gpu;

use core::{
    fmt::{Debug, Formatter},
    ops::{
        Bound::{Excluded, Included},
        Range,
    },
    sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering},
    time::Duration,
};

use aster_virtio::device::{
    VirtioDeviceError,
    gpu::{device::GpuDevice, first_device},
};
use device_id::{DeviceId, MajorId, MinorId};
use ostd::mm::{Paddr, VmIo};

#[cfg(ktest)]
use self::resource_tracking::VirglContextCounts;
use self::{
    cursor::{CURSOR_SIZE, CursorState, DrmModeCursor, DrmModeCursor2},
    queue::{AtomicCommitQueue, DrmEventQueue, VblankCompletionQueue},
    resource_tracking::{DrmResourceSnapshot, VirglContextTracker},
    uapi::*,
};
use crate::{
    context::current_userspace,
    device::{Device, DeviceType, DevtmpfsInodeMeta, registry::char},
    events::IoEvents,
    fs::{
        file::{
            GuardedVmoRange, Mappable, PerOpenFileOps, StatusFlags,
            file_table::{FileDesc, RawFileDesc, WithFileTable},
        },
        vfs::{inode::FileOps, path::Path},
    },
    prelude::*,
    process::{
        UserNamespace,
        credentials::capabilities::CapSet,
        posix_thread::{AsPosixThread, FileTableRefMut},
        signal::{PollHandle, Pollable},
    },
    security::lsm::hooks as lsm_hooks,
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
const DRIVER_DESC: &str = "Asterinas virtio-gpu 2D/3D driver";

/// Refresh rate advertised by the single synthesized virtio-gpu mode.
const DEFAULT_REFRESH_HZ: u32 = 60;
const HORIZONTAL_FRONT_PORCH: u32 = 16;
const HORIZONTAL_SYNC_WIDTH: u32 = 16;
const HORIZONTAL_BACK_PORCH: u32 = 16;
const VERTICAL_FRONT_PORCH: u32 = 1;
const VERTICAL_SYNC_WIDTH: u32 = 1;
const VERTICAL_BACK_PORCH: u32 = 2;

/// KMS object ids.
///
/// DRM mode objects share one global id namespace. Keeping these ids distinct
/// lets atomic requests resolve an object id to exactly one object type.
/// The virtio-gpu device exposes one object of each kind.
const CRTC_ID: u32 = 1;
const CONNECTOR_ID: u32 = 2;
const ENCODER_ID: u32 = 3;

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
const DRM_CAP_VBLANK_HIGH_CRTC: u64 = 2;
const DRM_CAP_DUMB_PREFERRED_DEPTH: u64 = 3;
const DRM_CAP_DUMB_PREFER_SHADOW: u64 = 4;
const DRM_CAP_TIMESTAMP_MONOTONIC: u64 = 6;
const DRM_CAP_CURSOR_WIDTH: u64 = 8;
const DRM_CAP_CURSOR_HEIGHT: u64 = 9;
const DRM_CAP_CRTC_IN_VBLANK_EVENT: u64 = 0x12;
const DRM_CAP_SYNCOBJ: u64 = 0x13;
const DRM_CAP_SYNCOBJ_TIMELINE: u64 = 0x14;
/// `DRM_CAP_PRIME`.
const DRM_CAP_PRIME: u64 = 0x5;
/// `DRM_PRIME_CAP_IMPORT | DRM_PRIME_CAP_EXPORT`.
const DRM_PRIME_CAP_IMPORT_EXPORT: u64 = 0x3;

/// `DRM_CLIENT_CAP_*` values accepted by `SET_CLIENT_CAP`.
const DRM_CLIENT_CAP_UNIVERSAL_PLANES: u64 = 2;
const DRM_CLIENT_CAP_ATOMIC: u64 = 3;

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
const DRM_EVENT_VBLANK: u32 = 0x01;
const DRM_EVENT_FLIP_COMPLETE: u32 = 0x02;
const DRM_EVENT_CRTC_SEQUENCE: u32 = 0x03;
/// Bounds page-flip events retained by one open DRM file.
const MAX_DRM_EVENTS: usize = 1024;
/// Bounds queued nonblocking atomic hardware updates per open DRM file.
const MAX_PENDING_ATOMIC_COMMITS: usize = 1024;

/// `DRM_PLANE_TYPE_PRIMARY` — the plane type enum value.
const DRM_PLANE_TYPE_PRIMARY: u32 = 1;

/// Our single primary plane id.
const PRIMARY_PLANE_ID: u32 = 4;

/// Legacy `DRM_IOCTL_WAIT_VBLANK` request bits.
const DRM_VBLANK_RELATIVE: u32 = 0x00000001;
const DRM_VBLANK_HIGH_CRTC_MASK: u32 = 0x0000003e;
const DRM_VBLANK_EVENT: u32 = 0x04000000;
const DRM_VBLANK_NEXT_ON_MISS: u32 = 0x10000000;
const DRM_VBLANK_SECONDARY: u32 = 0x20000000;
const DRM_VBLANK_SIGNAL: u32 = 0x40000000;
const DRM_VBLANK_SUPPORTED_MASK: u32 = DRM_VBLANK_RELATIVE
    | DRM_VBLANK_HIGH_CRTC_MASK
    | DRM_VBLANK_EVENT
    | DRM_VBLANK_NEXT_ON_MISS
    | DRM_VBLANK_SECONDARY
    | DRM_VBLANK_SIGNAL;

/// `DRM_IOCTL_CRTC_QUEUE_SEQUENCE` flags.
const DRM_CRTC_SEQUENCE_RELATIVE: u32 = 0x00000001;
const DRM_CRTC_SEQUENCE_NEXT_ON_MISS: u32 = 0x00000002;
const DRM_CRTC_SEQUENCE_SUPPORTED_MASK: u32 =
    DRM_CRTC_SEQUENCE_RELATIVE | DRM_CRTC_SEQUENCE_NEXT_ON_MISS;

/// Linux bounds a blocking legacy vblank wait to three seconds.
const VBLANK_WAIT_TIMEOUT: Duration = Duration::from_secs(3);

/// An atomic-capable KMS object exposed by this device.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum AtomicKmsObject {
    Crtc,
    Connector,
    PrimaryPlane,
}

impl AtomicKmsObject {
    const ALL: [Self; 3] = [Self::Crtc, Self::Connector, Self::PrimaryPlane];

    fn from_id(id: u32) -> Option<Self> {
        match id {
            CRTC_ID => Some(Self::Crtc),
            CONNECTOR_ID => Some(Self::Connector),
            PRIMARY_PLANE_ID => Some(Self::PrimaryPlane),
            _ => None,
        }
    }

    fn id(self) -> u32 {
        match self {
            Self::Crtc => CRTC_ID,
            Self::Connector => CONNECTOR_ID,
            Self::PrimaryPlane => PRIMARY_PLANE_ID,
        }
    }

    fn object_type(self) -> u32 {
        match self {
            Self::Crtc => DRM_MODE_OBJECT_CRTC,
            Self::Connector => DRM_MODE_OBJECT_CONNECTOR,
            Self::PrimaryPlane => DRM_MODE_OBJECT_PLANE,
        }
    }
}

/// Size of the single contiguous dumb-buffer pool, in bytes.
///
/// Covers framebuffers up to ~2048x2048 at 32 bpp; enough for the QEMU
/// virtio-gpu scanouts (1024x768 by default) and a generous multi-resolution
/// headroom. A single pool is required because the mmap path maps one
/// `Mappable::Vmo` per file and selects a buffer by its byte offset within it.
///
/// 64 MiB holds ~15 1280x800@32bpp buffers — enough for a GBM surface's
/// back/front/shadow buffers plus a few app allocations.
/// Page-granular spans are reused only after GEM, mmap, and host-resource
/// owners all release them.
const DUMB_POOL_SIZE: usize = 64 * 1024 * 1024;
const MAX_RESOURCE_FENCE_ASSOCIATIONS: u64 = 262_144;

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
    pool: Mutex<Option<Arc<Vmo>>>,
    /// Lifetime-aware allocator for page-aligned sub-ranges of `pool`.
    dumb_pool: Arc<dumb::DumbPool>,
    /// GEM objects by id. `object_id` is a monotonically increasing counter.
    gem_objects: SpinLock<BTreeMap<u32, Arc<GemObject>>>,
    /// Total owners across all entries in `gem_objects`.
    gem_references: AtomicU64,
    /// Global FLINK name → object_id.
    gem_names: SpinLock<BTreeMap<u32, u32>>,
    /// GEM `object_id` → live or cleanup-only virtio-gpu resource.
    gem_resources: SpinLock<BTreeMap<u32, GemResourceState>>,
    /// Host resources whose GEM objects are gone but whose cleanup must be retried.
    pending_resource_cleanup: SpinLock<BTreeSet<u32>>,
    /// Serializes the global GEM-object to host-resource transaction.
    resource_creation: Mutex<()>,
    /// Serializes EXECBUFFER resource capture with final GEM release.
    exec_resource_transaction: Mutex<()>,
    next_gem_id: AtomicU32,
    /// Device-wide framebuffer object ID allocator.
    next_framebuffer_id: AtomicU32,
    /// Monotonic virgl context id allocator (context id 0 is reserved).
    next_context_id: AtomicU32,
    /// Host-created virgl contexts and their attached resources.
    virgl_contexts: VirglContextTracker,
    /// Property manager for atomic modesetting.
    property_manager: property::PropertyManager,
    /// Display-refresh sequence and timestamp source for KMS completions.
    vblank_clock: vblank::VblankClock,
    /// Monotonic virtio-gpu fence id allocator (3D SUBMIT_3D fences).
    next_fence_id: AtomicU64,
    /// Tracked asynchronous command fences associated with each GEM object.
    resource_fences: SpinLock<BTreeMap<u32, Vec<Arc<fence::Fence>>>>,
    /// Total retained entries across all `resource_fences` vectors.
    fence_associations: AtomicU64,
    /// Device-wide fence set used as a conservative lifetime barrier.
    ///
    /// Virgl command streams are opaque, so userspace may omit a referenced
    /// resource from the BO list. Final resource and context destruction wait
    /// on this set even when no per-object association was supplied.
    tracked_fences: SpinLock<Vec<Arc<fence::Fence>>>,
    /// Device-wide DRM-master and KMS transaction state.
    kms_state: Mutex<KmsState>,
    /// Legacy primary-node authentication magic to per-file auth state.
    auth_magics: SpinLock<BTreeMap<u32, Weak<AtomicBool>>>,
    next_auth_magic: AtomicU32,
    next_file_id: AtomicU64,
}

/// A VMA-owned GEM reference; the final split/forked mapping releases it.
struct GemMappingLifetime {
    gpu_manager: Arc<GpuManager>,
    object_id: u32,
}

impl Debug for GemMappingLifetime {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> core::fmt::Result {
        formatter
            .debug_struct("GemMappingLifetime")
            .field("object_id", &self.object_id)
            .finish()
    }
}

impl Drop for GemMappingLifetime {
    fn drop(&mut self) {
        if let Err(error) = self.gpu_manager.release_gem_object(self.object_id) {
            warn!(
                "cannot release GEM object {} after its final mapping: {:?}",
                self.object_id, error
            );
        }
    }
}

#[derive(Debug)]
struct KmsState {
    /// Primary-node file id that currently owns DRM master.
    master_file_id: Option<u64>,
    /// Framebuffer currently driving the device scanout.
    scanout: Option<ActiveFramebuffer>,
    current_width: u32,
    current_height: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct ActiveFramebuffer {
    owner_file_id: u64,
    fb_id: u32,
}

impl KmsState {
    fn new(width: u32, height: u32) -> Self {
        Self {
            master_file_id: None,
            scanout: None,
            current_width: width,
            current_height: height,
        }
    }

    fn is_master(&self, file_id: u64) -> bool {
        self.master_file_id == Some(file_id)
    }

    fn scanout_matches(&self, file_id: u64, fb_id: u32) -> bool {
        self.scanout
            == Some(ActiveFramebuffer {
                owner_file_id: file_id,
                fb_id,
            })
    }

    fn scanout_owned_by(&self, file_id: u64) -> bool {
        self.scanout
            .is_some_and(|scanout| scanout.owner_file_id == file_id)
    }

    fn crtc_snapshot_for(&self, file_id: u64) -> (Option<u32>, bool, u32, u32) {
        let fb_id = self
            .scanout
            .filter(|scanout| scanout.owner_file_id == file_id)
            .map(|scanout| scanout.fb_id);
        (
            fb_id,
            self.scanout.is_some(),
            self.current_width,
            self.current_height,
        )
    }

    fn commit_scanout(&mut self, file_id: u64, fb_id: u32, width: u32, height: u32) {
        self.scanout = Some(ActiveFramebuffer {
            owner_file_id: file_id,
            fb_id,
        });
        self.current_width = width;
        self.current_height = height;
    }
}

impl GpuManager {
    fn new(gpu: Arc<GpuDevice>) -> Self {
        let (width, height) = (gpu.width(), gpu.height());
        Self {
            gpu,
            pool: Mutex::new(None),
            dumb_pool: dumb::DumbPool::new(DUMB_POOL_SIZE),
            gem_objects: SpinLock::new(BTreeMap::new()),
            gem_references: AtomicU64::new(0),
            gem_names: SpinLock::new(BTreeMap::new()),
            gem_resources: SpinLock::new(BTreeMap::new()),
            pending_resource_cleanup: SpinLock::new(BTreeSet::new()),
            resource_creation: Mutex::new(()),
            exec_resource_transaction: Mutex::new(()),
            next_gem_id: AtomicU32::new(1),
            next_framebuffer_id: AtomicU32::new(1),
            next_context_id: AtomicU32::new(1),
            virgl_contexts: VirglContextTracker::new(),
            property_manager: property::PropertyManager::new(),
            vblank_clock: vblank::VblankClock::new(),
            next_fence_id: AtomicU64::new(1),
            resource_fences: SpinLock::new(BTreeMap::new()),
            fence_associations: AtomicU64::new(0),
            tracked_fences: SpinLock::new(Vec::new()),
            kms_state: Mutex::new(KmsState::new(width, height)),
            auth_magics: SpinLock::new(BTreeMap::new()),
            next_auth_magic: AtomicU32::new(1),
            next_file_id: AtomicU64::new(1),
        }
    }

    /// Disables scanout and its display clock as one backend transition.
    fn disable_scanout(&self) -> Result<()> {
        self.gpu
            .disable_scanout()
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu disable failed"))?;
        self.vblank_clock.stop();
        Ok(())
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

    /// Allocates a framebuffer ID from the device-wide mode-object namespace.
    fn allocate_framebuffer_id(&self) -> Result<u32> {
        self.next_framebuffer_id
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |id| id.checked_add(1))
            .map_err(|_| Error::with_message(Errno::ENOSPC, "framebuffer IDs exhausted"))
    }

    /// Takes a diagnostic snapshot from the authoritative resource containers.
    ///
    /// The snapshot is intended for quiescent leak tests. Concurrent operations
    /// may advance individual containers between lock acquisitions.
    fn resource_snapshot(&self) -> DrmResourceSnapshot {
        let dumb_pool_usage = self.dumb_pool.usage();
        let gem_object_count = self.gem_objects.lock().len();

        let gem_resources = self.gem_resources.lock();
        let live_host_resources = gem_resources
            .values()
            .filter(|state| matches!(state, GemResourceState::Live(_)))
            .count();
        let cleanup_only_host_resources = gem_resources.len() - live_host_resources;
        drop(gem_resources);

        let context_counts = self.virgl_contexts.counts();
        let backend = self.gpu.resource_snapshot();
        DrmResourceSnapshot {
            dumb_pool_used_bytes: dumb_pool_usage.used_bytes(),
            dumb_pool_high_water_bytes: dumb_pool_usage.high_water_bytes(),
            dumb_pool_capacity_bytes: DUMB_POOL_SIZE,
            gem_objects: gem_object_count,
            gem_references: self.gem_references.load(Ordering::Relaxed),
            flink_names: self.gem_names.lock().len(),
            live_host_resources,
            cleanup_only_host_resources,
            pending_resource_cleanup: self.pending_resource_cleanup.lock().len(),
            virgl_contexts: context_counts.contexts,
            context_attachments: context_counts.attachments,
            pending_context_cleanup: context_counts.pending_cleanup,
            tracked_fences: self.tracked_fences.lock().len(),
            fence_associations: self.fence_associations.load(Ordering::Relaxed),
            backend_backing_owners: backend.backing_owners(),
            backend_pending_cleanup: backend.pending_cleanup(),
            scanout_resources: backend.scanout_resources(),
            cursor_resources: backend.cursor_resources(),
        }
    }

    /// Adds one owner to an existing GEM object and returns its buffer.
    fn retain_gem_object(&self, object_id: u32) -> Result<DumbBuffer> {
        let objects = self.gem_objects.lock();
        let object = objects
            .get(&object_id)
            .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;
        let references = object.ref_count.load(Ordering::Relaxed);
        let references = references.checked_add(1).ok_or_else(|| {
            Error::with_message(Errno::EOVERFLOW, "GEM reference count overflows")
        })?;
        object.ref_count.store(references, Ordering::Relaxed);
        self.gem_references.fetch_add(1, Ordering::Relaxed);
        Ok(object.buffer.clone())
    }

    /// Retains one GEM object for a mapping that may outlive every file handle.
    fn retain_gem_mapping(
        self: &Arc<Self>,
        object_id: u32,
    ) -> Result<(DumbBuffer, Arc<GemMappingLifetime>)> {
        let buffer = self.retain_gem_object(object_id)?;
        let lifetime = Arc::new(GemMappingLifetime {
            gpu_manager: self.clone(),
            object_id,
        });
        Ok((buffer, lifetime))
    }

    /// Drops one GEM owner and destroys global state after the final reference.
    fn release_gem_object(&self, object_id: u32) -> Result<()> {
        self.release_gem_object_and_report_cleanup(object_id)
            .map(|_| ())
    }

    /// Drops one GEM owner and reports whether final host cleanup was confirmed.
    fn release_gem_object_and_report_cleanup(&self, object_id: u32) -> Result<HostCleanupStatus> {
        let _transaction = self.exec_resource_transaction.lock();
        let released = {
            let mut objects = self.gem_objects.lock();
            let object = objects
                .get(&object_id)
                .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;
            let references = object.ref_count.load(Ordering::Relaxed);
            if references == 0 {
                return_errno_with_message!(Errno::EINVAL, "GEM object has no references");
            }
            if references > 1 {
                object.ref_count.store(references - 1, Ordering::Relaxed);
                self.gem_references.fetch_sub(1, Ordering::Relaxed);
                return Ok(HostCleanupStatus::Confirmed);
            }
            objects.remove(&object_id).unwrap()
        };
        self.gem_references.fetch_sub(1, Ordering::Relaxed);

        let name = released.name.load(Ordering::Relaxed);
        if name != 0 {
            let mut names = self.gem_names.lock();
            if names.get(&name) == Some(&object_id) {
                names.remove(&name);
            }
        }

        self.wait_for_all_fences();
        let resource = self.gem_resources.lock().remove(&object_id);
        let fences = self
            .resource_fences
            .lock()
            .remove(&object_id)
            .unwrap_or_default();
        self.fence_associations
            .fetch_sub(fences.len() as u64, Ordering::Relaxed);
        for fence in fences {
            if let Err(error) = fence.wait() {
                warn!(
                    "virtio-gpu fence failed before releasing GEM object {}: {:?}",
                    object_id, error
                );
            }
        }
        let mut cleanup_status = HostCleanupStatus::Confirmed;
        if let Some(resource_id) = resource.map(GemResourceState::resource_id)
            && let Err(error) = self.gpu.resource_unref(resource_id)
        {
            warn!(
                "cannot release virtio-gpu resource {} for GEM object {}: {:?}",
                resource_id, object_id, error
            );
            cleanup_status = HostCleanupStatus::Unconfirmed;
            self.pending_resource_cleanup.lock().insert(resource_id);
        }
        Ok(cleanup_status)
    }

    fn has_gem_resource(&self, object_id: u32) -> bool {
        self.gem_resources.lock().contains_key(&object_id)
    }

    fn live_gem_resource(&self, object_id: u32) -> Option<u32> {
        self.gem_resources
            .lock()
            .get(&object_id)
            .and_then(|state| state.live_resource_id())
    }

    fn live_gem_resource_metadata(
        &self,
        object_id: u32,
    ) -> Option<virgl_resource::LiveGemResource> {
        self.gem_resources
            .lock()
            .get(&object_id)
            .and_then(|state| state.live_resource())
    }

    fn insert_gem_resource(&self, object_id: u32, state: GemResourceState) {
        let previous = self.gem_resources.lock().insert(object_id, state);
        debug_assert!(previous.is_none());
    }

    fn allocate_fence_id(&self) -> Result<u64> {
        self.next_fence_id
            .try_update(Ordering::Relaxed, Ordering::Relaxed, next_fence_id)
            .map_err(|_| Error::with_message(Errno::ENOSPC, "virtio-gpu fence ids exhausted"))
    }

    /// Reserves all vector storage required to associate one submitted fence.
    ///
    /// The caller holds `exec_resource_transaction`, so final GEM release and
    /// another execbuffer cannot invalidate these reservations before commit.
    fn reserve_resource_fence_associations(&self, object_ids: &[u32]) -> Result<()> {
        let mut tracked = self.tracked_fences.lock();
        tracked.retain(|previous| !previous.is_signaled());
        tracked
            .try_reserve(1)
            .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot reserve tracked GPU fence"))?;
        drop(tracked);

        let mut resource_fences = self.resource_fences.lock();
        let mut removed_count = 0usize;
        for object_id in object_ids {
            let fences = resource_fences
                .get_mut(object_id)
                .expect("live GEM object has no fence tracking entry");
            let previous_len = fences.len();
            fences.retain(|previous| !previous.is_signaled());
            removed_count += previous_len - fences.len();
        }
        self.fence_associations
            .fetch_sub(removed_count as u64, Ordering::Relaxed);
        let retained = self
            .fence_associations
            .load(Ordering::Relaxed)
            .checked_add(object_ids.len() as u64)
            .ok_or_else(|| Error::with_message(Errno::ENOSPC, "GPU fence limit overflows"))?;
        if retained > MAX_RESOURCE_FENCE_ASSOCIATIONS {
            return_errno_with_message!(Errno::ENOSPC, "too many retained resource GPU fences");
        }
        for object_id in object_ids {
            let fences = resource_fences
                .get_mut(object_id)
                .expect("live GEM object has no fence tracking entry");
            fences.try_reserve(1).map_err(|_| {
                Error::with_message(Errno::ENOMEM, "cannot reserve resource GPU fence")
            })?;
        }
        Ok(())
    }

    /// Publishes a fence using storage reserved before device submission.
    fn associate_resource_fence(&self, object_ids: &[u32], fence: &Arc<fence::Fence>) {
        let mut tracked = self.tracked_fences.lock();
        debug_assert!(tracked.len() < tracked.capacity());
        tracked.push(fence.clone());
        drop(tracked);

        let mut resource_fences = self.resource_fences.lock();
        for object_id in object_ids {
            let fences = resource_fences
                .get_mut(object_id)
                .expect("live GEM object has no fence tracking entry");
            debug_assert!(fences.len() < fences.capacity());
            fences.push(fence.clone());
        }
        self.fence_associations
            .fetch_add(object_ids.len() as u64, Ordering::Relaxed);
    }

    fn resource_fences(&self, object_id: u32) -> Vec<Arc<fence::Fence>> {
        self.resource_fences
            .lock()
            .get(&object_id)
            .cloned()
            .unwrap_or_default()
    }

    fn clear_resource_fences(&self, object_id: u32, completed: &[Arc<fence::Fence>]) {
        let mut resource_fences = self.resource_fences.lock();
        let Some(current) = resource_fences.get_mut(&object_id) else {
            return;
        };
        let previous_len = current.len();
        current.retain(|fence| {
            !completed
                .iter()
                .any(|completed| Arc::ptr_eq(fence, completed))
        });
        let removed_count = previous_len - current.len();
        self.fence_associations
            .fetch_sub(removed_count as u64, Ordering::Relaxed);
    }

    fn wait_for_all_fences(&self) {
        let fences = self.tracked_fences.lock().clone();
        for fence in &fences {
            if let Err(error) = fence.wait() {
                warn!(
                    "virtio-gpu fence failed before lifetime teardown: {:?}",
                    error
                );
            }
        }
        let mut tracked = self.tracked_fences.lock();
        tracked.retain(|current| {
            !fences
                .iter()
                .any(|completed| Arc::ptr_eq(current, completed))
        });
    }

    /// Retries resources that outlived their GEM objects without holding a spinlock.
    fn drain_pending_resource_cleanup(&self) {
        retry_pending_ids(&self.pending_resource_cleanup, |resource_id| {
            self.gpu.resource_unref(resource_id).is_ok()
        });
    }

    /// Retries context destruction before retrying attached host resources.
    fn drain_pending_context_cleanup(&self) {
        self.virgl_contexts
            .retry_pending(|context_id| self.gpu.ctx_destroy(context_id).is_ok());
    }
}

fn next_fence_id(current: u64) -> Option<u64> {
    current.checked_add(1)
}

fn retry_pending_ids(
    pending_ids: &SpinLock<BTreeSet<u32>>,
    mut try_cleanup: impl FnMut(u32) -> bool,
) {
    let Some(last_id) = pending_ids.lock().last().copied() else {
        return;
    };
    let mut next_id = pending_ids.lock().first().copied();
    while let Some(id) = next_id {
        if try_cleanup(id) {
            pending_ids.lock().remove(&id);
        }
        if id == last_id {
            return;
        }
        next_id = pending_ids
            .lock()
            .range((Excluded(id), Included(last_id)))
            .next()
            .copied();
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum GemResourceState {
    Live(virgl_resource::LiveGemResource),
    CleanupOnly(u32),
}

impl GemResourceState {
    fn resource_id(self) -> u32 {
        match self {
            Self::Live(resource) => resource.create.resource_id,
            Self::CleanupOnly(resource_id) => resource_id,
        }
    }

    fn live_resource_id(self) -> Option<u32> {
        match self {
            Self::Live(resource) => Some(resource.create.resource_id),
            Self::CleanupOnly(_) => None,
        }
    }

    fn live_resource(self) -> Option<virgl_resource::LiveGemResource> {
        match self {
            Self::Live(resource) => Some(resource),
            Self::CleanupOnly(_) => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum HostCleanupStatus {
    Confirmed,
    Unconfirmed,
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn failed_resource_cleanup_does_not_block_later_resources() {
        // Regression for the cleanup starvation found while reviewing `9fc8a0824`.
        let pending_resources = SpinLock::new(BTreeSet::from([10, 11]));
        let mut attempted_resources = Vec::new();

        retry_pending_ids(&pending_resources, |resource_id| {
            attempted_resources.push(resource_id);
            if resource_id == 10 {
                pending_resources.lock().insert(12);
            }
            resource_id == 11
        });

        assert_eq!(attempted_resources, vec![10, 11]);
        assert_eq!(*pending_resources.lock(), BTreeSet::from([10, 12]));
    }

    #[ktest]
    fn fence_ids_do_not_wrap() {
        assert_eq!(next_fence_id(1), Some(2));
        assert_eq!(next_fence_id(u64::MAX), None);
    }

    #[ktest]
    fn synthesized_mode_pixel_clock_matches_refresh_rate() {
        let mode = build_mode(1280, 800);
        let total_pixels = u64::from(mode.htotal) * u64::from(mode.vtotal);
        let expected_hz = total_pixels * u64::from(DEFAULT_REFRESH_HZ);
        let pixel_clock_hz = u64::from(mode.clock) * 1000;

        assert!(expected_hz.abs_diff(pixel_clock_hz) < 1000);
        assert_eq!(mode.vrefresh, DEFAULT_REFRESH_HZ);
    }

    #[ktest]
    fn context_cleanup_remains_observable_until_confirmed() {
        let tracker = VirglContextTracker::new();
        tracker.record_created(7);
        tracker.record_attachment(7, 11);
        tracker.record_attachment(7, 12);
        assert_eq!(
            tracker.counts(),
            VirglContextCounts {
                contexts: 1,
                attachments: 2,
                pending_cleanup: 0,
            }
        );

        tracker.defer_destroy(7);
        tracker.retry_pending(|context_id| {
            assert_eq!(context_id, 7);
            false
        });
        assert_eq!(tracker.counts().pending_cleanup, 1);

        tracker.retry_pending(|context_id| context_id == 7);
        assert_eq!(tracker.counts(), VirglContextCounts::default());
    }

    #[ktest]
    fn legacy_page_flip_reservation_rejects_overlap_and_releases_on_drop() {
        let is_pending = Arc::new(AtomicBool::new(false));
        let reservation = LegacyPageFlipReservation::reserve(is_pending.clone()).unwrap();

        assert!(LegacyPageFlipReservation::reserve(is_pending.clone()).is_err());
        drop(reservation);
        assert!(LegacyPageFlipReservation::reserve(is_pending).is_ok());
    }
}

/// A dumb buffer: a page-aligned sub-range of the shared pool.
#[derive(Clone, Debug)]
struct DumbBuffer {
    offset: usize,
    size: usize,
    width: u32,
    height: u32,
    bpp: u32,
    allocation: Arc<dumb::PoolAllocation>,
}

impl DumbBuffer {
    fn mapped_range(&self) -> Option<Range<usize>> {
        Some(self.offset..self.offset.checked_add(self.allocation.size_bytes())?)
    }
}

/// A registered framebuffer referencing a dumb buffer.
#[derive(Clone, Copy, Debug)]
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
#[derive(Clone, Copy, Eq, PartialEq)]
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
        Ok(Box::new(DriHandle::new(
            self.gpu_manager.clone(),
            DriNodeType::Primary,
        )?))
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
        Ok(Box::new(DriHandle::new(
            self.gpu_manager.clone(),
            DriNodeType::Render,
        )?))
    }
}

// ---------------------------------------------------------------------------
// Per-open-file handle
// ---------------------------------------------------------------------------

/// Per-open-file DRM state.
///
/// GEM/dumb-buffer handles are namespaced per file.
/// Framebuffer IDs come from a device-wide namespace but ownership remains
/// attached to the creating file.
/// The pool and GEM object table are shared across all opens via [`GpuManager`].
struct DriHandle {
    gpu_manager: Arc<GpuManager>,
    node_type: DriNodeType,
    file_id: u64,
    owner_pid: u32,
    was_master: AtomicBool,
    authenticated: Arc<AtomicBool>,
    /// Whether this file enabled `DRM_CLIENT_CAP_UNIVERSAL_PLANES`.
    universal_planes: AtomicBool,
    /// Whether this file enabled `DRM_CLIENT_CAP_ATOMIC`.
    atomic_modesetting: AtomicBool,
    /// Legacy virgl context associated with this open DRM file.
    context: Mutex<VirglContext>,
    /// Serializes validation, device updates, and per-file cursor state.
    cursor_operation: Mutex<()>,
    /// Serializes event dequeue/copy/requeue transactions between readers.
    event_read_operation: Mutex<()>,
    /// Serializes page-flip event capacity checks with hardware commits.
    page_flip_operation: Mutex<()>,
    /// Prevents more than one legacy page flip from targeting one refresh.
    is_legacy_page_flip_pending: Arc<AtomicBool>,
    /// Keeps nonblocking atomic commits ordered with other KMS operations.
    atomic_commit_queue: Arc<AtomicCommitQueue>,
    /// Delivers this file's KMS and sequence events with one vblank worker.
    vblank_completion_queue: Arc<VblankCompletionQueue>,
    inner: SpinLock<DriInner>,
    event_queue: Arc<DrmEventQueue>,
}

/// Keeps one legacy page flip pending until its target refresh completes.
struct LegacyPageFlipReservation {
    is_pending: Arc<AtomicBool>,
}

impl LegacyPageFlipReservation {
    fn reserve(is_pending: Arc<AtomicBool>) -> Result<Self> {
        is_pending
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| {
                Error::with_message(Errno::EBUSY, "a legacy page flip is still pending")
            })?;
        Ok(Self { is_pending })
    }
}

impl Drop for LegacyPageFlipReservation {
    fn drop(&mut self) {
        self.is_pending.store(false, Ordering::Release);
    }
}

#[derive(Debug)]
struct VirglContext {
    id: u32,
    is_created: bool,
    /// Set after an ambiguous host-side context operation.
    is_poisoned: bool,
}

#[derive(Debug)]
struct DriInner {
    /// Legacy authentication token allocated by `DRM_IOCTL_GET_MAGIC`.
    auth_magic: Option<u32>,
    /// Per-file handle → GEM object_id.
    handles: BTreeMap<u32, u32>,
    /// Published and reserved per-file handles for each GEM object.
    object_handle_counts: BTreeMap<u32, usize>,
    next_handle: u32,
    /// Per-file DRM synchronization-object handle table.
    syncobjs: BTreeMap<u32, Arc<syncobj::SyncObject>>,
    next_syncobj_handle: u32,
    framebuffers: BTreeMap<u32, Framebuffer>,
    /// Cursor resource and position owned by this open DRM file.
    cursor: CursorState,
}

impl DriHandle {
    /// Resolves a per-file GEM handle to its device-wide object id.
    fn object_id_for_handle(&self, gem_handle: u32) -> Result<u32> {
        self.inner
            .lock()
            .handles
            .get(&gem_handle)
            .copied()
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))
    }

    fn new(gpu_manager: Arc<GpuManager>, node_type: DriNodeType) -> Result<Self> {
        let context_id = gpu_manager
            .next_context_id
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |id| id.checked_add(1))
            .map_err(|_| Error::with_message(Errno::ENOSPC, "virgl context ids exhausted"))?;
        let file_id = gpu_manager
            .next_file_id
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |id| id.checked_add(1))
            .map_err(|_| Error::with_message(Errno::ENOSPC, "DRM file ids exhausted"))?;
        let is_authenticated = if matches!(node_type, DriNodeType::Primary) {
            let mut kms_state = gpu_manager.kms_state.lock();
            *kms_state.master_file_id.get_or_insert(file_id) == file_id
        } else {
            true
        };
        let vblank_completion_queue = Arc::new(VblankCompletionQueue::new(gpu_manager.clone()));
        Ok(Self {
            gpu_manager,
            node_type,
            file_id,
            owner_pid: current!().pid(),
            was_master: AtomicBool::new(is_authenticated),
            authenticated: Arc::new(AtomicBool::new(is_authenticated)),
            universal_planes: AtomicBool::new(false),
            atomic_modesetting: AtomicBool::new(false),
            context: Mutex::new(VirglContext {
                id: context_id,
                is_created: false,
                is_poisoned: false,
            }),
            cursor_operation: Mutex::new(()),
            event_read_operation: Mutex::new(()),
            page_flip_operation: Mutex::new(()),
            is_legacy_page_flip_pending: Arc::new(AtomicBool::new(false)),
            atomic_commit_queue: Arc::new(AtomicCommitQueue::new()),
            vblank_completion_queue,
            inner: SpinLock::new(DriInner {
                auth_magic: None,
                handles: BTreeMap::new(),
                object_handle_counts: BTreeMap::new(),
                next_handle: 1,
                syncobjs: BTreeMap::new(),
                next_syncobj_handle: 1,
                framebuffers: BTreeMap::new(),
                cursor: CursorState::default(),
            }),
            event_queue: Arc::new(DrmEventQueue::new()),
        })
    }

    /// Returns true if KMS ioctls are forbidden on this handle.
    fn is_render_node(&self) -> bool {
        matches!(self.node_type, DriNodeType::Render)
    }

    fn lock_kms_as_master(&self) -> Result<MutexGuard<'_, KmsState>> {
        if self.is_render_node() {
            return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
        }
        let kms_state = self.gpu_manager.kms_state.lock();
        if !kms_state.is_master(self.file_id) {
            return_errno_with_message!(Errno::EACCES, "DRM master is owned by another file");
        }
        Ok(kms_state)
    }

    fn set_master(&self) -> Result<()> {
        if self.is_render_node() {
            return_errno_with_message!(Errno::EOPNOTSUPP, "render nodes cannot become DRM master");
        }
        self.check_master_permission()?;

        let mut kms_state = self.gpu_manager.kms_state.lock();
        match kms_state.master_file_id {
            None => {
                kms_state.master_file_id = Some(self.file_id);
            }
            Some(owner) if owner == self.file_id => {}
            Some(_) => return_errno_with_message!(Errno::EBUSY, "DRM master is already owned"),
        }
        self.was_master.store(true, Ordering::Release);
        self.authenticated.store(true, Ordering::Release);
        Ok(())
    }

    fn drop_master(&self) -> Result<()> {
        self.check_master_permission()?;

        let _page_flip_operation = self.page_flip_operation.lock();
        self.atomic_commit_queue.ensure_idle()?;
        let mut kms_state = self.gpu_manager.kms_state.lock();
        if !kms_state.is_master(self.file_id) {
            return_errno_with_message!(Errno::EINVAL, "file does not own DRM master");
        }
        kms_state.master_file_id = None;
        Ok(())
    }

    fn check_master_permission(&self) -> Result<()> {
        if self.was_master.load(Ordering::Acquire) && current!().pid() == self.owner_pid {
            return Ok(());
        }

        let thread = current_thread!();
        let posix_thread = thread.as_posix_thread().ok_or_else(|| {
            Error::with_message(
                Errno::EACCES,
                "DRM master operation requires a POSIX thread",
            )
        })?;
        let init_user_ns = UserNamespace::get_init_singleton();
        // Linux returns EACCES for DRM master permission failures; map the
        // capability LSM's EPERM to match userspace expectations.
        lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
            init_user_ns.as_ref(),
            posix_thread,
            CapSet::SYS_ADMIN,
        ))
        .map_err(|_| {
            Error::with_message(
                Errno::EACCES,
                "the caller does not have DRM master permission",
            )
        })
    }

    fn check_authenticated(&self) -> Result<()> {
        if self.is_render_node() || !self.authenticated.load(Ordering::Acquire) {
            return_errno_with_message!(
                Errno::EACCES,
                "ioctl requires an authenticated primary DRM client"
            );
        }
        Ok(())
    }

    fn get_auth_magic(&self) -> Result<u32> {
        if self.is_render_node() {
            return_errno_with_message!(
                Errno::EOPNOTSUPP,
                "authentication is not available on render nodes"
            );
        }

        let mut inner = self.inner.lock();
        if let Some(magic) = inner.auth_magic {
            return Ok(magic);
        }
        let magic = self
            .gpu_manager
            .next_auth_magic
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |magic| {
                magic.checked_add(1)
            })
            .map_err(|_| Error::with_message(Errno::ENOSPC, "DRM auth magics exhausted"))?;
        self.gpu_manager
            .auth_magics
            .lock()
            .insert(magic, Arc::downgrade(&self.authenticated));
        inner.auth_magic = Some(magic);
        Ok(magic)
    }

    fn authenticate_magic(&self, magic: u32) -> Result<()> {
        let _kms_state = self.lock_kms_as_master()?;
        let authenticated = self
            .gpu_manager
            .auth_magics
            .lock()
            .remove(&magic)
            .and_then(|state| state.upgrade())
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown DRM auth magic"))?;
        authenticated.store(true, Ordering::Release);
        Ok(())
    }

    fn ensure_virgl_context_locked(&self, context: &mut VirglContext) -> Result<u32> {
        if context.is_poisoned {
            return_errno_with_message!(Errno::EIO, "virgl context is no longer usable");
        }
        if !context.is_created {
            self.gpu_manager.drain_pending_context_cleanup();
            // `VIRTIO_GPU_F_CONTEXT_INIT` is not negotiated, so the legacy
            // context-create payload must leave `context_init` at zero.
            let create_result = self
                .gpu_manager
                .gpu
                .ctx_create(context.id, 0, b"asterinas-drm");
            if matches!(create_result, Err(VirtioDeviceError::AmbiguousCompletion)) {
                self.gpu_manager.virgl_contexts.record_created(context.id);
                if self.gpu_manager.gpu.ctx_destroy(context.id).is_ok() {
                    self.gpu_manager.virgl_contexts.record_destroyed(context.id);
                } else {
                    self.gpu_manager.virgl_contexts.defer_destroy(context.id);
                }
                context.is_poisoned = true;
                return_errno_with_message!(
                    Errno::EIO,
                    "virgl context creation completion is ambiguous"
                );
            }
            create_result
                .map_err(|_| Error::with_message(Errno::EIO, "cannot create virgl context"))?;
            self.gpu_manager.virgl_contexts.record_created(context.id);
            context.is_created = true;
        }
        Ok(context.id)
    }

    /// Prevents further submissions after a host command leaves context
    /// membership ambiguous. Context destruction is best-effort; poisoning is
    /// what guarantees that this file cannot reuse the context afterwards.
    fn poison_virgl_context_locked(&self, context: &mut VirglContext) {
        context.is_poisoned = true;
        if context.is_created {
            if self.gpu_manager.gpu.ctx_destroy(context.id).is_ok() {
                self.gpu_manager.virgl_contexts.record_destroyed(context.id);
            } else {
                self.gpu_manager.virgl_contexts.defer_destroy(context.id);
            }
            context.is_created = false;
        }
    }

    fn attach_resource_to_context_locked(
        &self,
        context: &mut VirglContext,
        resource_id: u32,
    ) -> Result<u32> {
        let context_id = self.ensure_virgl_context_locked(context)?;
        if self
            .gpu_manager
            .virgl_contexts
            .has_attachment(context_id, resource_id)
        {
            return Ok(context_id);
        }
        if self
            .gpu_manager
            .gpu
            .ctx_attach_resource(context_id, resource_id)
            .is_err()
        {
            self.poison_virgl_context_locked(context);
            return_errno_with_message!(Errno::EIO, "cannot attach virgl resource");
        }
        self.gpu_manager
            .virgl_contexts
            .record_attachment(context_id, resource_id);
        Ok(context_id)
    }

    /// Makes a host resource visible to this file's virgl context.
    ///
    /// The caller must hold [`GpuManager::resource_creation`] so the global
    /// object-to-resource mapping cannot change during the attachment.
    fn attach_resource_to_context(&self, resource_id: u32) -> Result<u32> {
        let mut context = self.context.lock();
        self.attach_resource_to_context_locked(&mut context, resource_id)
    }

    /// Makes all resources referenced by a submission visible to its context.
    ///
    /// The caller must hold [`GpuManager::resource_creation`].
    fn attach_resources_to_context(&self, resource_ids: &[u32]) -> Result<u32> {
        let mut context = self.context.lock();
        let mut context_id = self.ensure_virgl_context_locked(&mut context)?;
        for resource_id in resource_ids {
            context_id = self.attach_resource_to_context_locked(&mut context, *resource_id)?;
        }
        Ok(context_id)
    }

    fn detach_resource_from_context_locked(
        &self,
        context: &mut VirglContext,
        resource_id: u32,
    ) -> Result<()> {
        if !self
            .gpu_manager
            .virgl_contexts
            .has_attachment(context.id, resource_id)
        {
            return Ok(());
        }
        if self
            .gpu_manager
            .gpu
            .ctx_detach_resource(context.id, resource_id)
            .is_err()
        {
            self.poison_virgl_context_locked(context);
            return_errno_with_message!(Errno::EIO, "cannot detach virgl resource");
        }
        self.gpu_manager
            .virgl_contexts
            .record_detachment(context.id, resource_id);
        Ok(())
    }

    /// Detaches a resource while the caller holds
    /// [`GpuManager::resource_creation`].
    fn detach_resource_from_context(&self, resource_id: u32) -> Result<()> {
        let mut context = self.context.lock();
        self.detach_resource_from_context_locked(&mut context, resource_id)
    }

    /// Reserves one per-file handle reference and attaches an existing host
    /// resource before the handle can become visible to userspace.
    fn reserve_gem_handle(&self, object_id: u32) -> Result<u32> {
        let _resource_creation = self.gpu_manager.resource_creation.lock();
        let resource_id = self.gpu_manager.live_gem_resource(object_id);
        let mut context = self.context.lock();
        let gem_handle = {
            let mut inner = self.inner.lock();
            let gem_handle = inner.next_handle;
            inner.next_handle = gem_handle
                .checked_add(1)
                .ok_or_else(|| Error::with_message(Errno::ENOSPC, "GEM handle space exhausted"))?;
            let count = inner.object_handle_counts.entry(object_id).or_default();
            *count = count.checked_add(1).ok_or_else(|| {
                Error::with_message(Errno::EOVERFLOW, "GEM handle reference count overflows")
            })?;
            gem_handle
        };

        if let Some(resource_id) = resource_id
            && let Err(error) = self.attach_resource_to_context_locked(&mut context, resource_id)
        {
            let mut inner = self.inner.lock();
            if let Some(count) = inner.object_handle_counts.get_mut(&object_id) {
                *count -= 1;
                if *count == 0 {
                    inner.object_handle_counts.remove(&object_id);
                }
            }
            return Err(error);
        }
        Ok(gem_handle)
    }

    /// Drops one reserved or published handle reference, detaching the host
    /// resource when no handle in this file can use it any longer.
    fn release_gem_handle_reference(&self, object_id: u32) -> Result<()> {
        let _resource_creation = self.gpu_manager.resource_creation.lock();
        let resource_id = self.gpu_manager.live_gem_resource(object_id);
        let mut context = self.context.lock();
        let is_last = {
            let mut inner = self.inner.lock();
            let count = inner
                .object_handle_counts
                .get_mut(&object_id)
                .ok_or_else(|| {
                    Error::with_message(Errno::EINVAL, "GEM object has no per-file handles")
                })?;
            if *count == 0 {
                return_errno_with_message!(Errno::EINVAL, "GEM handle reference count is zero");
            }
            *count -= 1;
            if *count == 0 {
                inner.object_handle_counts.remove(&object_id);
                true
            } else {
                false
            }
        };
        if is_last
            && let Some(resource_id) = resource_id
            && let Err(error) = self.detach_resource_from_context_locked(&mut context, resource_id)
        {
            warn!(
                "poisoned virgl context {} after resource detach failed: {:?}",
                context.id, error
            );
        }
        Ok(())
    }

    fn reserve_legacy_page_flip(&self) -> Result<LegacyPageFlipReservation> {
        LegacyPageFlipReservation::reserve(self.is_legacy_page_flip_pending.clone())
    }

    fn ensure_no_pending_legacy_page_flip(&self) -> Result<()> {
        if self.is_legacy_page_flip_pending.load(Ordering::Acquire) {
            return_errno_with_message!(Errno::EBUSY, "a legacy page flip is still pending");
        }
        Ok(())
    }

    /// Serializes a KMS operation against an in-flight nonblocking commit.
    fn lock_page_flip_operation(&self) -> Result<MutexGuard<'_, ()>> {
        let operation = self.page_flip_operation.lock();
        self.atomic_commit_queue.ensure_idle()?;
        self.ensure_no_pending_legacy_page_flip()?;
        Ok(operation)
    }

    /// Pops complete DRM events into `writer`.
    fn read_events(&self, writer: &mut VmWriter) -> Result<usize> {
        if writer.avail() == 0 {
            return Ok(0);
        }
        let max_events = writer.avail() / size_of::<DrmEventVblank>();
        if max_events == 0 {
            return_errno_with_message!(Errno::EINVAL, "the buffer is too short for an event");
        }

        let _event_read_operation = self.event_read_operation.lock();
        let mut bytes = 0;
        while bytes / size_of::<DrmEventVblank>() < max_events {
            let Some(event) = self.event_queue.pop() else {
                break;
            };
            let write_result = match &event {
                queue::DrmEvent::Vblank(event) => writer.write_val(event),
                queue::DrmEvent::CrtcSequence(event) => writer.write_val(event),
            };
            if let Err(error) = write_result {
                self.event_queue.requeue_front(event);
                if bytes != 0 {
                    return Ok(bytes);
                }
                return Err(error.into());
            }
            bytes += size_of::<DrmEventVblank>();
        }
        if bytes == 0 {
            return_errno_with_message!(Errno::EAGAIN, "no pending DRM events");
        }
        Ok(bytes)
    }

    /// Snapshots the GEM object ids owned by this file without allocating
    /// while the per-file spinlock is held.
    fn snapshot_object_ids(&self) -> Vec<u32> {
        loop {
            let capacity = self.inner.lock().handles.len();
            let mut object_ids = Vec::with_capacity(capacity);
            let inner = self.inner.lock();
            if inner.handles.len() > object_ids.capacity() {
                continue;
            }
            object_ids.extend(inner.handles.values().copied());
            return object_ids;
        }
    }

    /// Resolves mmap ranges without allocating while the GEM table is locked.
    fn snapshot_mappable_ranges(&self, object_ids: &[u32]) -> Vec<GuardedVmoRange> {
        let mut ranges = Vec::with_capacity(object_ids.len());
        for object_id in object_ids {
            let Ok((buffer, lifetime)) = self.gpu_manager.retain_gem_mapping(*object_id) else {
                continue;
            };
            let Some(range) = buffer.mapped_range() else {
                continue;
            };
            ranges.push(GuardedVmoRange::new(range, lifetime));
        }
        ranges
    }
}

impl Drop for DriHandle {
    fn drop(&mut self) {
        // Workers own references to per-file event state and framebuffer
        // backing. Wait before tearing down the remaining per-file namespace.
        self.atomic_commit_queue.wait_until_idle();
        self.vblank_completion_queue.cancel_pending_and_wait();
        let mut kms_state = self.gpu_manager.kms_state.lock();
        let _cursor_operation = self.cursor_operation.lock();
        let (resource_id, position) = {
            let inner = self.inner.lock();
            (inner.cursor.resource_id, inner.cursor.position)
        };
        if kms_state.scanout_owned_by(self.file_id) {
            if let Err(error) = self.gpu_manager.disable_scanout() {
                warn!("cannot disable scanout on DRM file close: {:?}", error);
            }
            kms_state.scanout = None;
            self.gpu_manager.property_manager.reset_atomic_state();
        }
        if let Some(resource_id) = resource_id {
            let _ = self
                .gpu_manager
                .gpu
                .clear_cursor(resource_id, position.x, position.y);
        }
        if kms_state.is_master(self.file_id) {
            kms_state.master_file_id = None;
        }
        drop(kms_state);

        if let Some(magic) = self.inner.lock().auth_magic {
            self.gpu_manager.auth_magics.lock().remove(&magic);
        }
        self.gpu_manager
            .property_manager
            .release_file_blobs(self.file_id);

        let context = self.context.get_mut();
        self.gpu_manager.wait_for_all_fences();
        if context.is_created {
            match self.gpu_manager.gpu.ctx_destroy(context.id) {
                Ok(()) => self.gpu_manager.virgl_contexts.record_destroyed(context.id),
                Err(error) => {
                    warn!("cannot destroy virgl context {}: {:?}", context.id, error);
                    self.gpu_manager.virgl_contexts.defer_destroy(context.id);
                }
            }
        }

        let inner = self.inner.get_mut();
        let framebuffer_objects: Vec<_> = inner
            .framebuffers
            .values()
            .map(|framebuffer| framebuffer.object_id)
            .collect();
        let handle_objects: Vec<_> = inner.handles.values().copied().collect();
        inner.framebuffers.clear();
        inner.handles.clear();
        inner.object_handle_counts.clear();
        for object_id in framebuffer_objects.into_iter().chain(handle_objects) {
            if let Err(error) = self.gpu_manager.release_gem_object(object_id) {
                warn!(
                    "cannot release GEM object {} on close: {:?}",
                    object_id, error
                );
            }
        }
    }
}

// ---------------------------------------------------------------------------
// ioctl dispatch
// ---------------------------------------------------------------------------

impl Pollable for DriHandle {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        self.event_queue.poll(mask, poller)
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
    /// A DRM handle never participates in an SCM_RIGHTS ownership cycle:
    /// its GEM handles, framebuffers, and events are numeric IDs or kernel
    /// objects, and PRIME/sync-file exports install freshly created files
    /// into the recipient's table without being retained here.  The only
    /// transitively retained file descriptions are eventfds bound by
    /// `SYNCOBJ_EVENTFD`, and an eventfd retains nothing, so the chain always
    /// terminates at a leaf.
    fn is_scm_rights_proven_leaf(&self) -> bool {
        true
    }

    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        false
    }

    fn write_fdinfo(&self, formatter: &mut Formatter<'_>) -> core::fmt::Result {
        fdinfo::write(self, formatter)
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
        let object_ids = self.snapshot_object_ids();
        let ranges = self.snapshot_mappable_ranges(&object_ids);
        Ok(Mappable::VmoGuardedRanges { vmo: pool, ranges })
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
            let _page_flip_operation = self.lock_page_flip_operation()?;
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
            cmd @ GetMagic => {
                cmd.write(&DrmAuth {
                    magic: self.get_auth_magic()?,
                })?;
                Ok(0)
            }
            cmd @ GetCap => {
                let mut cap = cmd.read()?;
                cap.value = match cap.capability {
                    DRM_CAP_DUMB_BUFFER => 1,
                    DRM_CAP_VBLANK_HIGH_CRTC => 1,
                    DRM_CAP_DUMB_PREFERRED_DEPTH => 24,
                    DRM_CAP_DUMB_PREFER_SHADOW => 0,
                    DRM_CAP_TIMESTAMP_MONOTONIC => 1,
                    DRM_CAP_CURSOR_WIDTH => u64::from(CURSOR_SIZE),
                    DRM_CAP_CURSOR_HEIGHT => u64::from(CURSOR_SIZE),
                    DRM_CAP_CRTC_IN_VBLANK_EVENT => 1,
                    DRM_CAP_SYNCOBJ => 1,
                    DRM_CAP_SYNCOBJ_TIMELINE => 1,
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
                if cap.value > 1 {
                    return_errno_with_message!(
                        Errno::EINVAL,
                        "DRM client cap value must be 0 or 1"
                    );
                }
                let enabled = cap.value == 1;
                match cap.capability {
                    DRM_CLIENT_CAP_UNIVERSAL_PLANES => {
                        self.universal_planes.store(enabled, Ordering::Release);
                        Ok(0)
                    }
                    DRM_CLIENT_CAP_ATOMIC => {
                        self.atomic_modesetting.store(enabled, Ordering::Release);
                        if enabled {
                            self.universal_planes.store(true, Ordering::Release);
                        }
                        Ok(0)
                    }
                    _ => {
                        return_errno_with_message!(Errno::EINVAL, "unsupported DRM client cap")
                    }
                }
            }
            cmd @ WaitVblank => {
                vblank::wait_vblank(self, cmd)
            }
            cmd @ CrtcGetSequence => {
                vblank::get_crtc_sequence(self, cmd)
            }
            cmd @ CrtcQueueSequence => {
                vblank::queue_crtc_sequence(self, cmd)
            }
            cmd @ SyncobjCreate => {
                syncobj::create(self, cmd)
            }
            cmd @ SyncobjDestroy => {
                syncobj::destroy(self, cmd)
            }
            cmd @ SyncobjWait => {
                syncobj::wait(self, cmd)
            }
            cmd @ SyncobjReset => {
                syncobj::reset(self, cmd)
            }
            cmd @ SyncobjSignal => {
                syncobj::signal(self, cmd)
            }
            cmd @ SyncobjTimelineWait => {
                syncobj::timeline_wait(self, cmd)
            }
            cmd @ SyncobjQuery => {
                syncobj::query(self, cmd)
            }
            cmd @ SyncobjTransfer => {
                syncobj::transfer(self, cmd)
            }
            cmd @ SyncobjTimelineSignal => {
                syncobj::timeline_signal(self, cmd)
            }
            cmd @ AuthMagic => {
                let auth = cmd.read()?;
                self.authenticate_magic(auth.magic).map(|_| 0)
            }
            cmd @ GemClose => {
                let req = cmd.read()?;
                gem::gem_close(self, req.handle).map(|_| 0)
            }
            cmd @ GemFlink => {
                self.check_authenticated()?;
                let mut req = cmd.read()?;
                req.name = gem::gem_flink(self, req.handle)?;
                cmd.write(&req)?;
                Ok(0)
            }
            cmd @ GemOpen => {
                self.check_authenticated()?;
                let mut req = cmd.read()?;
                let (pending, size) = gem::gem_open(self, req.name)?;
                req.handle = pending.id();
                req.size = size;
                cmd.write(&req)?;
                pending.publish();
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
                let _page_flip_operation = self.lock_page_flip_operation()?;
                let mut kms_state = self.lock_kms_as_master()?;
                let req = cmd.read()?;
                kms::set_crtc(self, &mut kms_state, &req)?;
                Ok(0)
            }
            cmd @ ModeCursor => {
                let _kms_state = self.lock_kms_as_master()?;
                let req = cmd.read()?;
                kms::set_cursor(self, req.into())?;
                Ok(0)
            }
            cmd @ ModeCursor2 => {
                let _kms_state = self.lock_kms_as_master()?;
                let req = cmd.read()?;
                kms::set_cursor(self, req)?;
                Ok(0)
            }
            cmd @ ModeCreateDumb => {
                let req = cmd.read()?;
                let (response, pending_buffer) = dumb::create_dumb(self, &req)?;
                cmd.write(&response)?;
                pending_buffer.publish();
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
                if let Err(error) = cmd.write(&req) {
                    let _ = kms::rm_fb(self, req.fb_id);
                    return Err(error);
                }
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
                plane::get_plane_resources(self, cmd)
            }
            cmd @ ModeGetPlane => {
                if self.is_render_node() {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "KMS ioctl not available on render node"
                    );
                }
                plane::get_plane(self, cmd)
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
                if let Err(error) = cmd.write(&req) {
                    let _ = kms::rm_fb(self, req.fb_id);
                    return Err(error);
                }
                Ok(0)
            }
            cmd @ ModePageFlip => {
                let _page_flip_operation = self.lock_page_flip_operation()?;
                let mut kms_state = self.lock_kms_as_master()?;
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
                if req.flags & DRM_MODE_PAGE_FLIP_ASYNC != 0 {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "asynchronous page flips are not implemented"
                    );
                }
                let event_slot = if req.flags & DRM_MODE_PAGE_FLIP_EVENT != 0 {
                    Some(self.event_queue.reserve()?)
                } else {
                    None
                };
                let framebuffer = *self
                    .inner
                    .lock()
                    .framebuffers
                    .get(&req.fb_id)
                    .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown framebuffer id"))?;
                if kms_state.scanout.is_none()
                    || framebuffer.width != kms_state.current_width
                    || framebuffer.height != kms_state.current_height
                {
                    return_errno_with_message!(
                        Errno::EINVAL,
                        "page flip framebuffer does not match the active mode"
                    );
                }
                let flip_reservation = self.reserve_legacy_page_flip()?;
                kms::present_fb(self, &mut kms_state, req.fb_id)?;
                self.gpu_manager
                    .property_manager
                    .set_legacy_page_flip_state(req.fb_id, framebuffer.width, framebuffer.height);
                let gpu_manager = self.gpu_manager.clone();
                let user_data = req.user_data;
                let snapshot = gpu_manager.vblank_clock.snapshot();
                let target_sequence = snapshot.sequence.saturating_add(1);
                self.vblank_completion_queue.submit_at(
                    target_sequence,
                    Box::new(move |vblank| {
                        if let Some(event_slot) = event_slot {
                            event_slot.queue_flip(vblank, user_data);
                        }
                        drop(flip_reservation);
                    }),
                );
                Ok(0)
            }
            cmd @ ModeDirtyFb => {
                let _page_flip_operation = self.lock_page_flip_operation()?;
                let kms_state = self.lock_kms_as_master()?;
                let req = cmd.read()?;
                if req.fb_id == 0 {
                    return Ok(0);
                }
                let framebuffer = kms::prepare_fb(self, req.fb_id)?;
                if kms_state.scanout_matches(self.file_id, req.fb_id) {
                    kms::scanout_prepared_fb(&self.gpu_manager, framebuffer)?;
                }
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
            cmd @ ModeAtomic => {
                Some((|| -> Result<i32> {
                    if !self.atomic_modesetting.load(Ordering::Acquire) {
                        return_errno_with_message!(
                            Errno::EOPNOTSUPP,
                            "DRM_CLIENT_CAP_ATOMIC is not enabled"
                        );
                    }
                    // Atomic submissions may queue behind earlier nonblocking
                    // commits. Software state is exchanged before each ioctl
                    // returns, while the hardware queue preserves submission order.
                    let _page_flip_operation = self.page_flip_operation.lock();
                    self.ensure_no_pending_legacy_page_flip()?;
                    let mut kms_state = self.lock_kms_as_master()?;
                    atomic::mode_atomic(self, &mut kms_state, cmd, file_table)
                })())
            }
            cmd @ VirtgpuExecbuffer => {
                virtio_gpu::virtgpu_execbuffer(self, cmd, file_table)
            }
            cmd @ SyncobjHandleToFd => {
                syncobj::handle_to_fd(self, cmd, file_table)
            }
            cmd @ SyncobjFdToHandle => {
                syncobj::fd_to_handle(self, cmd, file_table)
            }
            cmd @ SyncobjEventfd => {
                syncobj::eventfd(self, cmd, file_table)
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
                let file: Arc<dyn crate::fs::file::FileLike> = file;
                let fd: FileDesc = file_table.unwrap().write().insert(file.clone(), fd_flags);
                req.fd = RawFileDesc::from(fd);
                if let Err(e) = cmd.write(&req) {
                    let closed = file_table.unwrap().write().close_file_if_same(fd, &file);
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
                let (pending, _size) = match prime::fd_to_handle(self, dma_buf) {
                    Ok(v) => v,
                    Err(e) => return Some(Err(e)),
                };
                let mut resp = req;
                resp.handle = pending.id();
                if let Err(e) = cmd.write(&resp) {
                    return Some(Err(e));
                }
                pending.publish();
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

    let hsync_start = width.saturating_add(HORIZONTAL_FRONT_PORCH);
    let hsync_end = hsync_start.saturating_add(HORIZONTAL_SYNC_WIDTH);
    let htotal = hsync_end.saturating_add(HORIZONTAL_BACK_PORCH);
    let vsync_start = height.saturating_add(VERTICAL_FRONT_PORCH);
    let vsync_end = vsync_start.saturating_add(VERTICAL_SYNC_WIDTH);
    let vtotal = vsync_end.saturating_add(VERTICAL_BACK_PORCH);
    DrmModeModeInfo {
        // Pixel clock is expressed in kHz and includes blanking intervals.
        // Keeping it consistent with the totals makes libdrm derive 60 Hz.
        clock: htotal
            .saturating_mul(vtotal)
            .saturating_mul(DEFAULT_REFRESH_HZ)
            / 1000,
        hdisplay: width as u16,
        hsync_start: hsync_start as u16,
        hsync_end: hsync_end as u16,
        htotal: htotal as u16,
        hskew: 0,
        vdisplay: height as u16,
        vsync_start: vsync_start as u16,
        vsync_end: vsync_end as u16,
        vtotal: vtotal as u16,
        vscan: 0,
        vrefresh: DEFAULT_REFRESH_HZ,
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
