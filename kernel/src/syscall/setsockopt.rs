// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    fs::file::file_table::{RawFileDesc, get_file_fast},
    prelude::*,
    util::net::{CSocketOptionLevel, new_raw_socket_option},
};

pub fn sys_setsockopt(
    sockfd: RawFileDesc,
    level: i32,
    optname: i32,
    optval: Vaddr,
    optlen: u32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let level = CSocketOptionLevel::try_from(level).map_err(|_| Errno::EOPNOTSUPP)?;

    debug!(
        "level = {:?}, sockfd = {}, optname = {}, optval = {}",
        level, sockfd, optname, optlen
    );

    // Validate optval pointer before option-name lookup: Linux returns EFAULT
    // for a NULL buffer even when the option name is unknown.
    if optval == 0 && optlen > 0 {
        return_errno_with_message!(Errno::EFAULT, "optval is NULL");
    }

    let mut file_table = ctx.thread_local.borrow_file_table_mut();
    let file = get_file_fast!(&mut file_table, sockfd.try_into()?);
    let socket = file.as_socket_or_err()?;

    let raw_option = {
        let mut option = new_raw_socket_option(level, optname)?;
        option.read_from_user(optval, optlen)?;
        option
    };
    debug!("raw option: {:?}", raw_option);

    socket.set_option(raw_option.as_sock_option())?;

    Ok(SyscallReturn::Return(0))
}
