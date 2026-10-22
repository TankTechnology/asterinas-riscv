// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    fs::file::file_table::{RawFileDesc, get_file_fast},
    prelude::*,
};

/// Waits for and/or initiates writeback of a file range.
///
/// This kernel writes through to the backing store, so there is nothing to
/// schedule: the call only validates its arguments. The flags are
/// `SYNC_FILE_RANGE_WAIT_BEFORE | WRITE | WAIT_AFTER`.
pub(super) fn sys_sync_file_range(
    raw_fd: RawFileDesc,
    offset: i64,
    nbytes: i64,
    flags: u32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    debug!(
        "raw_fd = {}, offset = {}, nbytes = {}, flags = {}",
        raw_fd, offset, nbytes, flags
    );

    if offset < 0 || nbytes < 0 {
        return_errno_with_message!(Errno::EINVAL, "offset and nbytes must not be negative");
    }
    if flags & !0x7 != 0 {
        return_errno_with_message!(Errno::EINVAL, "invalid flags");
    }

    let mut file_table = ctx.thread_local.borrow_file_table_mut();
    let _file = get_file_fast!(&mut file_table, raw_fd.try_into()?);

    Ok(SyscallReturn::Return(0))
}
