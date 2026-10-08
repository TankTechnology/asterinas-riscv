// SPDX-License-Identifier: MPL-2.0

//! `name_to_handle_at` and `open_by_handle_at`: produce and consume opaque file
//! handles so user space (e.g. systemd) can identify and re-open a file by a
//! stable identity rather than by path.
//!
//! A file handle is a `struct file_handle` in user space:
//!
//! ```c
//! struct file_handle {
//!     unsigned int handle_bytes; // size of f_handle[]
//!     int handle_type;           // opaque type tag
//!     unsigned char f_handle[];  // opaque payload
//! };
//! ```
//!
//! Asterinas encodes a bare little-endian 64-bit inode number as the payload.
//! The encoding and decoding are entirely in-kernel, so the tag and payload are
//! opaque to user space; they only need to round-trip through
//! `open_by_handle_at`.

use ostd::mm::VmIo;

use super::SyscallReturn;
use crate::{
    fs::{
        file::{
            CreationFlags, FileLike, InodeMode, OpenArgs,
            file_table::{FdFlags, RawFileDesc, get_file_fast},
        },
        vfs::path::{EmptyPathStr, FsPath, Path},
    },
    prelude::*,
    syscall::constants::MAX_FILENAME_LEN,
};

/// The `handle_type` tag written into `struct file_handle`. Opaque to user space.
const FILE_HANDLE_TYPE_INO64: i32 = 0x81;

/// The size (in bytes) of the opaque `f_handle` payload: a little-endian `u64` inode number.
const FILE_HANDLE_SIZE: u32 = 8;

bitflags! {
    struct NameToHandleFlags: u32 {
        const AT_EMPTY_PATH = 0x1000;
        const AT_SYMLINK_FOLLOW = 0x200;
    }
}

pub fn sys_name_to_handle_at(
    dirfd: RawFileDesc,
    pathname_ptr: Vaddr,
    handle_ptr: Vaddr,
    mount_id_ptr: Vaddr,
    flags: u32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let user_space = ctx.user_space();
    let pathname = user_space.read_cstring(pathname_ptr, MAX_FILENAME_LEN)?;
    let flags = NameToHandleFlags::from_bits(flags)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "invalid name_to_handle_at flags"))?;

    // Resolve the path to an inode. By default the final component is *not*
    // followed when it is a symlink; `AT_SYMLINK_FOLLOW` opts in to following.
    let path = {
        let pathname = pathname.to_string_lossy();
        let fs_path = FsPath::from_fd_at(
            dirfd,
            pathname.as_ref(),
            EmptyPathStr::AllowIfFlag(flags.bits()),
        )?;
        let fs_ref = ctx.thread_local.borrow_fs();
        let resolver = fs_ref.resolver().read();
        if flags.contains(NameToHandleFlags::AT_SYMLINK_FOLLOW) {
            resolver.lookup(&fs_path)?
        } else {
            resolver.lookup_no_follow(&fs_path)?
        }
    };

    let handle = path.inode().encode_file_handle()?;

    // `struct file_handle` starts with `handle_bytes`, then `handle_type`, then
    // the `f_handle` payload. If the user buffer is too small, report the
    // required size via `EOVERFLOW` and update `handle_bytes` so the caller can
    // retry with a larger buffer.
    let handle_bytes: u32 = user_space.read_val(handle_ptr)?;
    if handle_bytes < FILE_HANDLE_SIZE {
        user_space.write_val(handle_ptr, &FILE_HANDLE_SIZE)?;
        return_errno_with_message!(Errno::EOVERFLOW, "file handle buffer too small");
    }
    user_space.write_val(handle_ptr, &FILE_HANDLE_SIZE)?;
    user_space.write_val(handle_ptr + 4, &FILE_HANDLE_TYPE_INO64)?;
    user_space.write_bytes(handle_ptr + 8, handle.as_slice())?;

    // The mount id lets `open_by_handle_at` pin the right mount to interpret
    // the handle against.
    let mount_id = path.mount_node().id() as i32;
    user_space.write_val(mount_id_ptr, &mount_id)?;

    Ok(SyscallReturn::Return(0))
}

pub fn sys_open_by_handle_at(
    mount_fd: RawFileDesc,
    handle_ptr: Vaddr,
    flags: u32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let user_space = ctx.user_space();

    // Read and validate the handle.
    let handle_bytes: u32 = user_space.read_val(handle_ptr)?;
    let handle_type: i32 = user_space.read_val(handle_ptr + 4)?;
    if handle_type != FILE_HANDLE_TYPE_INO64 {
        return_errno_with_message!(Errno::ESTALE, "unrecognized file handle type");
    }
    if handle_bytes < FILE_HANDLE_SIZE {
        return_errno_with_message!(Errno::ESTALE, "truncated file handle");
    }
    let mut fh = [0u8; FILE_HANDLE_SIZE as usize];
    user_space.read_bytes(handle_ptr + 8, &mut fh)?;

    // `mount_fd` is any fd on the target mount; its filesystem interprets the
    // handle and recovers the inode.
    let (mount, inode) = {
        let mut file_table = ctx.thread_local.borrow_file_table_mut();
        let file = get_file_fast!(&mut file_table, mount_fd.try_into()?);
        let path = file.as_inode_handle_or_err()?.path();
        let mount = path.mount_node().clone();
        let inode = path.inode().fs().fh_to_inode(&fh)?;
        (mount, inode)
    };

    let open_args = OpenArgs::from_flags_and_mode(flags, InodeMode::from_bits_truncate(0))?;
    let path = Path::from_inode_and_mount(mount, inode);
    let file_handle: Arc<dyn FileLike> = Arc::new(path.open(open_args)?);

    let fd = {
        let file_table = ctx.thread_local.borrow_file_table();
        let mut file_table_locked = file_table.unwrap().write();
        let fd_flags =
            if CreationFlags::from_bits_truncate(flags).contains(CreationFlags::O_CLOEXEC) {
                FdFlags::CLOEXEC
            } else {
                FdFlags::empty()
            };
        file_table_locked.insert(file_handle.clone(), fd_flags)
    };

    Ok(SyscallReturn::Return(fd.into()))
}
