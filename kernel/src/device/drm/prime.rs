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

use super::{
    DriHandle, DumbBuffer, GpuManager,
    gem::{GemObjectRef, PendingGemHandle},
};
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

/// A dma-buf file wrapping an exported GEM/dumb buffer.
pub(super) struct DmaBufFile {
    /// Keeps the exporting device and GEM object alive until the fd closes.
    gpu_manager: Arc<GpuManager>,
    object_id: u32,
    /// The shared dumb-buffer pool the buffer is carved out of.
    pool: Arc<Vmo>,
    /// The buffer's sub-range within `pool`.
    buffer: DumbBuffer,
    access_mode: AccessMode,
    common: FileCommon,
}

impl DmaBufFile {
    fn new(
        gpu_manager: Arc<GpuManager>,
        object_id: u32,
        pool: Arc<Vmo>,
        buffer: DumbBuffer,
        access_mode: AccessMode,
    ) -> Self {
        let pseudo_path = AnonInodeFs::new_path(|_| "anon_inode:dmabuf".to_string());
        Self {
            gpu_manager,
            object_id,
            pool,
            buffer,
            access_mode,
            common: FileCommon::new(pseudo_path, StatusFlags::empty()),
        }
    }

    fn buffer(&self) -> DumbBuffer {
        self.buffer
    }
}

impl Drop for DmaBufFile {
    fn drop(&mut self) {
        if let Err(error) = self.gpu_manager.release_gem_object(self.object_id) {
            ostd::warn!(
                "cannot release GEM object {} after dma-buf close: {:?}",
                self.object_id,
                error
            );
        }
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
        self.access_mode
    }

    fn common(&self) -> &FileCommon {
        &self.common
    }

    fn mappable(&self) -> Result<Mappable> {
        let range = self
            .buffer
            .mapped_range()
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "dma-buf mapping range overflows"))?;
        let size = range.end - range.start;
        Ok(Mappable::VmoWindow {
            vmo: self.pool.clone(),
            vmo_offset: range.start,
            size,
        })
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
    const DRM_CLOEXEC: u32 = CreationFlags::O_CLOEXEC.bits();
    const DRM_RDWR: u32 = AccessMode::O_RDWR as u32;

    if flags & !(DRM_CLOEXEC | DRM_RDWR) != 0 {
        return_errno_with_message!(Errno::EINVAL, "unsupported PRIME export flags");
    }
    let inner = handle.inner.lock();
    let object_id = *inner
        .handles
        .get(&gem_handle)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?;
    drop(inner);

    let pool = handle.gpu_manager.ensure_pool()?;
    let buffer = handle.gpu_manager.retain_gem_object(object_id)?;
    let access_mode = if flags & DRM_RDWR != 0 {
        AccessMode::O_RDWR
    } else {
        AccessMode::O_RDONLY
    };
    let file = Arc::new(DmaBufFile::new(
        handle.gpu_manager.clone(),
        object_id,
        pool,
        buffer,
        access_mode,
    ));

    let fd_flags = if flags & DRM_CLOEXEC != 0 {
        FdFlags::CLOEXEC
    } else {
        FdFlags::empty()
    };
    Ok((file, fd_flags))
}

/// Imports a dma-buf fd as a per-file GEM handle.
///
/// Returns the new handle (and the buffer size, for completeness).
pub(super) fn fd_to_handle<'a>(
    handle: &'a DriHandle,
    file: &DmaBufFile,
) -> Result<(PendingGemHandle<'a>, u64)> {
    if !Arc::ptr_eq(&handle.gpu_manager, &file.gpu_manager) {
        return_errno_with_message!(Errno::EXDEV, "dma-buf belongs to another DRM device");
    }
    let buffer = file.buffer();
    let object_id = file.object_id;
    let object = GemObjectRef::retain(&handle.gpu_manager, object_id)?;
    let pending = PendingGemHandle::new(handle, object)?;
    Ok((pending, buffer.size as u64))
}
