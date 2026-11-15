// SPDX-License-Identifier: MPL-2.0

//! `shmget(2)`.
//!
//! SysV shared-memory segments are not managed by the current IPC namespace
//! layout, so the syscall reports `ENOSYS`.

use super::SyscallReturn;
use crate::prelude::*;

pub(super) fn sys_shmget(
    _key: i32,
    _size: usize,
    _shmflg: u32,
) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "SysV shared memory is not implemented yet");
}
