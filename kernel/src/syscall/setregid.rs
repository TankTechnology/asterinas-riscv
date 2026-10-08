// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::{Gid, posix_thread::ContextPthreadAdminApi},
};

pub fn sys_setregid(rgid: i32, egid: i32, ctx: &Context) -> Result<SyscallReturn> {
    let map_gid = |id: i32| -> Result<Option<Gid>> {
        if id < 0 {
            Ok(None)
        } else {
            ctx.thread_local
                .borrow_user_ns()
                .make_kgid(id.cast_unsigned())
                .map(Some)
        }
    };

    let rgid = map_gid(rgid)?;
    let egid = map_gid(egid)?;

    let credentials = ctx.credentials_mut();
    credentials.set_regid(rgid, egid)?;

    Ok(SyscallReturn::Return(0))
}
