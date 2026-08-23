// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::posix_thread::ContextPthreadAdminApi,
};

pub fn sys_setuid(uid: i32, ctx: &Context) -> Result<SyscallReturn> {
    if uid < 0 {
        return_errno_with_message!(Errno::EINVAL, "UIDs cannot be negative");
    }

    let uid = ctx
        .thread_local
        .borrow_user_ns()
        .make_kuid(uid.cast_unsigned())?;

    let credentials = ctx.credentials_mut();
    credentials.set_uid(uid)?;

    Ok(SyscallReturn::Return(0))
}
