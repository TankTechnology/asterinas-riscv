// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    fs::{
        file::{
            InodeType, Permission,
            file_table::{RawFileDesc, get_file_fast},
        },
        vfs::{
            notify::fanotify::FanotifyFile,
            path::{EmptyPathStr, FsPath},
        },
    },
    prelude::*,
    syscall::constants::MAX_FILENAME_LEN,
};

// --- `fanotify_init` flag bits (`linux/fanotify.h`). `FAN_CLASS_NOTIF` is 0. ---
const FAN_CLOEXEC: u32 = 0x00000001;
const FAN_NONBLOCK: u32 = 0x00000002;
const FAN_CLASS_CONTENT: u32 = 0x00000004;
const FAN_CLASS_PRE_CONTENT: u32 = 0x00000008;
const FAN_UNLIMITED_QUEUE: u32 = 0x00000010;
const FAN_UNLIMITED_MARKS: u32 = 0x00000020;
const FAN_ENABLE_AUDIT: u32 = 0x00000040;
const FAN_REPORT_FLAGS: u32 = 0x0000ff80; // REPORT_* (0x80 .. 0x4000)

/// The subset of `fanotify_init` flags this implementation accepts.
///
/// The permission classes (`FAN_CLASS_CONTENT` / `FAN_CLASS_PRE_CONTENT`), the
/// `FAN_REPORT_*` event-info flags and `FAN_ENABLE_AUDIT` are not implemented;
/// they are rejected with `EINVAL` so callers fall back to the notification
/// class.
const INIT_SUPPORTED: u32 = FAN_CLOEXEC | FAN_NONBLOCK | FAN_UNLIMITED_QUEUE | FAN_UNLIMITED_MARKS;

// --- `fanotify_mark` flag bits (`linux/fanotify.h`). ---
const FAN_MARK_ADD: u32 = 0x00000001;
const FAN_MARK_REMOVE: u32 = 0x00000002;
const FAN_MARK_DONT_FOLLOW: u32 = 0x00000004;
const FAN_MARK_ONLYDIR: u32 = 0x00000008;
const FAN_MARK_MOUNT: u32 = 0x00000010;
const FAN_MARK_IGNORED_MASK: u32 = 0x00000020;
const FAN_MARK_IGNORED_SURV_MODIFY: u32 = 0x00000040;
const FAN_MARK_FLUSH: u32 = 0x00000080;
const FAN_MARK_FILESYSTEM: u32 = 0x00000100;

const MARK_UNSUPPORTED: u32 =
    FAN_MARK_MOUNT | FAN_MARK_FILESYSTEM | FAN_MARK_IGNORED_MASK | FAN_MARK_IGNORED_SURV_MODIFY;
const MARK_ACTION_BITS: u32 = FAN_MARK_ADD | FAN_MARK_REMOVE | FAN_MARK_FLUSH;

pub fn sys_fanotify_init(flags: u32, event_f_flags: u32, ctx: &Context) -> Result<SyscallReturn> {
    debug!(
        "fanotify_init flags = {:#x}, event_f_flags = {:#x}",
        flags, event_f_flags
    );

    if flags & !INIT_SUPPORTED != 0 {
        return_errno_with_message!(Errno::EINVAL, "fanotify_init flags are not yet supported");
    }
    // `event_f_flags` carries file-status flags for the returned fd; the fd model
    // already handles non-blocking via the `FAN_NONBLOCK` init flag, so the only
    // additional value worth honouring is `O_CLOEXEC`, which fanotify conveys via
    // `FAN_CLOEXEC` instead. Ignore the raw value rather than rejecting it, since
    // Linux accepts `O_RDONLY`/`O_RDWR` here.
    let _ = event_f_flags;

    let fd_flags = if flags & FAN_CLOEXEC != 0 {
        crate::fs::file::file_table::FdFlags::CLOEXEC
    } else {
        crate::fs::file::file_table::FdFlags::empty()
    };
    let is_nonblocking = flags & FAN_NONBLOCK != 0;

    let file = FanotifyFile::new(is_nonblocking)?;
    let file_table = ctx.thread_local.borrow_file_table();
    let fd = file_table.unwrap().write().insert(file, fd_flags);
    Ok(SyscallReturn::Return(fd.into()))
}

pub fn sys_fanotify_mark(
    raw_fd: RawFileDesc,
    flags: u32,
    mask: u64,
    dirfd: RawFileDesc,
    path_addr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    debug!(
        "fanotify_mark raw_fd = {}, flags = {:#x}, mask = {:#x}, dirfd = {}, path_addr = {:#x}",
        raw_fd, flags, mask, dirfd, path_addr
    );

    if flags & MARK_UNSUPPORTED != 0 {
        return_errno_with_message!(Errno::EINVAL, "fanotify_mark flags are not yet supported");
    }

    // Exactly one of ADD / REMOVE / FLUSH may be set.
    if (flags & MARK_ACTION_BITS).count_ones() != 1 {
        return_errno_with_message!(
            Errno::EINVAL,
            "exactly one of FAN_MARK_ADD/REMOVE/FLUSH is required"
        );
    }

    let mut file_table = ctx.thread_local.borrow_file_table_mut();
    let file = get_file_fast!(&mut file_table, raw_fd.try_into()?);
    let fanotify_file = match file.downcast_ref::<FanotifyFile>() {
        Some(f) => f,
        None => return_errno_with_message!(Errno::EINVAL, "file is not a fanotify file"),
    };

    // FLUSH ignores the path and mask.
    if flags & FAN_MARK_FLUSH != 0 {
        let count = fanotify_file.flush();
        debug!("fanotify_mark FLUSH removed {} marks", count);
        return Ok(SyscallReturn::Return(0));
    }

    let path_name = ctx.user_space().read_cstring(path_addr, MAX_FILENAME_LEN)?;
    let dentry = {
        let path_name = path_name.to_string_lossy();
        let fs_path = FsPath::from_fd_at(dirfd, &path_name, EmptyPathStr::Reject)?;
        let fs_ref = ctx.thread_local.borrow_fs();
        let resolver = fs_ref.resolver().read();
        if flags & FAN_MARK_DONT_FOLLOW != 0 {
            resolver.lookup_no_follow(&fs_path)?
        } else {
            resolver.lookup(&fs_path)?
        }
    };

    // Verify the caller has read permission on the inode.
    let inode = dentry.inode();
    inode.check_permission(Permission::MAY_READ)?;

    if flags & FAN_MARK_ONLYDIR != 0 && inode.type_() != InodeType::Dir {
        return_errno_with_message!(Errno::ENOTDIR, "path is not a directory");
    }

    let mask = mask as u32;
    if flags & FAN_MARK_ADD != 0 {
        fanotify_file.add_mark(&dentry, mask)?;
    } else {
        fanotify_file.remove_mark(&dentry)?;
    }

    Ok(SyscallReturn::Return(0))
}
