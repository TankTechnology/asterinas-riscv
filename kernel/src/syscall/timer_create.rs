// SPDX-License-Identifier: MPL-2.0

use ostd::mm::VmIo;

use super::{
    SyscallReturn,
    clock_gettime::{DynamicClockIdInfo, DynamicCpuClockType},
};
use crate::{
    prelude::*,
    process::{
        Process, pid_table,
        posix_thread::AsPosixThread,
        signal::{
            c_types::{SigNotify, sigevent_t, sigval_t},
            constants::SIGALRM,
            sig_num::SigNum,
            signals::timer::{TimerSignal, TimerSignalState},
        },
    },
    syscall::ClockId,
    thread::{
        Thread,
        work_queue::{submit_work_item, work_item::WorkItem},
    },
    time::{
        Timer, clockid_t,
        clocks::{BootTimeClock, MonotonicClock, RealTimeClock},
        timer::TimerGuard,
    },
};

#[derive(Clone)]
enum TimerNotification {
    None,
    Process {
        process: Weak<Process>,
        num: SigNum,
        value: Option<sigval_t>,
    },
    Thread {
        thread: Weak<Thread>,
        num: SigNum,
        value: sigval_t,
    },
}

fn create_timer_callback(
    notification: TimerNotification,
    timer_id: usize,
    signal_state: Arc<TimerSignalState>,
) -> impl Fn(TimerGuard) + Send + Sync + 'static {
    let timer_id = i32::try_from(timer_id).unwrap();
    let delivery = match notification {
        TimerNotification::None => None,
        TimerNotification::Process {
            process,
            num,
            value,
        } => {
            let value = value.unwrap_or_else(|| sigval_t::from_int(timer_id));
            let state = signal_state.clone();
            Some(WorkItem::new(Box::new(move || {
                if let Some(process) = process.upgrade() {
                    process.enqueue_signal(Box::new(TimerSignal::new(
                        num,
                        timer_id,
                        value,
                        state.clone(),
                    )));
                }
            })))
        }
        TimerNotification::Thread { thread, num, value } => {
            let state = signal_state.clone();
            Some(WorkItem::new(Box::new(move || {
                let Some(thread) = thread.upgrade() else {
                    return;
                };
                if let Some(posix_thread) = thread.as_posix_thread() {
                    posix_thread.enqueue_signal(Box::new(TimerSignal::new(
                        num,
                        timer_id,
                        value,
                        state.clone(),
                    )));
                }
            })))
        }
    };

    move |guard: TimerGuard| {
        let Some(work_item) = &delivery else {
            return;
        };
        if signal_state.record_expirations(guard.overrun()) {
            submit_work_item(
                work_item.clone(),
                crate::thread::work_queue::WorkPriority::High,
            );
        }
    }
}

pub fn sys_timer_create(
    clockid: clockid_t,
    sigevent_addr: Vaddr,
    timer_id_addr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    if timer_id_addr == 0 {
        return_errno_with_message!(
            Errno::EINVAL,
            "the address of timer_id_addr should be valid"
        );
    }

    let current_process = current!();
    let notification = {
        // If `sigevent_addr` is NULL, use the default method (like `sys_alarm`) to send signal.
        if sigevent_addr == 0 {
            TimerNotification::Process {
                process: Arc::downgrade(&current_process),
                num: SIGALRM,
                value: None,
            }
        // Determine the timeout action through `sigevent`.
        } else {
            let sig_event = ctx.user_space().read_val::<sigevent_t>(sigevent_addr)?;
            let sigev_notify = SigNotify::try_from(sig_event.sigev_notify)?;
            let signo = sig_event.sigev_signo;
            match sigev_notify {
                // Do nothing when the timer is expired.
                SigNotify::SIGEV_NONE => TimerNotification::None,
                // Send a signal to the current process when the timer is expired.
                SigNotify::SIGEV_SIGNAL => TimerNotification::Process {
                    process: Arc::downgrade(&current_process),
                    num: SigNum::try_from(signo as u8)?,
                    value: Some(sig_event.sigev_value),
                },
                // SIGEV_THREAD is implemented by the C library using an
                // internal signal and helper thread.  The kernel side only
                // validates and delivers the requested signal, just like the
                // SIGEV_SIGNAL path.
                SigNotify::SIGEV_THREAD => TimerNotification::Process {
                    process: Arc::downgrade(&current_process),
                    num: SigNum::try_from(signo as u8)?,
                    value: Some(sig_event.sigev_value),
                },
                // Send a signal to the specified thread when the timer is expired.
                SigNotify::SIGEV_THREAD_ID => {
                    let tid = sig_event.sigev_un.read_tid() as u32;
                    let thread = pid_table::pid_table_mut().get_thread(tid).ok_or_else(|| {
                        Error::with_message(Errno::EINVAL, "the target thread does not exist")
                    })?;
                    let posix_thread = thread.as_posix_thread().unwrap();
                    if posix_thread.process().pid() != current_process.pid() {
                        return_errno_with_message!(
                            Errno::EINVAL,
                            "the target thread does not belong to the current process"
                        );
                    }
                    TimerNotification::Thread {
                        thread: Arc::downgrade(&thread),
                        num: SigNum::try_from(signo as u8)?,
                        value: sig_event.sigev_value,
                    }
                }
            }
        }
    };

    let Some(timer_id) =
        current_process
            .timer_manager()
            .create_posix_timer(move |timer_id, signal_state| {
                let callback = create_timer_callback(notification, timer_id, signal_state);
                create_timer(clockid, callback, ctx)
            })?
    else {
        return_errno_with_message!(Errno::EAGAIN, "timer IDs are exhausted");
    };
    ctx.user_space().write_val(timer_id_addr, &timer_id)?;
    Ok(SyscallReturn::Return(0))
}

pub fn sys_timer_delete(timer_id: usize, _ctx: &Context) -> Result<SyscallReturn> {
    let current_process = current!();
    let Some(timer) = current_process.timer_manager().remove_posix_timer(timer_id) else {
        return_errno_with_message!(Errno::EINVAL, "invalid timer ID");
    };

    timer.lock().cancel();
    Ok(SyscallReturn::Return(0))
}

/// Creates a timer associated with the specified clock ID.
///
/// This timer will invoke the given callback function (`func`) when it expires.
pub fn create_timer<F>(clockid: clockid_t, func: F, ctx: &Context) -> Result<Arc<Timer>>
where
    F: Fn(TimerGuard) + Send + Sync + 'static,
{
    let process_timer_manager = ctx.process.timer_manager();
    let timer = if clockid >= 0 {
        let clock_id = ClockId::try_from(clockid)?;
        match clock_id {
            ClockId::CLOCK_PROCESS_CPUTIME_ID => process_timer_manager.create_prof_timer(func),
            ClockId::CLOCK_THREAD_CPUTIME_ID => ctx.posix_thread.create_prof_timer(func),
            ClockId::CLOCK_REALTIME => RealTimeClock::timer_manager().create_timer(func),
            ClockId::CLOCK_MONOTONIC => MonotonicClock::timer_manager().create_timer(func),
            ClockId::CLOCK_BOOTTIME => BootTimeClock::timer_manager().create_timer(func),
            _ => return_errno_with_message!(Errno::EINVAL, "invalid clock ID"),
        }
    } else {
        let dynamic_clockid_info = DynamicClockIdInfo::try_from(clockid)?;
        match dynamic_clockid_info {
            DynamicClockIdInfo::Pid(pid, clock_type) => {
                let process = pid_table::pid_table_mut()
                    .get_process(pid)
                    .ok_or_else(|| Error::with_message(Errno::EINVAL, "invalid clock ID"))?;
                let process_timer_manager = process.timer_manager();
                match clock_type {
                    DynamicCpuClockType::Profiling | DynamicCpuClockType::Scheduling => {
                        process_timer_manager.create_prof_timer(func)
                    }
                    DynamicCpuClockType::Virtual => {
                        process_timer_manager.create_virtual_timer(func)
                    }
                }
            }
            DynamicClockIdInfo::Tid(tid, clock_type) => {
                let thread = pid_table::pid_table_mut()
                    .get_thread(tid)
                    .ok_or_else(|| Error::with_message(Errno::EINVAL, "invalid clock ID"))?;
                let posix_thread = thread.as_posix_thread().unwrap();
                match clock_type {
                    DynamicCpuClockType::Profiling | DynamicCpuClockType::Scheduling => {
                        posix_thread.create_prof_timer(func)
                    }
                    DynamicCpuClockType::Virtual => posix_thread.create_virtual_timer(func),
                }
            }
            DynamicClockIdInfo::Fd(_) => return_errno_with_message!(
                Errno::EOPNOTSUPP,
                "the file descriptor does not provide a dynamic clock"
            ),
        }
    };
    Ok(timer)
}
