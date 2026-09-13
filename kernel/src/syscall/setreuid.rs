// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::{Uid, posix_thread::ContextPthreadAdminApi},
};

pub fn sys_setreuid(ruid: i32, euid: i32, ctx: &Context) -> Result<SyscallReturn> {
    let map_uid = |id: i32| -> Result<Option<Uid>> {
        if id < 0 {
            Ok(None)
        } else {
            ctx.thread_local
                .borrow_user_ns()
                .make_kuid(id.cast_unsigned())
                .map(Some)
        }
    };

    let ruid = map_uid(ruid)?;
    let euid = map_uid(euid)?;

    let credentials = ctx.credentials_mut();
    credentials.set_reuid(ruid, euid)?;

    Ok(SyscallReturn::Return(0))
}
