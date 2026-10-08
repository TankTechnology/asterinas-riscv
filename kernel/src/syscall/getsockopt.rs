// SPDX-License-Identifier: MPL-2.0

use ostd::mm::VmIo;

use super::SyscallReturn;
use crate::{
    fs::file::file_table::{RawFileDesc, get_file_fast},
    prelude::*,
    util::net::{CSocketOptionLevel, new_raw_socket_option},
};

pub fn sys_getsockopt(
    sockfd: RawFileDesc,
    level: i32,
    optname: i32,
    optval: Vaddr,
    optlen_addr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let level = CSocketOptionLevel::try_from(level).map_err(|_| Errno::EOPNOTSUPP)?;

    let user_space = ctx.user_space();
    let optlen: u32 = user_space.read_val(optlen_addr)?;

    debug!("level = {level:?}, sockfd = {sockfd}, optname = {optname:?}, optlen = {optlen}");

    // Validate optval pointer before option-name lookup: Linux returns EFAULT
    // for a NULL buffer even when the option name is unknown. A NULL optval
    // with a zero length is legal, though: it is the size-query form used by
    // e.g. NETLINK_LIST_MEMBERSHIPS.
    if optval == 0 && optlen > 0 {
        return_errno_with_message!(Errno::EFAULT, "optval is NULL");
    }

    let mut file_table = ctx.thread_local.borrow_file_table_mut();
    let file = get_file_fast!(&mut file_table, sockfd.try_into()?);
    let socket = file.as_socket_or_err()?;

    let mut raw_option = new_raw_socket_option(level, optname)?;
    debug!("raw option: {:?}", raw_option);

    socket.get_option(raw_option.as_sock_option_mut())?;

    let write_len = {
        let mut new_opt_len = optlen;
        let res = raw_option.write_to_user(optval, &mut new_opt_len);
        if new_opt_len != optlen {
            user_space.write_val(optlen_addr, &new_opt_len)?;
        }
        res?
    };
    user_space.write_val(optlen_addr, &(write_len as u32))?;

    Ok(SyscallReturn::Return(0))
}
