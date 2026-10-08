// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    fs::{
        file::{
            file_table::{get_file_fast, RawFileDesc},
            StatusFlags,
        },
        vfs::file_system::sync_all_live_file_systems,
    },
    prelude::*,
};

pub fn sys_sync(_ctx: &Context) -> Result<SyscallReturn> {
    sync_all_live_file_systems()?;
    Ok(SyscallReturn::Return(0))
}

pub fn sys_syncfs(raw_fd: RawFileDesc, ctx: &Context) -> Result<SyscallReturn> {
    debug!("raw_fd = {}", raw_fd);

    let mut file_table = ctx.thread_local.borrow_file_table_mut();
    let file = get_file_fast!(&mut file_table, raw_fd.try_into()?);
    if file.status_flags().contains(StatusFlags::O_PATH) {
        return_errno_with_message!(Errno::EBADF, "the file is opened as a path");
    }
    file.path().fs().sync()?;
    Ok(SyscallReturn::Return(0))
}
