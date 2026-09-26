// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::prelude::*;

/// Reports that filesystem quota control is not implemented.
///
/// Programs such as Firefox probe quota support during startup. Keeping this
/// as a named syscall preserves Linux's `ENOSYS` feature-detection contract
/// without sending every probe through the noisy unknown-syscall fallback.
pub fn sys_quotactl(
    _cmd: u64,
    _special_addr: Vaddr,
    _id: u64,
    _data_addr: Vaddr,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    debug!("quotactl called — ENOSYS");
    return_errno_with_message!(Errno::ENOSYS, "filesystem quotas are not implemented");
}
