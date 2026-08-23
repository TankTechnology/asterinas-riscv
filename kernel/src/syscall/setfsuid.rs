// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::{Uid, posix_thread::ContextPthreadAdminApi},
};

pub fn sys_setfsuid(uid: i32, ctx: &Context) -> Result<SyscallReturn> {
    let fsuid = if uid >= 0 {
        Some(
            ctx.thread_local
                .borrow_user_ns()
                .make_kuid(uid.cast_unsigned())?,
        )
    } else {
        None
    };

    let old_fsuid = {
        let credentials = ctx.credentials_mut();
        credentials
            .set_fsuid(fsuid)
            .unwrap_or_else(|old_fsuid| old_fsuid)
    };

    Ok(SyscallReturn::Return(
        <Uid as Into<u32>>::into(old_fsuid) as _
    ))
}
