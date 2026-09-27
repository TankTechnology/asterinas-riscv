// SPDX-License-Identifier: MPL-2.0

use ostd::power::{poweroff, restart, ExitCode};

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::{credentials::capabilities::CapSet, UserNamespace},
    security::lsm::hooks as lsm_hooks,
};

// Linux reboot magic constants.
const LINUX_REBOOT_MAGIC1: u32 = 0xfee1dead;
const LINUX_REBOOT_MAGIC2: u32 = 0x28121969;
const LINUX_REBOOT_MAGIC2A: u32 = 0x05121996;
const LINUX_REBOOT_MAGIC2B: u32 = 0x16041998;
const LINUX_REBOOT_MAGIC2C: u32 = 0x20112000;

/// Linux reboot commands.
#[repr(u32)]
#[derive(Clone, Copy, Debug, Eq, PartialEq, TryFromInt)]
enum RebootCmd {
    Restart = 0x01234567,
    Halt = 0xcdef0123,
    PowerOff = 0x4321fedc,
    // TODO: Add more reboot sub-commands.
}

pub fn sys_reboot(
    magic1: u32,
    magic2: u32,
    op: u32,
    _arg: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    debug!(
        "[sys_reboot]: magic1 = {:#x}, magic2 = {:#x}, op = {:#x}",
        magic1, magic2, op
    );

    // Verify magic numbers
    if magic1 != LINUX_REBOOT_MAGIC1 {
        return_errno_with_message!(Errno::EINVAL, "the reboot magic is invalid");
    }
    if magic2 != LINUX_REBOOT_MAGIC2
        && magic2 != LINUX_REBOOT_MAGIC2A
        && magic2 != LINUX_REBOOT_MAGIC2B
        && magic2 != LINUX_REBOOT_MAGIC2C
    {
        return_errno_with_message!(Errno::EINVAL, "the reboot magic is invalid");
    }

    lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
        UserNamespace::get_init_singleton().as_ref(),
        ctx.posix_thread,
        CapSet::SYS_BOOT,
    ))?;

    let cmd = RebootCmd::try_from(op)?;

    // A direct reboot(2) must persist dirty filesystems too: callers may not
    // have run sync(2), and the power operation does not return on success.
    // Propagate flush errors instead of silently losing data on restart.
    super::sync::sys_sync(ctx)?;

    match cmd {
        RebootCmd::Restart => restart(ExitCode::Success),
        RebootCmd::Halt | RebootCmd::PowerOff => poweroff(ExitCode::Success),
    }
}
