// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    bpf::prog::ProgFile,
    fs::file::file_table::{RawFileDesc, get_file_fast},
    net::socket::options::AttachBpf,
    prelude::*,
    util::net::{CSocketOptionLevel, new_raw_socket_option},
};

/// `SO_ATTACH_BPF`: the payload is an eBPF program file descriptor.
const SO_ATTACH_BPF: i32 = 50;

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

    // The descriptor is taken out of the table before anything else is looked
    // up: resolving the program below needs the table again, and the thread's
    // table can only be borrowed once.
    let file = {
        let mut file_table = ctx.thread_local.borrow_file_table_mut();
        get_file_fast!(&mut file_table, sockfd.try_into()?).into_owned()
    };
    let socket = file.as_socket_or_err()?;

    // Unlike every other socket option, this one's payload is a file
    // descriptor rather than a value, so it is resolved here.
    if level == CSocketOptionLevel::SOL_SOCKET && optname == SO_ATTACH_BPF {
        if optlen != size_of::<i32>() as u32 {
            return_errno_with_message!(Errno::EINVAL, "SO_ATTACH_BPF takes one descriptor");
        }
        let user_space = ctx.user_space();
        let mut reader = user_space.reader(optval, size_of::<i32>())?;
        let program_fd: i32 = reader.read_val()?;

        let program_file = {
            let mut file_table = ctx.thread_local.borrow_file_table_mut();
            get_file_fast!(&mut file_table, program_fd.try_into()?).into_owned()
        };
        let program_file = program_file.downcast_ref::<ProgFile>().ok_or_else(|| {
            Error::with_message(Errno::EBADF, "the descriptor is not a bpf program")
        })?;
        let mut option = AttachBpf::new();
        option.set(program_file.program().clone());
        socket.set_option(&option)?;
        return Ok(SyscallReturn::Return(0));
    }

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
