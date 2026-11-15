// SPDX-License-Identifier: MPL-2.0

//! `adjtimex(2)` and `clock_adjtime(2)`.
//!
//! The kernel keeps the wall clock as an adjustment on top of the monotonic
//! clock, so a single step is cheap. Frequency discipline is not modelled:
//! offset corrections are applied as steps and the remaining fields are
//! accepted so standard NTP clients (systemd-timesyncd) can start and step
//! the clock. This is enough for a board without an RTC; a future PLL can
//! replace the step without changing the ABI.

use core::time::Duration;

use ostd::mm::VmIo;

use super::{SyscallReturn, clock_gettime::ClockId};
use crate::{
    prelude::*,
    process::credentials::capabilities::CapSet,
    time::{SystemTime, clockid_t, timeval_t},
};

const ADJ_OFFSET: u32 = 0x0001;
const ADJ_SETOFFSET: u32 = 0x0100;
const ADJ_NANO: u32 = 0x2000;
const ADJ_OFFSET_SINGLESHOT: u32 = 0x8001;

const STA_PLL: i32 = 0x0001;

/// `struct timex` from the asm-generic ABI (64-bit `long`).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(crate) struct timex_t {
    pub modes: u32,
    pub _pad0: i32,
    pub offset: i64,
    pub freq: i64,
    pub maxerror: i64,
    pub esterror: i64,
    pub status: i32,
    pub _pad1: i32,
    pub constant: i64,
    pub precision: i64,
    pub tolerance: i64,
    pub time: timeval_t,
    pub tick: i64,
    pub ppsfreq: i64,
    pub jitter: i64,
    pub shift: i32,
    pub _pad2: i32,
    pub stabil: i64,
    pub jitcnt: i64,
    pub calcnt: i64,
    pub errcnt: i64,
    pub stbcnt: i64,
    pub tai: i32,
    pub _padding: [i32; 11],
}

pub fn sys_adjtimex(user_timex_ptr: Vaddr, ctx: &Context) -> Result<SyscallReturn> {
    adjust_timex(user_timex_ptr, ctx)
}

pub fn sys_clock_adjtime(
    clockid: clockid_t,
    user_timex_ptr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let clock_id = ClockId::try_from(clockid)?;
    if clock_id != ClockId::CLOCK_REALTIME {
        return_errno_with_message!(Errno::EINVAL, "only CLOCK_REALTIME can be adjusted");
    }
    adjust_timex(user_timex_ptr, ctx)
}

fn adjust_timex(user_timex_ptr: Vaddr, ctx: &Context) -> Result<SyscallReturn> {
    let user_space = ctx.user_space();
    let mut timex = user_space.read_val::<timex_t>(user_timex_ptr)?;

    if timex.modes == 0 {
        fill_current_time(&mut timex)?;
        user_space.write_val(user_timex_ptr, &timex)?;
        return Ok(SyscallReturn::Return(0));
    }

    let creds = ctx.posix_thread.credentials();
    if !creds.effective_capset().contains(CapSet::SYS_TIME) {
        return_errno_with_message!(Errno::EPERM, "adjtimex requires CAP_SYS_TIME");
    }

    let offset_nanos = if timex.modes & ADJ_SETOFFSET != 0 {
        // ADJ_SETOFFSET carries a signed timeval in `time`. With ADJ_NANO the
        // fractional field is already in nanoseconds (this is what
        // systemd-timesyncd sends); otherwise it is microseconds.
        let sec = timex.time.sec as i128;
        let fraction = timex.time.usec as i128;
        let nanos = if timex.modes & ADJ_NANO != 0 {
            fraction
        } else {
            fraction * 1_000
        };
        Some((sec * 1_000_000_000 + nanos).clamp(i64::MIN as i128, i64::MAX as i128) as i64)
    } else if timex.modes == ADJ_OFFSET_SINGLESHOT || timex.modes & ADJ_OFFSET != 0 {
        // ADJ_OFFSET is the local clock minus the reference; stepping must
        // move the clock the other way. ADJ_SETOFFSET above is an explicit
        // "add this much" request and keeps its sign.
        let offset = if timex.modes & ADJ_NANO != 0 {
            timex.offset
        } else {
            timex.offset.saturating_mul(1_000)
        };
        Some(-offset)
    } else {
        None
    };

    if let Some(nanos) = offset_nanos {
        apply_offset(nanos)?;
    }

    fill_current_time(&mut timex)?;
    user_space.write_val(user_timex_ptr, &timex)?;
    Ok(SyscallReturn::Return(0))
}

fn fill_current_time(timex: &mut timex_t) -> Result<()> {
    let elapsed = SystemTime::now().duration_since(&SystemTime::UNIX_EPOCH)?;
    timex.time = timeval_t {
        sec: elapsed.as_secs() as _,
        usec: elapsed.subsec_micros() as _,
    };
    timex.status = STA_PLL;
    timex.tick = 10_000;
    timex.precision = 1;
    Ok(())
}

fn apply_offset(nanos: i64) -> Result<()> {
    let now = SystemTime::now();
    let target = if nanos >= 0 {
        now.checked_add(Duration::from_nanos(nanos as u64))
    } else {
        now.checked_sub(Duration::from_nanos(nanos.unsigned_abs()))
    }
    .ok_or_else(|| Error::with_message(Errno::EINVAL, "the requested time is out of range"))?;

    SystemTime::set(target);
    Ok(())
}
