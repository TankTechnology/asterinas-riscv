// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::prelude::*;

/// settimeofday (syscall 170).
///
/// systemd and other programs may call this. Returning ENOSYS is safe —
/// callers fall back to clock_settime or continue without changing the time.
pub fn sys_settimeofday(_tv: u64, _tz: u64, _ctx: &Context) -> Result<SyscallReturn> {
    debug!("settimeofday called — ENOSYS (not implemented)");
    return_errno_with_message!(Errno::ENOSYS, "settimeofday is not implemented");
}
