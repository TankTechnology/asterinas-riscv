// SPDX-License-Identifier: MPL-2.0

//! PRIME / dma-buf sharing for GEM objects.
//!
//! `DRM_IOCTL_PRIME_HANDLE_TO_FD` exports a GEM handle as a dma-buf-like file
//! descriptor; `DRM_IOCTL_PRIME_FD_TO_HANDLE` imports that descriptor back into
//! the caller's per-file GEM handle space. Mesa's virtio-gpu (virgl) driver
//! uses this pair to attach a GBM dumb buffer to a virgl resource, so without
//! it the driver falls back to the CPU llvmpipe path.
//!
//! Every dumb buffer is a page-aligned sub-range of the single shared pool VMO
//! (see [`super::GpuManager::ensure_pool`]). A [`DmaBufFile`] therefore holds
//! the pool plus the buffer's offset/size; `mmap` on the exported fd maps the
//! pool VMO and the caller supplies the buffer offset, matching the existing
//! dumb-buffer mmap convention.

use core::fmt::Display;

use crate::{
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
    vm::page_cache::Vmo,
};

use super::{DriHandle, DumbBuffer, GemObject};

/// A dma-buf file wrapping an exported GEM/dumb buffer.
pub(super) struct DmaBufFile {
    /// The shared dumb-buffer pool the buffer is carved out of.
    pool: Arc<Vmo>,
    /// The buffer's sub-range within `pool`.
    buffer: DumbBuffer,
    common: FileCommon,
}

impl DmaBufFile {
    fn new(pool: Arc<Vmo>, buffer: DumbBuffer) -> Self {
        let pseudo_path = AnonInodeFs::new_path(|_| "anon_inode:dmabuf".to_string());
        Self {
            pool,
            buffer,
            common: FileCommon::new(pseudo_path, StatusFlags::empty()),
        }
    }

    fn buffer(&self) -> DumbBuffer {
        self.buffer
    }
}

impl Pollable for DmaBufFile {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        // A dma-buf has no read/write events; report only the always-available
        // bits the caller asked for.
        mask & IoEvents::OUT
    }
}

impl FileLike for DmaBufFile {
    fn access_mode(&self) -> AccessMode {
        AccessMode::O_RDWR
    }

    fn common(&self) -> &FileCommon {
        &self.common
    }

    fn mappable(&self) -> Result<Mappable> {
        Ok(Mappable::Vmo(self.pool.clone()))
    }

    fn dump_proc_fdinfo(self: Arc<Self>, fd_flags: FdFlags) -> Box<dyn Display> {
        struct FdInfo {
            flags: u32,
            size: usize,
        }
        impl Display for FdInfo {
            fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
                writeln!(f, "pos:\t{}", 0)?;
                writeln!(f, "flags:\t0{:o}", self.flags)?;
                writeln!(f, "size:\t{}", self.size)
            }
        }
        let mut flags = self.common.status_flags().bits() | self.access_mode() as u32;
        if fd_flags.contains(FdFlags::CLOEXEC) {
            flags |= CreationFlags::O_CLOEXEC.bits();
        }
        Box::new(FdInfo {
            flags,
            size: self.buffer.size,
        })
    }
}

/// Exports a per-file GEM handle as a dma-buf file plus its fd flags.
///
/// The caller installs the returned `Arc<DmaBufFile>` into the current
/// process's file table and returns the resulting fd to userspace.
pub(super) fn handle_to_fd(
    handle: &DriHandle,
    gem_handle: u32,
    flags: u32,
) -> Result<(Arc<DmaBufFile>, FdFlags)> {
    let inner = handle.inner.lock();
    let object_id = *inner
        .handles
        .get(&gem_handle)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?;
    drop(inner);

    let guard = handle.gpu_manager.gem_objects.lock();
    let obj = guard
        .get(&object_id)
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;
    let buffer = obj.buffer;
    drop(guard);

    let pool = handle.gpu_manager.ensure_pool()?;
    let file = Arc::new(DmaBufFile::new(pool, buffer));

    // `DRM_CLOEXEC` is the only supported export flag.
    let fd_flags = if flags & CreationFlags::O_CLOEXEC.bits() != 0 {
        FdFlags::CLOEXEC
    } else {
        FdFlags::empty()
    };
    Ok((file, fd_flags))
}

/// Imports a dma-buf fd as a per-file GEM handle.
///
/// Returns the new handle (and the buffer size, for completeness).
pub(super) fn fd_to_handle(
    handle: &DriHandle,
    file: &DmaBufFile,
) -> Result<(u32, u64)> {
    let buffer = file.buffer();

    // Register a fresh GEM object backed by the same pool sub-range.
    let object_id = handle
        .gpu_manager
        .next_gem_id
        .fetch_add(1, core::sync::atomic::Ordering::Relaxed);
    let gem_obj = GemObject {
        name: core::sync::atomic::AtomicU32::new(0),
        ref_count: core::sync::atomic::AtomicU32::new(1),
        buffer,
    };
    handle
        .gpu_manager
        .gem_objects
        .lock()
        .insert(object_id, Arc::new(gem_obj));

    let mut inner = handle.inner.lock();
    let gem_handle = inner.next_handle;
    inner.next_handle += 1;
    inner.handles.insert(gem_handle, object_id);

    Ok((gem_handle, buffer.size as u64))
}
