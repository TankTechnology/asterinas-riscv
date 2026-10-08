// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{prelude::*, process::Gid};

pub fn sys_getgid(ctx: &Context) -> Result<SyscallReturn> {
    // The ID is reported in the caller's user namespace; unmapped IDs
    // appear as the overflow ID (65534), as in Linux `from_kgid`.
    let user_ns = ctx.process.user_ns().lock();
    let gid = user_ns.map_kgid(ctx.posix_thread.credentials().rgid());

    Ok(SyscallReturn::Return(<Gid as Into<u32>>::into(gid) as _))
}
