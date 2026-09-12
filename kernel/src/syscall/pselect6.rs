// SPDX-License-Identifier: MPL-2.0

use core::time::Duration;

use ostd::mm::VmIo;

use super::{SyscallReturn, select::do_sys_select};
use crate::{
    fs::file::file_table::RawFileDesc,
    prelude::*,
    process::{posix_thread::ContextPthreadAdminApi, signal::sig_mask::SigMask},
    time::{clocks::MonotonicClock, timespec_t},
};

pub fn sys_pselect6(
    nfds: RawFileDesc,
    readfds_addr: Vaddr,
    writefds_addr: Vaddr,
    exceptfds_addr: Vaddr,
    timespec_addr: Vaddr,
    sigmask_addr: Vaddr,
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
        let sigmask_with_size = user_space.read_val::<SigMaskWithSize>(sigmask_addr)?;
        if sigmask_with_size.addr != 0 {
            if sigmask_with_size.size != size_of::<SigMask>() {
                return_errno_with_message!(Errno::EINVAL, "invalid sigmask size");
            }

            let sigmask = user_space.read_val::<SigMask>(sigmask_with_size.addr)?;
            ctx.save_and_set_sig_mask(sigmask);
        }
    }

    let result = do_sys_select(
        nfds,
        readfds_addr,
        writefds_addr,
        exceptfds_addr,
        timeout,
        ctx,
    );

    // pselect6 replays its user arguments when stopped without a caught
    // handler. Preserve time already spent waiting before that replay; libc
    // hides this Linux ABI copyback by passing a private timeout copy.
    // See Linux v6.12 fs/select.c: do_pselect and poll_select_finish.
    if let Some(duration) = timeout
        && !duration.is_zero()
    {
        let remaining = duration.saturating_sub(clock.read_time() - started.unwrap());
        if user_space
            .write_val(timespec_addr, &timespec_t::from(remaining))
            .is_err()
        {
            // Failed copyback must not replace a successful result with
            // EFAULT, but restarting with the old timeout is not allowed.
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

// Reference: <https://elixir.bootlin.com/linux/v6.19.8/source/fs/select.c#L763-L772>
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
struct SigMaskWithSize {
    addr: Vaddr,
    size: usize,
}
