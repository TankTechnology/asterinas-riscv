// SPDX-License-Identifier: MPL-2.0

//! The `readahead` system call.

use super::SyscallReturn;
use crate::{
    fs::file::{
        InodeHandle, InodeType, StatusFlags,
        file_table::{RawFileDesc, get_file_fast},
    },
    prelude::*,
};

/// Validates a Linux `readahead(2)` request.
///
/// Asterinas does not yet expose an explicit asynchronous page-cache prefetch
/// operation. This handler therefore provides the same advisory no-op behavior
/// as `POSIX_FADV_WILLNEED`, while preserving Linux's argument and file checks.
pub fn sys_readahead(
    raw_fd: RawFileDesc,
    offset: i64,
    count: usize,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let mut file_table = ctx.thread_local.borrow_file_table_mut();
    let file = get_file_fast!(&mut file_table, raw_fd.try_into()?);
    if offset < 0 {
        return_errno_with_message!(Errno::EINVAL, "offset cannot be negative");
    }
    if !file.access_mode().is_readable() || file.status_flags().contains(StatusFlags::O_PATH) {
        return_errno_with_message!(Errno::EBADF, "file is not opened for reading");
    }

    let is_regular_file = file
        .downcast_ref::<InodeHandle>()
        .is_some_and(|handle| handle.path().inode().type_() == InodeType::File);
    if !is_regular_file {
        return_errno_with_message!(Errno::EINVAL, "file does not support readahead");
    }

    debug!("raw_fd={raw_fd}, offset=0x{offset:x}, count=0x{count:x}");
    Ok(SyscallReturn::Return(0))
}
