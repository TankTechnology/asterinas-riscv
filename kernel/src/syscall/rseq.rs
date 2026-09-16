// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::prelude::*;

/// Reports that restartable sequences are unavailable.
pub fn sys_rseq(
    _rseq_ptr: Vaddr,
    _rseq_len: usize,
    _flags: u32,
    _sig: u32,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    // Success would permit userspace to rely on CPU-ID updates and critical-section
    // aborts on preemption, migration, and signal delivery. Until that protocol is
    // implemented, ENOSYS keeps libc on its fallback paths without touching memory.
    // See https://github.com/torvalds/linux/blob/v6.12/include/uapi/linux/rseq.h.
    return_errno_with_message!(Errno::ENOSYS, "restartable sequences are not implemented");
}
