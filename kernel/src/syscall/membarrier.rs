// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::prelude::*;

/// membarrier(2) — issue memory barriers on a set of threads.
///
/// Asterinas is a framekernel without JIT-compiled code or per-thread memory
/// ordering concerns beyond what the hardware already guarantees, so the
/// private-expedited commands are trivially satisfied. The global commands
/// require inter-CPU IPIs, which Asterinas does not implement; they are
/// rejected with `EINVAL`, matching Linux's behaviour on a kernel that does
/// not register those commands.
///
/// Reference: <https://man7.org/linux/man-pages/man2/membarrier.2.html>
pub fn sys_membarrier(cmd: u32, flags: u32, _cpu_id: i32, _ctx: &Context) -> Result<SyscallReturn> {
    const QUERY: u32 = 0;
    const PRIVATE_EXPEDITED: u32 = 1 << 3;
    const REGISTER_PRIVATE_EXPEDITED: u32 = 1 << 4;
    const PRIVATE_EXPEDITED_SYNC_CORE: u32 = 1 << 5;
    const REGISTER_PRIVATE_EXPEDITED_SYNC_CORE: u32 = 1 << 6;

    const SUPPORTED: u32 = PRIVATE_EXPEDITED
        | REGISTER_PRIVATE_EXPEDITED
        | PRIVATE_EXPEDITED_SYNC_CORE
        | REGISTER_PRIVATE_EXPEDITED_SYNC_CORE;

    // `MEMBARRIER_CMD_FLAG_CPU` (flags == 1) is not supported.
    if flags != 0 {
        return_errno_with_message!(Errno::EINVAL, "membarrier flags are unsupported");
    }

    match cmd {
        QUERY => Ok(SyscallReturn::Return(SUPPORTED as isize)),
        PRIVATE_EXPEDITED
        | REGISTER_PRIVATE_EXPEDITED
        | PRIVATE_EXPEDITED_SYNC_CORE
        | REGISTER_PRIVATE_EXPEDITED_SYNC_CORE => Ok(SyscallReturn::Return(0)),
        _ => return_errno_with_message!(Errno::EINVAL, "membarrier command is unsupported"),
    }
}
