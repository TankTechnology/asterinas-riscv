// SPDX-License-Identifier: MPL-2.0

use ostd::mm::VmIo;

use super::{
    SyscallReturn,
    sched_getattr::{LinuxSchedAttr, SCHED_RESET_ON_FORK, update_sched_attr_with},
};
use crate::{prelude::*, thread::Tid};

pub fn sys_sched_setscheduler(
    tid: Tid,
    policy: i32,
    addr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    if addr == 0 {
        return_errno_with_message!(Errno::EINVAL, "invalid user space address");
    }

    let prio = ctx.user_space().read_val(addr)?;

    // The legacy `sched_setscheduler` API folds `SCHED_RESET_ON_FORK` into the
    // `policy` argument's high bit (see `<linux/sched.h>`). Strip it before
    // interpreting the policy code and record the flag separately.
    let reset_on_fork = (policy as u32) & SCHED_RESET_ON_FORK != 0;

    let attr = LinuxSchedAttr {
        sched_policy: (policy as u32) & !SCHED_RESET_ON_FORK,
        sched_priority: prio,
        ..Default::default()
    };

    let policy = attr.try_into()?;
    update_sched_attr_with(tid, ctx, Some(reset_on_fork), |_| Ok(policy))?;

    Ok(SyscallReturn::Return(0))
}
