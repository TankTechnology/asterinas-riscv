// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::prelude::*;

/// process_madvise (syscall 440 in the asm-generic numbering).
pub fn sys_process_madvise(
    _pidfd: u64,
    _vec: u64,
    _vlen: u64,
    _behavior: u64,
    _flags: u32,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    debug!("process_madvise called — ENOSYS");
    return_errno_with_message!(Errno::ENOSYS, "process_madvise is not implemented");
}

/// process_mrelease (syscall 448 in the asm-generic numbering).
pub fn sys_process_mrelease(_pidfd: u64, _flags: u32, _ctx: &Context) -> Result<SyscallReturn> {
    debug!("process_mrelease called — ENOSYS");
    return_errno_with_message!(Errno::ENOSYS, "process_mrelease is not implemented");
}
