// SPDX-License-Identifier: MPL-2.0

//! Restartable sequences feature probe.
//!
//! Debian's libc probes `rseq(2)` during process startup. Returning the Linux
//! feature-detection error through a named syscall keeps the behavior explicit
//! and avoids routing a valid syscall number through the unknown-syscall path.

use super::SyscallReturn;
use crate::prelude::*;

pub(super) fn sys_rseq(
    _rseq_ptr: Vaddr,
    _rseq_len: usize,
    _flags: u32,
    _sig: u32,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "restartable sequences are not implemented");
}
