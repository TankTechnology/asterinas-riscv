// SPDX-License-Identifier: MPL-2.0

use core::time::Duration;

use ostd::mm::VmIo;

use crate::{
    context::current_userspace,
    prelude::*,
    process::posix_thread::futex::{
        FutexFlags, FutexOp, FutexVisibility, futex_op_and_flags_from_u32, futex_requeue,
        futex_wait_bitset, futex_wake, futex_wake_bitset, futex_wake_op,
    },
    syscall::{SyscallReturn, restart_syscall::RestartBlock},
    time::{
        clocks::{MonotonicClock, RealTimeClock},
        timer::Timeout,
        timespec_t,
        wait::ManagedTimeout,
    },
};

pub fn sys_futex(
    futex_addr: Vaddr,
    futex_op: i32,
    futex_val: u32,
    utime_addr: Vaddr,
    futex_new_addr: Vaddr,
    bitset: u32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let (futex_op, futex_flags) = futex_op_and_flags_from_u32(futex_op as _)?;
    debug!(
        "futex_op = {:?}, futex_flags = {:?}, futex_addr = 0x{:x}, futex_val = 0x{:x}",
        futex_op, futex_flags, futex_addr, futex_val
    );

    let is_real_time = futex_flags.contains(FutexFlags::FUTEX_CLOCK_REALTIME);
    let get_futex_deadline = |timeout_addr: Vaddr| -> Result<Option<Duration>> {
        if timeout_addr == 0 {
            return Ok(None);
        }

        let timeout = {
            let time_spec: timespec_t = current_userspace!().read_val(timeout_addr)?;
            Duration::try_from(time_spec)?
        };

        // Freeze relative waits before entering the futex queue. Absolute
        // MONOTONIC timestamps belong to the caller's time namespace, whereas
        // timer managers and saved restart deadlines always use host clocks.
        let deadline = if futex_op == FutexOp::FUTEX_WAIT {
            MonotonicClock::timer_manager()
                .clock()
                .read_time()
                .saturating_add(timeout)
        } else if is_real_time {
            timeout
        } else {
            ctx.thread_local
                .borrow_ns_proxy()
                .unwrap()
                .time_ns()
                .remove_offset(timeout, false)
        };
        Ok(Some(deadline))
    };

    let visibility = FutexVisibility::from(futex_flags);
    let res = match futex_op {
        FutexOp::FUTEX_WAIT | FutexOp::FUTEX_WAIT_BITSET => {
            let deadline = get_futex_deadline(utime_addr)?;
            if is_real_time && futex_op == FutexOp::FUTEX_WAIT {
                return_errno_with_message!(Errno::ENOSYS, "FUTEX_WAIT cannot use CLOCK_REALTIME");
            }
            let bitset = if futex_op == FutexOp::FUTEX_WAIT {
                u32::MAX // FUTEX_BITSET_MATCH_ANY
            } else {
                bitset
            };
            if let Some(deadline) = deadline {
                return FutexRestart {
                    futex_addr,
                    futex_val,
                    bitset,
                    visibility,
                    is_real_time,
                    deadline,
                }
                .restart(ctx);
            }
            futex_wait_bitset(
                futex_addr as _,
                futex_val as _,
                None,
                bitset as _,
                ctx,
                visibility,
            )
            .map(|_| 0)
        }
        FutexOp::FUTEX_WAKE => {
            let max_count = futex_val_to_max_count(futex_val);
            futex_wake(futex_addr as _, max_count, visibility)
        }
        FutexOp::FUTEX_WAKE_BITSET => {
            let max_count = futex_val_to_max_count(futex_val);
            futex_wake_bitset(futex_addr as _, max_count, bitset as _, visibility)
        }
        FutexOp::FUTEX_REQUEUE => {
            let max_nwakes = futex_val_to_max_count(futex_val);
            // The `utime_addr` is used as the maximum number of requeues in this case.
            // When `utime_addr` is 0 or negative, it means no requeues.
            let max_nrequeues = (utime_addr as i32).max(0) as usize;
            futex_requeue(
                futex_addr as _,
                max_nwakes,
                max_nrequeues,
                futex_new_addr as _,
                ctx,
                visibility,
            )
        }
        FutexOp::FUTEX_WAKE_OP => {
            let futex_val_2 = utime_addr as u32;

            futex_wake_op(
                futex_addr,
                futex_new_addr,
                futex_val_to_max_count(futex_val),
                futex_val_to_max_count(futex_val_2),
                bitset,
                ctx,
                visibility,
            )
        }
        _ => {
            warn!("futex op = {:?}", futex_op);
            return_errno_with_message!(Errno::ENOSYS, "unsupported futex op");
        }
    }
    .map_err(|err| match err.error() {
        Errno::ETIME => Error::new(Errno::ETIMEDOUT),
        Errno::EINTR => Error::new(Errno::ERESTARTSYS),
        _ => err,
    })?;

    debug!("futex returns, tid= {} ", ctx.posix_thread.tid());
    Ok(SyscallReturn::Return(res as _))
}

/// A timed futex wait's immutable arguments and host-clock deadline.
#[derive(Clone, Copy)]
pub(crate) struct FutexRestart {
    futex_addr: Vaddr,
    futex_val: u32,
    bitset: u32,
    visibility: FutexVisibility,
    is_real_time: bool,
    deadline: Duration,
}

impl FutexRestart {
    pub(super) fn restart(self, ctx: &Context) -> Result<SyscallReturn> {
        let timer_manager = if self.is_real_time {
            RealTimeClock::timer_manager()
        } else {
            MonotonicClock::timer_manager()
        };
        let timeout = ManagedTimeout::new_with_manager(Timeout::When(self.deadline), timer_manager);
        // The lower layer resolves wake-versus-cancellation under the bucket
        // lock. Save restart work only after it has dequeued and unlocked; an
        // actual futex wake must retain its successful result even with a signal.
        futex_wait_bitset(
            self.futex_addr,
            self.futex_val as _,
            Some(timeout),
            self.bitset,
            ctx,
            self.visibility,
        )
        .map_err(|err| match err.error() {
            Errno::ETIME => Error::new(Errno::ETIMEDOUT),
            Errno::EINTR => {
                // Linux v6.12 kernel/futex/waitwake.c: futex_wait. A caught
                // handler returns EINTR even with SA_RESTART. STOP/CONT without
                // a handler reuses these arguments, never a fresh duration or
                // a second conversion from the caller's time namespace.
                ctx.thread_local
                    .restart_block()
                    .set(RestartBlock::Futex(self));
                Error::new(Errno::ERESTART_RESTARTBLOCK)
            }
            _ => err,
        })?;
        Ok(SyscallReturn::Return(0))
    }
}

fn futex_val_to_max_count(futex_val: u32) -> usize {
    // From gVisor/test/syscalls/linux/futex.cc:260: "The Linux kernel wakes one
    // waiter even if val is 0 or negative." To be consistent with Linux, we set
    // the max_count to 1 if it is 0 or negative.
    (futex_val as i32).max(1) as usize
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::{futex_op_and_flags_from_u32, futex_val_to_max_count};
    use crate::process::posix_thread::futex::{FutexFlags, FutexOp};

    #[ktest]
    fn futex_operation_parser_separates_operation_and_flags() {
        let (op, flags) = futex_op_and_flags_from_u32(
            FutexOp::FUTEX_WAIT_BITSET as u32
                | FutexFlags::FUTEX_PRIVATE.bits()
                | FutexFlags::FUTEX_CLOCK_REALTIME.bits(),
        )
        .unwrap();
        assert_eq!(op, FutexOp::FUTEX_WAIT_BITSET);
        assert!(flags.contains(FutexFlags::FUTEX_PRIVATE));
        assert!(flags.contains(FutexFlags::FUTEX_CLOCK_REALTIME));

        assert!(futex_op_and_flags_from_u32(FutexOp::FUTEX_WAIT as u32 | 0x400).is_err());
        assert!(futex_op_and_flags_from_u32(11).is_err());
    }

    #[ktest]
    fn futex_wake_count_matches_linux_zero_and_negative_rule() {
        assert_eq!(futex_val_to_max_count(0), 1);
        assert_eq!(futex_val_to_max_count(u32::MAX), 1);
        assert_eq!(futex_val_to_max_count(2), 2);
        assert_eq!(futex_val_to_max_count(i32::MAX as u32), i32::MAX as usize);
    }
}
