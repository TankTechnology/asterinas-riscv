// SPDX-License-Identifier: MPL-2.0

//! Anonymous file descriptors backed by a per-open file object.
//!
//! Kernel subsystems sometimes need to hand userspace a descriptor that is not
//! backed by a device node or a regular file — Linux does this with
//! `anon_inode_getfile`. This module is the analogue for components outside
//! `aster-core`: the caller supplies a [`PerOpenFileOps`] implementation
//! (dma-buf descriptors are the first user) and gets back an fd number.
//!
//! The descriptor's display name is a fixed string, so `/proc/<pid>/fd/<n>`
//! shows the category (`anon_inode:[drm-buf]`) rather than a per-instance
//! name.

use ostd::task::Task;

use super::{
    AccessMode, FileLike, InodeHandle, PerOpenFileOps, StatusFlags,
    file_table::{FdFlags, FileDesc},
};
use crate::{fs::pseudofs::AnonInodeFs, prelude::*};

/// Creates an anonymous file descriptor owned by `ops`.
///
/// `name` is the fixed name shown by `/proc/<pid>/fd/<n>`, e.g.
/// `"anon_inode:[drm-buf]"`. When `cloexec` is set, the descriptor is closed
/// on `exec`.
pub fn create_anon_file(
    ops: Box<dyn PerOpenFileOps>,
    cloexec: bool,
    name: &'static str,
) -> Result<i32> {
    let path = AnonInodeFs::new_static_path(name);
    let handle =
        InodeHandle::new_with_open_file(path, AccessMode::O_RDWR, StatusFlags::empty(), ops);
    let fd_flags = if cloexec {
        FdFlags::CLOEXEC
    } else {
        FdFlags::empty()
    };

    let current = Task::current().unwrap();
    let mut file_table = current.as_thread_local().unwrap().borrow_file_table_mut();
    let fd = file_table
        .unwrap()
        .write()
        .insert(Arc::new(handle), fd_flags);
    Ok(fd.into())
}

/// Closes the file descriptor `fd` in the current thread's file table.
///
/// Kernel subsystems that consume a descriptor passed in an ioctl (a commit
/// in-fence, for example) use this instead of re-entering the close syscall.
pub fn close_file_fd(fd: i32) -> Result<()> {
    let current = Task::current().unwrap();
    let mut file_table = current.as_thread_local().unwrap().borrow_file_table_mut();
    let fd: FileDesc = fd.try_into()?;
    file_table
        .unwrap()
        .write()
        .close_file(fd)
        .map(|_closed| ())
        .ok_or_else(|| Error::with_message(Errno::EBADF, "the FD does not exist"))
}

/// Runs `f` on the per-open file object behind `fd` if it is a `T`.
///
/// Returns `EBADF` if `fd` is not open, and `EINVAL` if it is not an anonymous
/// file or holds a different per-open type. This is how a subsystem recognizes
/// descriptors created by [`create_anon_file`]; the descriptor may have been
/// passed over a unix socket, so it can be found in any process.
pub fn with_anon_file_as<T: Any, R>(fd: i32, f: impl FnOnce(&T) -> R) -> Result<R> {
    let file: Arc<dyn FileLike> = {
        let current = Task::current().unwrap();
        let file_table = current.as_thread_local().unwrap().borrow_file_table();
        let fd: FileDesc = fd.try_into()?;
        file_table.unwrap().read().get_file(fd)?.clone()
    };

    let handle = file.downcast_ref::<InodeHandle>().ok_or_else(|| {
        Error::with_message(Errno::EINVAL, "the fd is not an anonymous file")
    })?;
    let ops = handle.open_file_ops().ok_or_else(|| {
        Error::with_message(Errno::EINVAL, "the anonymous file has no per-open object")
    })?;
    let typed = (ops as &dyn Any).downcast_ref::<T>().ok_or_else(|| {
        Error::with_message(Errno::EINVAL, "the anonymous file has an unexpected type")
    })?;

    Ok(f(typed))
}
