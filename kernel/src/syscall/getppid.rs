// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::prelude::*;

pub fn sys_getppid(ctx: &Context) -> Result<SyscallReturn> {
    // Report the parent's virtual PID in the caller's PID namespace. The
    // parent may live outside the caller's namespace (e.g., the parent of a
    // namespace's init process); it is then invisible and 0 is returned, as
    // in Linux.
    let caller_ns = ctx.process.pid_ns().clone();
    let ppid = ctx
        .process
        .parent()
        .lock()
        .process()
        .upgrade()
        .and_then(|parent| parent.pid_in_ns(&caller_ns))
        .unwrap_or(0);

    Ok(SyscallReturn::Return(ppid as _))
}
