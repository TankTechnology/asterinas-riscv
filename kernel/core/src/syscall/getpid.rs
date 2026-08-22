// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::prelude::*;

pub(super) fn sys_getpid(ctx: &Context) -> Result<SyscallReturn> {
    let process = &ctx.process;
    // Report the virtual PID in the caller's own PID namespace. The process
    // is always registered in its own namespace, so the lookup cannot fail.
    let pid = process.pid_in_ns(process.pid_ns()).unwrap();
    debug!("pid = {}", pid);
    Ok(SyscallReturn::Return(pid as _))
}
