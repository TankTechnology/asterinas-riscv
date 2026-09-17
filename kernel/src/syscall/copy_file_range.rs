// SPDX-License-Identifier: MPL-2.0

use ostd::mm::VmIo;

use super::SyscallReturn;
use crate::{
    fs::{
        self,
        file::{
            InodeHandle, InodeType, SeekFrom, StatusFlags,
            file_table::{RawFileDesc, WithFileTable},
        },
    },
    prelude::*,
};

/// `copy_file_range(fd_in, off_in, fd_out, off_out, len, flags)` — copies data
/// between two files without a round trip through user space.
///
/// This is a generic read/write-loop implementation (like `sendfile`): it
/// works for any pair of files, across filesystems, and for pipes when the
/// offsets are NULL. Filesystem-accelerated copies (reflinks, server-side
/// copy) are not attempted.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/fs/read_write.c#L1512>.
pub fn sys_copy_file_range(
    fd_in: RawFileDesc,
    off_in_ptr: Vaddr,
    fd_out: RawFileDesc,
    off_out_ptr: Vaddr,
    len: usize,
    flags: u32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    // The `flags` argument is reserved for future extensions and must be zero.
    if flags != 0 {
        return_errno_with_message!(Errno::EINVAL, "copy_file_range flags must be zero");
    }

    let read_offset = |ptr: Vaddr| -> Result<Option<usize>> {
        if ptr == 0 {
            return Ok(None);
        }
        let offset: i64 = ctx.user_space().read_val(ptr)?;
        if offset < 0 {
            return_errno_with_message!(Errno::EINVAL, "offset cannot be negative");
        }
        if (offset as u64).checked_add(len as u64).is_none() {
            return_errno_with_message!(Errno::EINVAL, "offset + len overflows");
        }
        Ok(Some(offset as usize))
    };
    let mut off_in = read_offset(off_in_ptr)?;
    let mut off_out = read_offset(off_out_ptr)?;

    let (in_file, out_file) = ctx
        .thread_local
        .borrow_file_table_mut()
        .read_with(|inner| {
            let in_file = inner.get_file(fd_in.try_into()?)?.clone();
            let out_file = inner.get_file(fd_out.try_into()?)?.clone();
            Ok::<_, Error>((in_file, out_file))
        })?;

    // Verify that `in_file` is readable and `out_file` is writable upfront,
    // even if `len` is zero.
    if !in_file.access_mode().is_readable() {
        return_errno_with_message!(Errno::EBADF, "fd_in is not open for reading");
    }
    if !out_file.access_mode().is_writable() {
        return_errno_with_message!(Errno::EBADF, "fd_out is not open for writing");
    }

    let in_inode = in_file
        .downcast_ref::<InodeHandle>()
        .map(|handle| handle.path().inode());
    let out_inode = out_file
        .downcast_ref::<InodeHandle>()
        .map(|handle| handle.path().inode());

    // Linux reports EISDIR when `fd_in` is a directory.
    if in_inode.is_some_and(|inode| inode.type_() == InodeType::Dir) {
        return_errno_with_message!(Errno::EISDIR, "fd_in is a directory");
    }

    // Copying to an append-only file is not allowed.
    let out_is_pipe = out_inode.is_some_and(|inode| inode.type_() == InodeType::NamedPipe);
    if !out_is_pipe && out_file.status_flags().contains(StatusFlags::O_APPEND) {
        return_errno_with_message!(Errno::EBADF, "fd_out is opened with O_APPEND");
    }

    // When an offset pointer is NULL, the file offset is used (and advanced).
    // Resolve the effective start offsets for the overlap check below.
    let effective_off_in = match off_in {
        Some(off) => Some(off),
        None => Some(in_file.seek(SeekFrom::Current(0)).map_err(|_| {
            Error::with_message(Errno::EINVAL, "fd_in is not seekable and off_in is NULL")
        })?),
    };
    let effective_off_out = match off_out {
        Some(off) => Some(off),
        None => Some(out_file.seek(SeekFrom::Current(0)).map_err(|_| {
            Error::with_message(Errno::EINVAL, "fd_out is not seekable and off_out is NULL")
        })?),
    };

    // Overlapping ranges within the same file are rejected.
    if let (Some(in_inode), Some(out_inode)) = (in_inode, out_inode)
        && Arc::ptr_eq(in_inode, out_inode)
        && len > 0
        && let (Some(start_in), Some(start_out)) = (effective_off_in, effective_off_out)
    {
        let end_in = start_in.saturating_add(len);
        let end_out = start_out.saturating_add(len);
        if start_in < end_out && start_out < end_in {
            return_errno_with_message!(
                Errno::EINVAL,
                "input and output ranges overlap within the same file"
            );
        }
    }

    const BUFFER_SIZE: usize = PAGE_SIZE;
    let mut buffer = vec![0u8; BUFFER_SIZE].into_boxed_slice();
    let mut total_len = 0;

    while total_len < len {
        let max_readlen = buffer.len().min(len - total_len);

        let read_res = if let Some(off) = off_in {
            in_file.read_bytes_at(off, &mut buffer[..max_readlen])
        } else {
            in_file.read_bytes(&mut buffer[..max_readlen])
        };

        let read_len = match read_res {
            Ok(0) => break,
            Ok(len) => len,
            Err(e) => {
                if total_len > 0 {
                    warn!("error occurs when trying to read file: {:?}", e);
                    break;
                }
                return Err(e);
            }
        };

        // Short reads and short writes are both acceptable; the number of
        // bytes actually copied is returned to user space.
        let write_res = if let Some(off) = off_out {
            out_file.write_bytes_at(off, &buffer[..read_len])
        } else {
            out_file.write_bytes(&buffer[..read_len])
        };

        match write_res {
            Ok(len) => {
                total_len += len;
                if let Some(off) = off_in.as_mut() {
                    *off += len;
                }
                if let Some(off) = off_out.as_mut() {
                    *off += len;
                }
                if len < read_len {
                    break;
                }
            }
            Err(e) => {
                if total_len > 0 {
                    warn!("error occurs when trying to write file: {:?}", e);
                    break;
                }
                return Err(e);
            }
        }
    }

    if off_in_ptr != 0 {
        ctx.user_space()
            .write_val(off_in_ptr, &(off_in.unwrap() as i64))?;
    }
    if off_out_ptr != 0 {
        ctx.user_space()
            .write_val(off_out_ptr, &(off_out.unwrap() as i64))?;
    }

    fs::vfs::notify::on_access(&in_file);
    fs::vfs::notify::on_modify(&out_file);

    Ok(SyscallReturn::Return(total_len as _))
}
