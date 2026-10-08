// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{prelude::*, process::Uid};

pub fn sys_getuid(ctx: &Context) -> Result<SyscallReturn> {
    // The ID is reported in the caller's user namespace; unmapped IDs
    // appear as the overflow ID (65534), as in Linux `from_kuid`.
    let user_ns = ctx.process.user_ns().lock();
    let uid = user_ns.map_kuid(ctx.posix_thread.credentials().ruid());

    Ok(SyscallReturn::Return(<Uid as Into<u32>>::into(uid) as _))
}
