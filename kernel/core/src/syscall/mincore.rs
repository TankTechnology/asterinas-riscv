// SPDX-License-Identifier: MPL-2.0

//! `mincore(2)`.
//!
//! Page-residency reporting needs the VMAR query interface to expose per-page
//! residency, which it does not yet do, so the syscall reports `ENOSYS`.

use super::SyscallReturn;
use crate::prelude::*;

pub(super) fn sys_mincore(
    _addr: Vaddr,
    _length: usize,
    _vec_ptr: Vaddr,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "mincore is not implemented yet");
}
