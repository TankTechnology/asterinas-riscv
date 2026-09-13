// SPDX-License-Identifier: MPL-2.0

use core::time::Duration;

use ostd::{mm::VmIo, sync::Waiter};

use super::{ClockId, SyscallReturn, restart_syscall::RestartBlock};
use crate::{
    prelude::*,
    time::{
        TIMER_ABSTIME, TimerManager, clockid_t,
        clocks::{BootTimeClock, MonotonicClock, RealTimeClock},
        timer::Timeout,
        timespec_t,
        wait::ManagedTimeout,
    },
};

pub fn sys_nanosleep(
    request_timespec_addr: Vaddr,
    remain_timespec_addr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let clockid = ClockId::CLOCK_MONOTONIC;

    do_clock_nanosleep(
        clockid as clockid_t,
        false,
        request_timespec_addr,
        remain_timespec_addr,
        ctx,
    )
}

pub fn sys_clock_nanosleep(
    clockid: clockid_t,
    flags: i32,
    request_timespec_addr: Vaddr,
    remain_timespec_addr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let is_abs_time = (flags & TIMER_ABSTIME) != 0;

    do_clock_nanosleep(
        clockid,
        is_abs_time,
        request_timespec_addr,
        remain_timespec_addr,
        ctx,
    )
}

fn do_clock_nanosleep(
    clockid: clockid_t,
    is_abs_time: bool,
    request_timespec_addr: Vaddr,
    remain_timespec_addr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    ctx.thread_local.restart_block().take();
    let request_time = {
        let timespec = ctx
            .user_space()
            .read_val::<timespec_t>(request_timespec_addr)?;
        Duration::try_from(timespec)?
    };

    debug!(
        "clockid = {:?}, is_abs_time = {}, request_time = {:?}, remain_timespec_addr = 0x{:x}",
        clockid, is_abs_time, request_time, remain_timespec_addr
    );

    // Relative CLOCK_REALTIME sleeps are unaffected by wall-clock adjustments.
    // Linux likewise uses the monotonic base for relative realtime hrtimers.
    let clockid = if !is_abs_time && clockid == ClockId::CLOCK_REALTIME as clockid_t {
        ClockId::CLOCK_MONOTONIC as clockid_t
    } else {
        clockid
    };
    let timer_manager = sleep_timer_manager(clockid, ctx)?;
    // Timer managers use host clocks. Relative durations have no namespace
    // offset; absolute namespace timestamps must be translated exactly once.
    let deadline = if is_abs_time {
        match ClockId::try_from(clockid)? {
            ClockId::CLOCK_MONOTONIC | ClockId::CLOCK_BOOTTIME => ctx
                .thread_local
                .borrow_ns_proxy()
                .unwrap()
                .time_ns()
                .remove_offset(
                    request_time,
                    clockid == ClockId::CLOCK_BOOTTIME as clockid_t,
                ),
            _ => request_time,
        }
    } else {
        timer_manager
            .clock()
            .read_time()
            .checked_add(request_time)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "sleep deadline overflows"))?
    };
    let request = NanosleepRestart {
        clockid,
        deadline,
        remain_timespec_addr: if is_abs_time {
            None
        } else {
            Some(remain_timespec_addr)
        },
    };
    request.restart(ctx)
}

/// An interrupted sleep's host-clock deadline, never a fresh duration.
#[derive(Clone, Copy)]
pub(crate) struct NanosleepRestart {
    clockid: clockid_t,
    deadline: Duration,
    // None denotes TIMER_ABSTIME; Some(0) is a relative sleep without copyout.
    remain_timespec_addr: Option<Vaddr>,
}

impl NanosleepRestart {
    pub(super) fn restart(self, ctx: &Context) -> Result<SyscallReturn> {
        let Self {
            clockid,
            deadline,
            remain_timespec_addr,
        } = self;
        let timer_manager = sleep_timer_manager(clockid, ctx)?;

        let waiter = Waiter::new_pair().0;
        let res = waiter.pause_until_or_timeout(
            || None,
            ManagedTimeout::new_with_manager(Timeout::When(deadline), timer_manager),
        );

        match res {
            Err(e) if e.error() == Errno::ETIME => Ok(SyscallReturn::Return(0)),
            Err(e) if e.error() == Errno::EINTR => {
                let end_time = timer_manager.clock().read_time();

                if end_time >= deadline {
                    return Ok(SyscallReturn::Return(0));
                }

                if let Some(remain_timespec_addr) = remain_timespec_addr
                    && remain_timespec_addr != 0
                {
                    let remaining_duration = deadline - end_time;
                    let remaining_timespec = timespec_t::from(remaining_duration);
                    ctx.user_space()
                        .write_val(remain_timespec_addr, &remaining_timespec)?;
                }

                // A caught handler always turns these into EINTR, even with
                // SA_RESTART. With no handler, absolute sleeps replay the original
                // call; relative sleeps use restart_syscall with this deadline.
                // See Linux v6.12 kernel/time/hrtimer.c: hrtimer_nanosleep.
                if remain_timespec_addr.is_none() {
                    return_errno_with_message!(
                        Errno::ERESTARTNOHAND,
                        "absolute sleep was interrupted"
                    );
                }
                ctx.thread_local
                    .restart_block()
                    .set(RestartBlock::Nanosleep(self));
                return_errno_with_message!(
                    Errno::ERESTART_RESTARTBLOCK,
                    "relative sleep was interrupted"
                );
            }
            Ok(()) | Err(_) => unreachable!(),
        }
    }
}

fn sleep_timer_manager<'a>(clockid: clockid_t, ctx: &'a Context) -> Result<&'a Arc<TimerManager>> {
    Ok(match ClockId::try_from(clockid)? {
        ClockId::CLOCK_BOOTTIME => BootTimeClock::timer_manager(),
        ClockId::CLOCK_MONOTONIC => MonotonicClock::timer_manager(),
        ClockId::CLOCK_REALTIME => RealTimeClock::timer_manager(),
        // FIXME: We should better not expose this prof timer manager.
        ClockId::CLOCK_PROCESS_CPUTIME_ID => {
            ctx.process.timer_manager().prof_timer().timer_manager()
        }
        ClockId::CLOCK_THREAD_CPUTIME_ID
        | ClockId::CLOCK_MONOTONIC_RAW
        | ClockId::CLOCK_REALTIME_COARSE
        | ClockId::CLOCK_MONOTONIC_COARSE => {
            return_errno_with_message!(
                Errno::EOPNOTSUPP,
                "unsupported clockid for clock_nanosleep"
            );
        }
    })
}
