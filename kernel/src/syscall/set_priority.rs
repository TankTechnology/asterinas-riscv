// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::Ordering;

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::{ResourceType::RLIMIT_NICE, credentials::capabilities::CapSet},
    sched::Nice,
    syscall::get_priority::{PriorityTarget, get_processes},
};

pub fn sys_set_priority(which: i32, who: u32, prio: i32, ctx: &Context) -> Result<SyscallReturn> {
    let prio_target = PriorityTarget::new(which, who, ctx)?;
    let new_nice: Nice = {
        let nice_raw = prio.clamp(
            Nice::MIN.value().get() as i32,
            Nice::MAX.value().get() as i32,
        ) as i8;
        nice_raw.try_into().unwrap()
    };

    debug!(
        "set_priority prio_target: {:?}, new_nice: {:?}",
        prio_target, new_nice
    );

    // Like Linux, lowering the nice value (raising the priority) below the
    // RLIMIT_NICE-derived limit is only allowed with CAP_SYS_NICE. This is what
    // makes `nice(2)` work for root: libc implements `nice()` on top of
    // `getpriority` + `setpriority` (neither riscv64 nor x86-64 has a dedicated
    // `nice` syscall).
    let caller_caps = ctx.posix_thread.credentials().effective_capset();

    let processes = get_processes(prio_target)?;
    for process in processes.iter() {
        let rlimit = process.resource_limits();
        let limit = (rlimit.get_rlimit(RLIMIT_NICE).get_cur() as i8)
            .try_into()
            .map_err(|msg| Error::with_message(Errno::EINVAL, msg))?;

        let cur_nice = process.nice().load(Ordering::Relaxed);
        if new_nice < cur_nice && new_nice < limit && !caller_caps.contains(CapSet::SYS_NICE) {
            return_errno!(Errno::EACCES);
        }
        // FIXME: `setpriority` updates only the per-process nice value. Fair
        // scheduler state is kept in each thread's `SchedPolicy::Fair`, so it
        // should be updated from the same source of truth.
        process.nice().store(new_nice, Ordering::Relaxed);
    }

    Ok(SyscallReturn::Return(0))
}
