// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::{posix_thread::ContextPthreadAdminApi, Gid},
};

pub fn sys_setfsgid(gid: i32, ctx: &Context) -> Result<SyscallReturn> {
    let fsgid = if gid >= 0 {
        // Unlike the other setgid-family syscalls, Linux does not report an error when the
        // requested filesystem ID is unmapped. It leaves the ID unchanged and returns the old
        // value, which is the same behavior as passing `None` to `set_fsgid` below.
        ctx.thread_local
            .borrow_user_ns()
            .make_kgid(gid.cast_unsigned())
            .ok()
    } else {
        None
    };

    let old_fsgid = {
        let credentials = ctx.credentials_mut();
        credentials
            .set_fsgid(fsgid)
            .unwrap_or_else(|old_fsgid| old_fsgid)
    };
    let old_fsgid = ctx.thread_local.borrow_user_ns().map_kgid(old_fsgid);

    Ok(SyscallReturn::Return(
        <Gid as Into<u32>>::into(old_fsgid) as _
    ))
}
