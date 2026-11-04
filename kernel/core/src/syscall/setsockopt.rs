// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    fs::file::file_table::{RawFileDesc, get_file_fast},
    prelude::*,
    util::net::{CSocketOptionLevel, new_raw_socket_option},
};

pub(super) fn sys_setsockopt(
    sockfd: RawFileDesc,
    level: i32,
    optname: i32,
    optval: Vaddr,
    optlen: i32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    // `setsockopt(2)` reports an unknown level as `ENOPROTOOPT` (unlike
    // `getsockopt(2)`, which uses `EOPNOTSUPP`).
    let level = CSocketOptionLevel::try_from(level).map_err(|_| Errno::ENOPROTOOPT)?;

    if optlen < 0 {
        return_errno_with_message!(Errno::EINVAL, "optlen is negative");
    }
    let optlen = optlen.cast_unsigned();

    debug!(
        "level = {:?}, sockfd = {}, optname = {}, optval = {}",
        level, sockfd, optname, optlen
    );

    // Validate optval pointer before option-name lookup: Linux returns EFAULT
    // for a NULL buffer even when the option name is unknown.
    if optval == 0 && optlen > 0 {
        return_errno_with_message!(Errno::EFAULT, "optval is NULL");
    }

    // The socket-level options all take an integer value; Linux rejects an
    // empty one with EINVAL before dispatching on the option name.
    if level == CSocketOptionLevel::SOL_SOCKET && optlen == 0 {
        return_errno_with_message!(Errno::EINVAL, "the option length is zero");
    }

    let mut file_table = ctx.thread_local.borrow_file_table_mut();
    let file = get_file_fast!(&mut file_table, sockfd.try_into()?);
    let socket = file.as_socket_or_err()?;

    let raw_option = {
        // An unsupported level is `ENOPROTOOPT` for `setsockopt(2)`.
        let mut option =
            new_raw_socket_option(level, optname).map_err(|err| match err.error() {
                Errno::EOPNOTSUPP => Error::with_message(Errno::ENOPROTOOPT, "unsupported level"),
                _ => err,
            })?;
        option.read_from_user(optval, optlen)?;
        option
    };
    debug!("raw option: {:?}", raw_option);

    socket.set_option(raw_option.as_sock_option())?;

    Ok(SyscallReturn::Return(0))
}
