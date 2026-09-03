// SPDX-License-Identifier: MPL-2.0

use ostd::mm::VmIo;

use super::{SyscallReturn, sched_getattr::access_sched_attr_with};
use crate::{prelude::*, sched::SchedPolicy, thread::Tid, time::timespec_t};

pub fn sys_sched_rr_get_interval(
    tid: Tid,
    interval_addr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    // Linux resolves the target task before copying the interval to userspace.
    // Reference: <https://elixir.bootlin.com/linux/v6.17.7/source/kernel/sched/syscalls.c#L1680>
    let interval = access_sched_attr_with(tid, ctx, |attr| {
        let interval = match attr.policy() {
            SchedPolicy::RealTime { rt_policy, .. } => rt_policy.time_slice_duration(),
            _ => core::time::Duration::ZERO,
        };
        Ok(interval)
    })?;

    ctx.user_space()
        .write_val(interval_addr, &timespec_t::from(interval))?;

    Ok(SyscallReturn::Return(0))
}
