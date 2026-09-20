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

use core::fmt::Display;

use super::{DriHandle, GEM_OBJECTS, ensure_pool, object_for_handle};
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
    /// The object this descriptor names.
    object_id: u32,
    /// The size of that object, reported by import.
    size: u64,
    access_mode: AccessMode,
    common: FileCommon,
}

impl DmaBufFile {
    pub(super) fn object_id(&self) -> u32 {
        self.object_id
    }

    pub(super) fn size(&self) -> u64 {
        self.size
    }
}

impl Drop for DmaBufFile {
    fn drop(&mut self) {
        // The export took a reference on the object; closing the descriptor is
        // what gives it back.
        super::release_object(self.object_id);
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
    /// Says that this file owns no other file description, which is what lets
    /// it be passed over a socket.
    ///
    /// The claim is checkable: the fields are a device pool VMO, an object id,
    /// a size, an access mode, and [`FileCommon`] — a path, status flags and an
    /// owner. None of them can reach the file table. Without this the
    /// classifier in `net::socket::unix::ctrl_msg` returns `Unsupported` and
    /// the *whole* `sendmsg` is refused with `EPERM`, so the descriptor never
    /// arrives and the peer blocks for one that will not come.
    fn is_scm_rights_proven_leaf(&self) -> bool {
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
                writeln!(f, "pos:\t{}", 0)?;
                writeln!(f, "flags:\t0{:o}", flags)?;
                writeln!(f, "size:\t{}", self.inner.size)?;
                writeln!(f, "mnt_id:\t{}", AnonInodeFs::mount_node().id())?;
                writeln!(f, "ino:\t{}", AnonInodeFs::shared_inode().ino())?;
                write!(f, "drm-dmabuf:\tobject={}", self.inner.object_id)
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
    /// `DRM_CLOEXEC`, which is `O_CLOEXEC` under another name.
    const DRM_CLOEXEC: u32 = CreationFlags::O_CLOEXEC.bits();
    /// `DRM_RDWR`, which is `O_RDWR` under another name.
    const DRM_RDWR: u32 = AccessMode::O_RDWR as u32;

    if flags & !(DRM_CLOEXEC | DRM_RDWR) != 0 {
        return_errno_with_message!(Errno::EINVAL, "unsupported PRIME export flags");
    }

    let object_id = object_for_handle(&handle.inner.lock(), gem_handle)?;

    // Take the object's reference before releasing the lock that found it, so
    // it cannot be freed between the lookup and the descriptor existing.
    let (size, pool) = {
        let mut objects = GEM_OBJECTS.lock();
        let object = objects
            .objects
            .get_mut(&object_id)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM object"))?;
        object.refs = object.refs.saturating_add(1);
        (object.size as u64, ensure_pool(&mut objects)?)
    };

    let access_mode = if flags & DRM_RDWR != 0 {
        AccessMode::O_RDWR
    } else {
        AccessMode::O_RDONLY
    };
    let path = AnonInodeFs::new_path(|_| "anon_inode:dma-buf".to_string());
    let file = Arc::new(DmaBufFile {
        pool,
        object_id,
        size,
        access_mode,
        common: FileCommon::new(path, StatusFlags::empty()),
    });

    let fd_flags = if flags & DRM_CLOEXEC != 0 {
        FdFlags::CLOEXEC
    } else {
        FdFlags::empty()
    };
    Ok((file, fd_flags))
}

/// Imports a dma-buf descriptor as a new handle in this file's handle space.
pub(super) fn fd_to_handle(handle: &DriHandle, file: &DmaBufFile) -> Result<u32> {
    let mut inner = handle.inner.lock();
    let mut objects = GEM_OBJECTS.lock();
    let object = objects
        .objects
        .get_mut(&file.object_id)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM object"))?;
    object.refs = object.refs.saturating_add(1);

    let new_handle = inner.next_handle;
    inner.next_handle = inner.next_handle.saturating_add(1);
    inner.handles.insert(new_handle, file.object_id);
    Ok(new_handle)
}
