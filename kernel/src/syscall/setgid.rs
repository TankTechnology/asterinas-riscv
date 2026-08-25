// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::posix_thread::ContextPthreadAdminApi,
};

pub fn sys_setgid(gid: i32, ctx: &Context) -> Result<SyscallReturn> {
    if gid < 0 {
        return_errno_with_message!(Errno::EINVAL, "GIDs cannot be negative");
    }

    let gid = ctx
        .thread_local
        .borrow_user_ns()
        .make_kgid(gid.cast_unsigned())?;

    let credentials = ctx.credentials_mut();
    credentials.set_gid(gid)?;

    Ok(SyscallReturn::Return(0))
}
