// SPDX-License-Identifier: MPL-2.0

use core::time::Duration;

use ostd::mm::VmIo;

use super::{SyscallReturn, clock_gettime::ClockId};
use crate::{
    prelude::*,
    process::credentials::capabilities::CapSet,
    time::{SystemTime, clockid_t, timespec_t},
};

pub fn sys_clock_settime(
    clockid: clockid_t,
    timespec_addr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    debug!("clockid = {:?}", clockid);

    // Linux allows setting only CLOCK_REALTIME; every other clock ID
    // (including dynamic ones) fails with EINVAL.
    let clock_id = ClockId::try_from(clockid)?;
    if clock_id != ClockId::CLOCK_REALTIME {
        return_errno_with_message!(Errno::EINVAL, "only CLOCK_REALTIME can be set");
    }

    // Setting the wall clock requires CAP_SYS_TIME in the effective set.
    let creds = ctx.posix_thread.credentials();
    if !creds.effective_capset().contains(CapSet::SYS_TIME) {
        return_errno_with_message!(Errno::EPERM, "clock_settime requires CAP_SYS_TIME");
    }

    let timespec = ctx.user_space().read_val::<timespec_t>(timespec_addr)?;
    if timespec.sec < 0 || !(0..1_000_000_000).contains(&timespec.nsec) {
        return_errno_with_message!(Errno::EINVAL, "invalid timespec");
    }

    let target = SystemTime::UNIX_EPOCH
        .checked_add(Duration::new(timespec.sec as u64, timespec.nsec as u32))
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "the time is out of range"))?;
    SystemTime::set(target);

    Ok(SyscallReturn::Return(0))
}
