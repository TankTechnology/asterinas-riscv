// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    fs::file::file_table::{RawFileDesc, get_file_fast},
    prelude::*,
};

/// `sync_file_range2(fd, flags, offset, nbytes)` — the RISC-V / asm-generic
/// variant of `sync_file_range`. On RISC-V (and most non-x86 architectures)
/// `flags` is the *second* argument, unlike x86's
/// `sync_file_range(fd, offset, nbytes, flags)` where it is last.
///
/// Asterinas does not track per-range dirty state, so this conservatively
/// flushes the whole file's data (`fdatasync`). That is always at least as
/// strong as the requested range flush, and never weaker — the caller may get
/// more than it asked for, but never less.
pub fn sys_sync_file_range2(
    raw_fd: RawFileDesc,
    flags: u32,
    offset: u64,
    nbytes: u64,
    ctx: &Context,
) -> Result<SyscallReturn> {
    debug!(
        "raw_fd = {}, flags = {:#x}, offset = {}, nbytes = {}",
        raw_fd, flags, offset, nbytes
    );

    let mut file_table = ctx.thread_local.borrow_file_table_mut();
    let file = get_file_fast!(&mut file_table, raw_fd.try_into()?);
    let path = file.as_inode_handle_or_err()?.path();
    path.sync_data()?;

    Ok(SyscallReturn::Return(0))
}
