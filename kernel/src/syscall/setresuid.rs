// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::{posix_thread::ContextPthreadAdminApi, Uid},
};

pub fn sys_setresuid(ruid: i32, euid: i32, suid: i32, ctx: &Context) -> Result<SyscallReturn> {
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
    let suid = map_uid(suid)?;

    let credentials = ctx.credentials_mut();
    credentials.set_resuid(ruid, euid, suid)?;

    Ok(SyscallReturn::Return(0))
}
