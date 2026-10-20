// SPDX-License-Identifier: MPL-2.0

use core::{
    sync::atomic::{AtomicBool, AtomicU64, Ordering},
    time::Duration,
};

use ostd::mm::VmIo;

use super::{SyscallReturn, poll::do_sys_poll};
use crate::{
    prelude::*,
    process::{posix_thread::ContextPthreadAdminApi, signal::sig_mask::SigMask},
    time::{clocks::MonotonicClock, timer::Timeout, timespec_t},
};

// Opt-in boundary tracing for the short Firefox probe.  This deliberately
// records only syscall entry/return and never changes ppoll's wait semantics.
// It lets a bounded run distinguish a thread stuck inside Poller::wait from a
// userspace path that returned from ppoll but failed to process the socket.
static PPOLL_PROFILE: AtomicBool = AtomicBool::new(false);
static PPOLL_CALLS: AtomicU64 = AtomicU64::new(0);
static PPOLL_RET_ZERO: AtomicU64 = AtomicU64::new(0);
static PPOLL_RET_READY: AtomicU64 = AtomicU64::new(0);
static PPOLL_RET_ERROR: AtomicU64 = AtomicU64::new(0);
static PPOLL_DETAIL_MIN_PID: spin::Once<u32> = spin::Once::new();
const PPOLL_PROFILE_LOG_INTERVAL: u64 = 8_192;

aster_cmdline::define_flag_param_early!("asterinas.ppoll_profile", PPOLL_PROFILE);
aster_cmdline::define_kv_param!("asterinas.ppoll_detail_min_pid", PPOLL_DETAIL_MIN_PID);

fn ppoll_detail_enabled(pid: u32) -> bool {
    PPOLL_DETAIL_MIN_PID
        .get()
        .is_none_or(|minimum| pid >= *minimum)
}

fn ppoll_profile_record_result(result: &Result<SyscallReturn>) {
    match result {
        Ok(SyscallReturn::Return(value)) if *value == 0 => {
            PPOLL_RET_ZERO.fetch_add(1, Ordering::Relaxed);
        }
        Ok(SyscallReturn::Return(_)) => {
            PPOLL_RET_READY.fetch_add(1, Ordering::Relaxed);
        }
        Ok(_) => {}
        Err(_) => {
            PPOLL_RET_ERROR.fetch_add(1, Ordering::Relaxed);
        }
    }
}

fn ppoll_profile_log(
    call: u64,
    phase: &str,
    nfds: u32,
    timeout: Option<Duration>,
    result: Option<&Result<SyscallReturn>>,
    ctx: &Context,
) {
    let (zero, ready, error_count) = if result.is_some() {
        (
            PPOLL_RET_ZERO.load(Ordering::Relaxed),
            PPOLL_RET_READY.load(Ordering::Relaxed),
            PPOLL_RET_ERROR.load(Ordering::Relaxed),
        )
    } else {
        (0, 0, 0)
    };
    if let Some(result) = result {
        match result {
            Ok(SyscallReturn::Return(value)) => ostd::error!(
                "ASTERINAS_PPOLL_PROFILE call={} phase={} pid={} tid={} nfds={} timeout={:?} return={} ret_zero={} ret_ready={} ret_error={}",
                call,
                phase,
                ctx.process.pid(),
                ctx.posix_thread.tid(),
                nfds,
                timeout,
                value,
                zero,
                ready,
                error_count,
            ),
            Ok(_) => ostd::error!(
                "ASTERINAS_PPOLL_PROFILE call={} phase={} pid={} tid={} nfds={} timeout={:?} return=non-value ret_zero={} ret_ready={} ret_error={}",
                call,
                phase,
                ctx.process.pid(),
                ctx.posix_thread.tid(),
                nfds,
                timeout,
                zero,
                ready,
                error_count,
            ),
            Err(errno) => ostd::error!(
                "ASTERINAS_PPOLL_PROFILE call={} phase={} pid={} tid={} nfds={} timeout={:?} errno={:?} ret_zero={} ret_ready={} ret_error={}",
                call,
                phase,
                ctx.process.pid(),
                ctx.posix_thread.tid(),
                nfds,
                timeout,
                errno.error(),
                zero,
                ready,
                error_count,
            ),
        }
    } else {
        ostd::error!(
            "ASTERINAS_PPOLL_PROFILE call={} phase={} pid={} tid={} nfds={} timeout={:?}",
            call,
            phase,
            ctx.process.pid(),
            ctx.posix_thread.tid(),
            nfds,
            timeout,
        );
    }
}

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
    let profile_call =
        if PPOLL_PROFILE.load(Ordering::Relaxed) && ppoll_detail_enabled(ctx.process.pid()) {
            let call = PPOLL_CALLS.fetch_add(1, Ordering::Relaxed) + 1;
            if call <= 16 || call.is_multiple_of(PPOLL_PROFILE_LOG_INTERVAL) {
                ppoll_profile_log(call, "enter", nfds, timeout, None, ctx);
            }
            Some(call)
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

    if profile_call.is_some() {
        ppoll_profile_record_result(&result);
    }

    if let Some(call) = profile_call
        && (call <= 16 || call.is_multiple_of(PPOLL_PROFILE_LOG_INTERVAL))
    {
        ppoll_profile_log(call, "return", nfds, timeout, Some(&result), ctx);
    }

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
