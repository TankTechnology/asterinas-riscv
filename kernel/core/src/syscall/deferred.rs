// SPDX-License-Identifier: MPL-2.0

//! Stubs for syscalls whose implementation is still being re-based.
//!
//! Each of these returns `ENOSYS`, which user space treats as "this syscall is
//! not available", exactly as a kernel built without the feature would.

use super::SyscallReturn;
use crate::prelude::*;

pub(super) fn sys_timer_getoverrun(_timer_id: i32, _ctx: &Context) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "timer_getoverrun is not implemented yet");
}

pub(super) fn sys_syslog(
    _log_type: i32,
    _buf_ptr: Vaddr,
    _len: usize,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "syslog is not implemented yet");
}

pub(super) fn sys_restart_syscall(_ctx: &Context) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "restart_syscall is not implemented yet");
}
