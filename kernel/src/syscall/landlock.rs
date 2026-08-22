// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::prelude::*;

/// Landlock (syscalls 444, 445, 446 in the asm-generic numbering).
///
/// systemd probes landlock availability at startup.  Returning ENOSYS
/// matches a kernel built without CONFIG_SECURITY_LANDLOCK and is the
/// correct fallback — no landlock is better than a broken one.
pub fn sys_landlock_create_ruleset(
    _attr: u64,
    _size: u64,
    _flags: u32,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    debug!("landlock_create_ruleset called — ENOSYS");
    return_errno_with_message!(Errno::ENOSYS, "landlock is not implemented");
}

pub fn sys_landlock_add_rule(
    _ruleset_fd: u64,
    _rule_type: u64,
    _rule_attr: u64,
    _flags: u32,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "landlock is not implemented");
}

pub fn sys_landlock_restrict_self(
    _ruleset_fd: u64,
    _flags: u32,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "landlock is not implemented");
}
