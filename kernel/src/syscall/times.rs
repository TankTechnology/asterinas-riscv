// SPDX-License-Identifier: MPL-2.0

//! Process CPU accounting for the POSIX `times(2)` interface.

use core::time::Duration;

use ostd::mm::VmIo;

use super::SyscallReturn;
use crate::{prelude::*, time::clocks::MonotonicClock};

/// Linux exposes process CPU times in USER_HZ (100 Hz) units, independently
/// of the kernel's scheduler tick frequency.
const USER_HZ: u64 = 100;

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct tms_t {
    pub tms_utime: i64,
    pub tms_stime: i64,
    pub tms_cutime: i64,
    pub tms_cstime: i64,
}

fn duration_to_ticks(duration: Duration) -> i64 {
    let ticks = duration.as_nanos().saturating_mul(USER_HZ as u128) / 1_000_000_000;
    i64::try_from(ticks).unwrap_or(i64::MAX)
}

/// Return process and reaped-child CPU time, plus elapsed real time.
pub fn sys_times(buf_addr: Vaddr, ctx: &Context) -> Result<SyscallReturn> {
    // Account the current kernel segment before taking the snapshot. This is
    // important for callers that repeatedly invoke `times()` to generate
    // measurable system CPU time.
    ctx.posix_thread.account_cpu_time();

    let process = ctx.process.as_ref();
    let (child_user, child_system) = process.reaped_children_stats().lock().get();
    let tms = tms_t {
        tms_utime: duration_to_ticks(process.prof_clock().user_clock().read_time()),
        tms_stime: duration_to_ticks(process.prof_clock().kernel_clock().read_time()),
        tms_cutime: duration_to_ticks(child_user),
        tms_cstime: duration_to_ticks(child_system),
    };

    ctx.user_space().write_val(buf_addr, &tms)?;

    let elapsed = duration_to_ticks(MonotonicClock::get().read_time());
    Ok(SyscallReturn::Return(elapsed as isize))
}

#[cfg(ktest)]
mod tests {
    use core::time::Duration;

    use super::duration_to_ticks;

    #[test]
    fn times_uses_user_hz_not_scheduler_hz() {
        assert_eq!(duration_to_ticks(Duration::from_secs(1)), 100);
        assert_eq!(duration_to_ticks(Duration::from_millis(9)), 0);
        assert_eq!(duration_to_ticks(Duration::from_millis(10)), 1);
    }
}
