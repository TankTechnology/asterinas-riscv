// SPDX-License-Identifier: MPL-2.0

use core::time::Duration;

use ostd::mm::VmIo;

use super::{SyscallReturn, poll::do_sys_poll};
use crate::{
    prelude::*,
    process::{posix_thread::ContextPthreadAdminApi, signal::sig_mask::SigMask},
    time::{clocks::MonotonicClock, timer::Timeout, timespec_t},
};

pub fn sys_ppoll(
    fds: Vaddr,
    nfds: u32,
    timespec_addr: Vaddr,
    sigmask_addr: Vaddr,
    sigmask_size: usize,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let user_space = ctx.user_space();

    let timeout = if timespec_addr != 0 {
        let time_spec = user_space.read_val::<timespec_t>(timespec_addr)?;
        Some(Duration::try_from(time_spec)?)
    } else {
        None
    };
    let clock = MonotonicClock::timer_manager().clock();
    let started = timeout.map(|_| clock.read_time());

    if sigmask_addr != 0 {
        if sigmask_size != size_of::<SigMask>() {
            return_errno_with_message!(Errno::EINVAL, "invalid sigmask size");
        }

        let sigmask = user_space.read_val::<SigMask>(sigmask_addr)?;
        ctx.save_and_set_sig_mask(sigmask);
    }

    let result = do_sys_poll(fds, nfds, timeout.map(Timeout::After), ctx);

    // Unlike poll's restart block, ppoll replays the user arguments. Write back
    // its remaining duration before signal delivery; libc hides this ABI update
    // by passing a copy. STOP/CONT without a caught handler can then restart.
    // See Linux v6.12 fs/select.c: poll_select_finish.
    if let Some(duration) = timeout
        && !duration.is_zero()
    {
        let remaining = duration.saturating_sub(clock.read_time() - started.unwrap());
        if user_space
            .write_val(timespec_addr, &timespec_t::from(remaining))
            .is_err()
        {
            // A read-only timeout must not turn success into EFAULT. Since the
            // duration was not updated, an interrupted call must not restart.
            return result;
        }
    }

    result.map_err(|err| {
        if err.error() == Errno::EINTR {
            Error::new(Errno::ERESTARTNOHAND)
        } else {
            err
        }
    })
}
