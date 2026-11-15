// SPDX-License-Identifier: MPL-2.0

//! System V shared-memory control (`shmctl`).
//!
//! Segment management still depends on the older IPC namespace layout, so the
//! syscall reports `ENOSYS` until that is re-based.

use super::SyscallReturn;
use crate::prelude::*;

pub(super) fn sys_shmctl(
    _shmid: i32,
    _command: i32,
    _buf_addr: Vaddr,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "SysV shared memory control is not implemented yet");
}
