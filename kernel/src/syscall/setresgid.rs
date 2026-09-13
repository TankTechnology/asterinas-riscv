// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::{Gid, posix_thread::ContextPthreadAdminApi},
};

pub fn sys_setresgid(rgid: i32, egid: i32, sgid: i32, ctx: &Context) -> Result<SyscallReturn> {
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
    let sgid = map_gid(sgid)?;

    let credentials = ctx.credentials_mut();
    credentials.set_resgid(rgid, egid, sgid)?;

    Ok(SyscallReturn::Return(0))
}
