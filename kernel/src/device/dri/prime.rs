// SPDX-License-Identifier: MPL-2.0

//! PRIME / dma-buf sharing for GEM objects.
//!
//! `DRM_IOCTL_PRIME_HANDLE_TO_FD` exports a GEM handle as a dma-buf-like file
//! descriptor; `DRM_IOCTL_PRIME_FD_TO_HANDLE` imports one back into the
//! caller's per-file handle space.
//!
//! This pair is what makes `DRM_CAP_PRIME` true rather than merely advertised.
//! Mesa reads that capability as the answer to a single question — can this
//! device hand a buffer out at all — and takes its `create_dumb()` path for
//! *every* buffer when the answer is no. `create_dumb()` accepts a scanout
//! buffer only as XRGB8888 or XBGR8888, and Xorg's modesetting driver asks for
//! ARGB8888 with `SCANOUT`, so on that path `drmmode_create_bo()` returns NULL
//! before issuing any ioctl, `ScreenInit()` fails without a message, and the
//! session restarts into the same wall. Nothing above the kernel can tell that
//! apart from a driver that works.
//!
//! Buffers here are page-aligned spans of the single device-wide pool (see
//! [`super::GEM_OBJECTS`]), so a dma-buf holds the pool plus the object id it
//! names, and takes a reference on the object for as long as the descriptor is
//! open.

use core::{
    fmt::Display,
    sync::atomic::{AtomicU64, Ordering},
};

use aster_virtio::device::gpu::device::GpuDevice;
use ostd::mm::{
    VmIo,
    dma::{DmaSyncDirection, sync_dma_range},
};

use super::{DriHandle, GEM_OBJECTS, GemObjects, ensure_pool, fence, object_for_handle};
use crate::{
    context::current_userspace,
    events::IoEvents,
    fs::{
        file::{
            AccessMode, CreationFlags, FileCommon, FileLike, Mappable, StatusFlags,
            file_table::FdFlags,
        },
        pseudofs::AnonInodeFs,
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
    util::ioctl::RawIoctl,
    vm::page_cache::Vmo,
};

/// `DMA_BUF_IOCTL_SYNC`: `_IOW('b', 0, struct dma_buf_sync)`.
const DMA_BUF_IOCTL_SYNC: u32 = 0x4008_6200;
const DMA_BUF_SYNC_READ: u64 = 1;
const DMA_BUF_SYNC_WRITE: u64 = 2;
const DMA_BUF_SYNC_END: u64 = 4;

/// A dma-buf descriptor naming one exported GEM object.
///
/// It is a [`FileLike`] rather than an inode handle, which is why the SCM_RIGHTS
/// classifier has to be told about it by name: everything it holds is a device
/// pool, an object id and a size, and none of those can retain a file
/// description. Without that, passing one to another process is refused.
pub(crate) struct DmaBufFile {
    /// The device-wide pool the object is carved out of. Holding it keeps the
    /// mapping valid after every other file has closed.
    pool: Arc<Vmo>,
    /// The exported object's shape and GPU mapping, fixed when the descriptor
    /// is created.
    export: DmaBufExport,
    /// The provider that owns the host resource, if this object is a virgl
    /// resource. Keeping the provider with the dma-buf lets its final close
    /// issue `RESOURCE_UNREF` after the device-wide GEM reference is gone.
    gpu: Option<Arc<GpuDevice>>,
    /// The minimal implicit-sync attachment: the last completed job's fence,
    /// or a fence that is already signalled when no job has run.
    fence: Arc<fence::FenceState>,
    /// CPU-access cache maintenance accounting for `DMA_BUF_IOCTL_SYNC`.
    cache: DmaBufCache,
    access_mode: AccessMode,
    common: FileCommon,
}

/// The immutable description an export hands to an importer.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct DmaBufExport {
    pub(super) object_id: u32,
    pub(super) offset: usize,
    pub(super) size: u64,
    /// The object's bits-per-pixel when known; raw PVR GEMs carry 0.
    pub(super) format: u32,
    /// The object's job-context GPU VA, when it is a PVR GEM.
    pub(super) pvr_vaddr: Option<usize>,
}

/// Counters for the cache operations a dma-buf's CPU-access ioctl requested.
#[derive(Debug, Default)]
struct DmaBufCache {
    sync_calls: AtomicU64,
    cleans: AtomicU64,
    invalidations: AtomicU64,
}

impl DmaBufCache {
    fn record(&self, clean: bool, invalidate: bool) {
        self.sync_calls.fetch_add(1, Ordering::Relaxed);
        if clean {
            self.cleans.fetch_add(1, Ordering::Relaxed);
        }
        if invalidate {
            self.invalidations.fetch_add(1, Ordering::Relaxed);
        }
    }

    fn stats(&self) -> (u64, u64, u64) {
        (
            self.sync_calls.load(Ordering::Relaxed),
            self.cleans.load(Ordering::Relaxed),
            self.invalidations.load(Ordering::Relaxed),
        )
    }
}

impl DmaBufFile {
    /// Applies the cache maintenance a `DMA_BUF_IOCTL_SYNC` request asks for.
    ///
    /// `START|READ` and `START|WRITE` invalidate before the CPU touches the
    /// buffer; `END|WRITE` cleans so the device sees CPU stores. `END|READ`
    /// needs no maintenance.
    fn sync_cpu_access(&self, flags: u64) -> Result<()> {
        let (clean, invalidate) = classify_cpu_access(flags)?;

        let (paddr, size) =
            {
                let objects = GEM_OBJECTS.lock();
                let object = objects.objects.get(&self.export.object_id).ok_or_else(|| {
                    Error::with_message(Errno::EINVAL, "dma-buf object disappeared")
                })?;
                let base = self.pool.paddr().ok_or_else(|| {
                    Error::with_message(Errno::ENODEV, "dma-buf pool has no memory")
                })?;
                (base + object.offset, object.size)
            };

        if invalidate {
            sync_dma_range(paddr, size, DmaSyncDirection::FromDevice)?;
        }
        if clean {
            sync_dma_range(paddr, size, DmaSyncDirection::ToDevice)?;
        }
        self.cache.record(clean, invalidate);
        Ok(())
    }
}

/// Decodes `dma_buf_sync.flags` into `(clean, invalidate)`.
fn classify_cpu_access(flags: u64) -> Result<(bool, bool)> {
    let known = DMA_BUF_SYNC_READ | DMA_BUF_SYNC_WRITE | DMA_BUF_SYNC_END;
    if flags & !known != 0 || flags & (DMA_BUF_SYNC_READ | DMA_BUF_SYNC_WRITE) == 0 {
        return_errno_with_message!(Errno::EINVAL, "invalid dma-buf sync flags");
    }
    let end = flags & DMA_BUF_SYNC_END != 0;
    let clean = end && flags & DMA_BUF_SYNC_WRITE != 0;
    let invalidate = !end && flags & (DMA_BUF_SYNC_READ | DMA_BUF_SYNC_WRITE) != 0;
    Ok((clean, invalidate))
}

impl Drop for DmaBufFile {
    fn drop(&mut self) {
        // The export took a reference on the object; closing the descriptor is
        // what gives it back.
        if let Some(resource_id) = super::release_object_and_take_resource(self.export.object_id)
            && let Some(gpu) = self.gpu.as_ref()
        {
            let _ = gpu.resource_unref(resource_id);
        }
    }
}

impl Pollable for DmaBufFile {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        // A dma-buf is memory, not a stream: there is no state to wait for.
        // Report only the always-available bits the caller asked about, which
        // is what keeps `poll` on one from blocking forever.
        mask & (IoEvents::IN | IoEvents::OUT)
    }
}

impl FileLike for DmaBufFile {
    fn is_scm_rights_leaf(&self) -> bool {
        true
    }

    fn access_mode(&self) -> AccessMode {
        self.access_mode
    }

    fn common(&self) -> &FileCommon {
        &self.common
    }

    /// Maps the device pool, not the buffer's span within it.
    ///
    /// `Mappable` has no window variant on this branch, so the mapping starts
    /// at the pool base rather than at this object's offset — a caller that
    /// mmaps a dma-buf gets the pool and must apply the offset itself, exactly
    /// as a dumb-buffer client does after `MODE_MAP_DUMB`. Consumers that
    /// import the descriptor rather than map it (the usual case, and the one
    /// that matters here) are unaffected.
    fn mappable(&self) -> Result<Mappable> {
        Ok(Mappable::Vmo(self.pool.clone()))
    }

    fn ioctl(&self, raw_ioctl: RawIoctl) -> Result<i32> {
        if raw_ioctl.cmd() != DMA_BUF_IOCTL_SYNC {
            return_errno_with_message!(Errno::ENOTTY, "unsupported dma-buf ioctl");
        }
        let flags: u64 = current_userspace!().read_val(raw_ioctl.arg())?;
        self.sync_cpu_access(flags)?;
        Ok(0)
    }

    fn dump_proc_fdinfo(self: Arc<Self>, fd_flags: FdFlags) -> Box<dyn Display> {
        struct FdInfo {
            inner: Arc<DmaBufFile>,
            fd_flags: FdFlags,
        }

        impl Display for FdInfo {
            fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
                let mut flags =
                    self.inner.common.status_flags().bits() | self.inner.access_mode() as u32;
                if self.fd_flags.contains(FdFlags::CLOEXEC) {
                    flags |= CreationFlags::O_CLOEXEC.bits();
                }
                let (sync_calls, cleans, invalidations) = self.inner.cache.stats();
                writeln!(f, "pos:\t{}", 0)?;
                writeln!(f, "flags:\t0{:o}", flags)?;
                writeln!(f, "size:\t{}", self.inner.export.size)?;
                writeln!(f, "mnt_id:\t{}", AnonInodeFs::mount_node().id())?;
                writeln!(f, "ino:\t{}", AnonInodeFs::shared_inode().ino())?;
                writeln!(
                    f,
                    "drm-dmabuf:\tobject={} offset={} size={} format={} gpu_va={:#x?} fence={}",
                    self.inner.export.object_id,
                    self.inner.export.offset,
                    self.inner.export.size,
                    self.inner.export.format,
                    self.inner.export.pvr_vaddr,
                    if self.inner.fence.is_signaled() {
                        "signalled"
                    } else {
                        "pending"
                    },
                )?;
                write!(
                    f,
                    "drm-dmabuf-cache:\tsync={} clean={} invalidate={}",
                    sync_calls, cleans, invalidations,
                )
            }
        }

        Box::new(FdInfo {
            inner: self,
            fd_flags,
        })
    }
}

/// Exports a per-file GEM handle as a dma-buf file plus the flags its
/// descriptor should carry.
///
/// The caller installs the returned file into the current process's file table
/// and reports the resulting descriptor to userspace.
pub(super) fn handle_to_fd(
    handle: &DriHandle,
    gem_handle: u32,
    flags: u32,
) -> Result<(Arc<DmaBufFile>, FdFlags)> {
    let (access_mode, fd_flags) = export_flags(flags)?;

    let object_id = object_for_handle(&handle.inner.lock(), gem_handle)?;

    // Take the object's reference before releasing the lock that found it, so
    // it cannot be freed between the lookup and the descriptor existing.
    let (export, pool) = {
        let mut objects = GEM_OBJECTS.lock();
        // Allocate the shared pool before taking the export reference.  A
        // failed pool allocation must leave the object reference count
        // unchanged; otherwise a rejected PRIME export would keep a GEM
        // object (and its host resource) alive forever.
        let pool = ensure_pool(&mut objects)?;
        let export = describe_object(&mut objects, object_id)?;
        (export, pool)
    };

    let path = AnonInodeFs::new_path(|_| "anon_inode:dma-buf".to_string());
    let file = Arc::new(DmaBufFile {
        pool,
        export,
        gpu: handle.display.gpu.clone(),
        fence: fence::completed_or_signaled(),
        cache: DmaBufCache::default(),
        access_mode,
        common: FileCommon::new(path, StatusFlags::empty()),
    });

    #[cfg(all(target_arch = "riscv64", feature = "powervr_probe"))]
    print_prime_marker(true, false, export);

    Ok((file, fd_flags))
}

/// Increments the object's reference and returns the description the dma-buf
/// carries. Split out so export/import accounting can be tested without a
/// pool or a `FileCommon`.
fn describe_object(objects: &mut GemObjects, object_id: u32) -> Result<DmaBufExport> {
    let object = objects
        .objects
        .get_mut(&object_id)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM object"))?;
    object.refs = object.refs.saturating_add(1);
    Ok(DmaBufExport {
        object_id,
        offset: object.offset,
        size: object.size as u64,
        format: object.bpp,
        pvr_vaddr: object.pvr_vaddr,
    })
}

/// Imports a description as one more handle on the same device-wide object.
///
/// The PVR mapping is established when the object is created, so importing a
/// PVR GEM reuses that GPU VA rather than installing a second mapping.
fn import_descriptor(objects: &mut GemObjects, export: &DmaBufExport) -> Result<u32> {
    let object = objects
        .objects
        .get_mut(&export.object_id)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM object"))?;
    if object.size as u64 != export.size {
        return_errno_with_message!(Errno::EINVAL, "dma-buf size does not match its object");
    }
    object.refs = object.refs.saturating_add(1);
    Ok(export.object_id)
}

/// Emits the board-visible PRIME marker for PVR objects only.
#[cfg(all(target_arch = "riscv64", feature = "powervr_probe"))]
fn print_prime_marker(exported: bool, imported: bool, export: DmaBufExport) {
    static EXPORTED: AtomicU64 = AtomicU64::new(0);
    static IMPORTED: AtomicU64 = AtomicU64::new(0);

    if export.pvr_vaddr.is_none() {
        return;
    }
    let exported_count = if exported {
        EXPORTED.fetch_add(1, Ordering::Relaxed) + 1
    } else {
        EXPORTED.load(Ordering::Relaxed)
    };
    let imported_count = if imported {
        IMPORTED.fetch_add(1, Ordering::Relaxed) + 1
    } else {
        IMPORTED.load(Ordering::Relaxed)
    };
    aster_logger::println!(
        "ASTERINAS_POWERVR_PRIME status=ok exported={} imported={}",
        exported_count,
        imported_count,
    );
}

/// Interprets `DRM_IOCTL_PRIME_HANDLE_TO_FD`'s `flags` argument.
///
/// `PRIME` reuses the open flags rather than defining its own: Linux's
/// `DRM_CLOEXEC` *is* `O_CLOEXEC` and `DRM_RDWR` *is* `O_RDWR`. That aliasing
/// is the reason this is a function with a test rather than four inline
/// branches — a driver that invented its own values would accept a `flags`
/// word libdrm never sends and reject the ones it does, and both failures look
/// like the ioctl not being implemented.
///
/// Split from [`handle_to_fd`] so the decoding can be checked without a device:
/// it is the whole of what that function does before it touches the object
/// space, and it is the part with the uapi contract in it.
fn export_flags(flags: u32) -> Result<(AccessMode, FdFlags)> {
    /// `DRM_CLOEXEC`, which is `O_CLOEXEC` under another name.
    const DRM_CLOEXEC: u32 = CreationFlags::O_CLOEXEC.bits();
    /// `DRM_RDWR`, which is `O_RDWR` under another name.
    const DRM_RDWR: u32 = AccessMode::O_RDWR as u32;

    if flags & !(DRM_CLOEXEC | DRM_RDWR) != 0 {
        return_errno_with_message!(Errno::EINVAL, "unsupported PRIME export flags");
    }

    let access_mode = if flags & DRM_RDWR != 0 {
        AccessMode::O_RDWR
    } else {
        AccessMode::O_RDONLY
    };
    let fd_flags = if flags & DRM_CLOEXEC != 0 {
        FdFlags::CLOEXEC
    } else {
        FdFlags::empty()
    };
    Ok((access_mode, fd_flags))
}

/// Imports a dma-buf descriptor as a new handle in this file's handle space.
pub(super) fn fd_to_handle(handle: &DriHandle, file: &DmaBufFile) -> Result<u32> {
    let mut inner = handle.inner.lock();
    let mut objects = GEM_OBJECTS.lock();
    let object_id = import_descriptor(&mut objects, &file.export)?;
    let new_handle = inner.next_handle;
    inner.next_handle = inner.next_handle.saturating_add(1);
    inner.handles.insert(new_handle, object_id);
    drop(objects);
    drop(inner);

    #[cfg(all(target_arch = "riscv64", feature = "powervr_probe"))]
    print_prime_marker(false, true, file.export);

    Ok(new_handle)
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn prime_export_flags_alias_the_open_flags() {
        // libdrm sends `DRM_CLOEXEC` and `DRM_RDWR`, which are `O_CLOEXEC` and
        // `O_RDWR`. Asserted against the literals the uapi defines rather than
        // against the constants this module uses, so a change to either side
        // has to be a deliberate one.
        assert_eq!(CreationFlags::O_CLOEXEC.bits(), 0o2000000);
        assert_eq!(AccessMode::O_RDWR as u32, 0o2);
    }

    #[ktest]
    fn no_flags_means_a_read_only_descriptor_that_survives_exec() {
        let (access, fd_flags) = export_flags(0).expect("no flags is legal");
        assert_eq!(access, AccessMode::O_RDONLY);
        assert!(!fd_flags.contains(FdFlags::CLOEXEC));
    }

    #[ktest]
    fn each_flag_selects_its_own_field() {
        let cloexec = CreationFlags::O_CLOEXEC.bits();
        let rdwr = AccessMode::O_RDWR as u32;

        let (access, fd_flags) = export_flags(rdwr).expect("DRM_RDWR");
        assert_eq!(access, AccessMode::O_RDWR);
        assert!(!fd_flags.contains(FdFlags::CLOEXEC));

        let (access, fd_flags) = export_flags(cloexec).expect("DRM_CLOEXEC");
        assert_eq!(access, AccessMode::O_RDONLY);
        assert!(fd_flags.contains(FdFlags::CLOEXEC));

        let (access, fd_flags) = export_flags(cloexec | rdwr).expect("both");
        assert_eq!(access, AccessMode::O_RDWR);
        assert!(fd_flags.contains(FdFlags::CLOEXEC));
    }

    #[ktest]
    fn a_flag_the_uapi_does_not_define_is_refused() {
        // Refused rather than masked off. A client that sent something else
        // meant something by it, and quietly handing back a descriptor with
        // different properties than it asked for is how a consumer ends up
        // writing to a mapping it was given read-only.
        let undefined = 0o4000;
        assert!(export_flags(undefined).is_err());
        assert!(export_flags(CreationFlags::O_CLOEXEC.bits() | undefined).is_err());
        // `O_ACCMODE`'s write-only bit is not `O_RDWR` and is not defined here.
        assert!(export_flags(0o1).is_err());
    }

    #[ktest]
    fn gpu_prime_export_import_roundtrip() {
        use super::super::{GemObject, release_object_locked};

        let mut objects = GemObjects::new();
        objects.objects.insert(
            7,
            GemObject {
                offset: 0x1000,
                size: 4096,
                pitch: 16,
                width: 4,
                height: 1,
                bpp: 32,
                resource_id: None,
                pvr_vaddr: Some(0x40_0000_0000),
                refs: 1,
            },
        );

        let export = describe_object(&mut objects, 7).expect("export");
        assert_eq!(export.object_id, 7);
        assert_eq!(export.offset, 0x1000);
        assert_eq!(export.size, 4096);
        assert_eq!(export.format, 32);
        assert_eq!(export.pvr_vaddr, Some(0x40_0000_0000));
        assert_eq!(objects.objects[&7].refs, 2, "export holds a reference");

        let imported = import_descriptor(&mut objects, &export).expect("import");
        assert_eq!(imported, export.object_id);
        assert_eq!(objects.objects[&7].refs, 3, "import holds a reference");
        assert_eq!(objects.objects[&7].pvr_vaddr, export.pvr_vaddr);

        release_object_locked(&mut objects, imported);
        release_object_locked(&mut objects, imported);
        assert!(objects.objects.contains_key(&7));
        release_object_locked(&mut objects, imported);
        assert!(
            !objects.objects.contains_key(&7),
            "object outlived its import and export references"
        );
    }

    #[ktest]
    fn gpu_dmabuf_cpu_access_sync() {
        assert_eq!(
            classify_cpu_access(DMA_BUF_SYNC_READ | DMA_BUF_SYNC_END).unwrap(),
            (false, false),
            "END|READ needs no maintenance"
        );
        assert_eq!(
            classify_cpu_access(DMA_BUF_SYNC_READ).unwrap(),
            (false, true),
            "START|READ invalidates before CPU reads"
        );
        assert_eq!(
            classify_cpu_access(DMA_BUF_SYNC_WRITE).unwrap(),
            (false, true),
            "START|WRITE invalidates before CPU stores"
        );
        assert_eq!(
            classify_cpu_access(DMA_BUF_SYNC_WRITE | DMA_BUF_SYNC_END).unwrap(),
            (true, false),
            "END|WRITE publishes CPU stores"
        );
        assert!(classify_cpu_access(0).is_err());
        assert!(
            classify_cpu_access(DMA_BUF_SYNC_READ | 8).is_err(),
            "undefined flags are refused, not masked"
        );

        let cache = DmaBufCache::default();
        cache.record(false, true);
        cache.record(true, false);
        assert_eq!(cache.stats(), (2, 1, 1));

        use ostd::mm::{HasPaddr, PAGE_SIZE, dma::DmaCoherent};

        let coherent = DmaCoherent::alloc(1, false).unwrap();
        sync_dma_range(coherent.paddr(), PAGE_SIZE, DmaSyncDirection::FromDevice).unwrap();
        sync_dma_range(coherent.paddr(), PAGE_SIZE, DmaSyncDirection::ToDevice).unwrap();
    }
}
