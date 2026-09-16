// SPDX-License-Identifier: MPL-2.0

use super::{
    SyscallReturn,
    sched_getattr::{LinuxSchedAttr, SCHED_RESET_ON_FORK, access_sched_attr_with},
};
use crate::{prelude::*, thread::Tid};

pub fn sys_sched_getscheduler(tid: Tid, ctx: &Context) -> Result<SyscallReturn> {
    let (policy, reset_on_fork) =
        access_sched_attr_with(tid, ctx, |attr| Ok((attr.policy(), attr.reset_on_fork())))?;
    let policy = LinuxSchedAttr::try_from(policy)?.sched_policy;
    let reset_flag = if reset_on_fork {
        SCHED_RESET_ON_FORK
    } else {
        0
    };
    let policy = policy | reset_flag;
    Ok(SyscallReturn::Return(policy as isize))
}
