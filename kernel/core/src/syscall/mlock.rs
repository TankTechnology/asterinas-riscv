// SPDX-License-Identifier: MPL-2.0

//! `mlock(2)` family.
//!
//! Memory locking is not implemented yet: the VMAR keeps no locked-memory
//! accounting, so the syscalls report `ENOSYS` rather than pretending that the
//! pages are pinned.

use super::SyscallReturn;
use crate::prelude::*;

pub fn sys_mlock(_addr: Vaddr, _len: usize, _ctx: &Context) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "mlock is not implemented yet");
}

pub fn sys_munlock(_addr: Vaddr, _len: usize, _ctx: &Context) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "munlock is not implemented yet");
}

pub fn sys_mlockall(_flags: i32, _ctx: &Context) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "mlockall is not implemented yet");
}

pub fn sys_munlockall(_ctx: &Context) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "munlockall is not implemented yet");
}
