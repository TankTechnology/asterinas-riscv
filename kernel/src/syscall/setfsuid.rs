// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::{posix_thread::ContextPthreadAdminApi, Uid},
};

pub fn sys_setfsuid(uid: i32, ctx: &Context) -> Result<SyscallReturn> {
    let fsuid = if uid >= 0 {
        // Unlike the other setuid-family syscalls, Linux does not report an error when the
        // requested filesystem ID is unmapped. It leaves the ID unchanged and returns the old
        // value, which is the same behavior as passing `None` to `set_fsuid` below.
        ctx.thread_local
            .borrow_user_ns()
            .make_kuid(uid.cast_unsigned())
            .ok()
    } else {
        None
    };

    let old_fsuid = {
        let credentials = ctx.credentials_mut();
        credentials
            .set_fsuid(fsuid)
            .unwrap_or_else(|old_fsuid| old_fsuid)
    };
    let old_fsuid = ctx.thread_local.borrow_user_ns().map_kuid(old_fsuid);

    Ok(SyscallReturn::Return(
        <Uid as Into<u32>>::into(old_fsuid) as _
    ))
}
