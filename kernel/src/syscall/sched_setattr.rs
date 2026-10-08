// SPDX-License-Identifier: MPL-2.0

use super::{
    SyscallReturn,
    sched_getattr::{
        SCHED_FLAG_RESET_ON_FORK, read_linux_sched_attr_from_user, update_sched_attr_with,
    },
};
use crate::{prelude::*, sched::SchedPolicy, thread::Tid};

pub fn sys_sched_setattr(
    tid: Tid,
    addr: Vaddr,
    flags: u32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    if addr == 0 {
        return_errno_with_message!(Errno::EINVAL, "invalid user space address");
    }
    if flags != 0 {
        // Linux also has no support for any flags yet.
        return_errno_with_message!(Errno::EINVAL, "invalid flags");
    }

    let attr = read_linux_sched_attr_from_user(addr, ctx)?;

    // `SCHED_FLAG_RESET_ON_FORK` is carried in `sched_flags` (see
    // `<linux/sched/types.h>`), unlike the legacy `sched_setscheduler` API
    // which folds it into the `policy` argument.
    let reset_on_fork = (attr.sched_flags & SCHED_FLAG_RESET_ON_FORK) != 0;

    let policy = SchedPolicy::try_from(attr)?;
    update_sched_attr_with(tid, ctx, Some(reset_on_fork), |_| Ok(policy))?;

    Ok(SyscallReturn::Return(0))
}
