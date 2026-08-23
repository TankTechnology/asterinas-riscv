// SPDX-License-Identifier: MPL-2.0

use ostd::mm::VmIo;

use super::{SyscallReturn, getrusage::rusage_t};
use crate::{
    prelude::*,
    process::{ProcessFilter, WaitOptions, WaitStatus, do_wait, posix_thread::AsPosixThread},
};

pub fn sys_wait4(
    wait_pid: u64,
    status_ptr: u64,
    wait_options: u32,
    rusage_addr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let wait_options = WaitOptions::from_bits(wait_options)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown wait option"))?;

    // A positive PID is interpreted in the caller's PID namespace; children
    // outside it are invisible to `wait4`.
    let caller_pid_ns = ctx.process.pid_ns();
    let wait_pid = if wait_pid as i64 > 0 && !caller_pid_ns.is_init() {
        let process = caller_pid_ns
            .process_of_vpid(wait_pid as u32)
            .ok_or_else(|| Error::with_message(Errno::ECHILD, "no such child process"))?;
        process.pid() as u64
    } else {
        wait_pid
    };

    let process_filter = ProcessFilter::from_id(wait_pid as _)?;

    if wait_options.intersects(WaitOptions::WSTOPPED | WaitOptions::WCONTINUED)
        && wait_options.contains(WaitOptions::WNOWAIT)
    {
        return_errno_with_message!(
            Errno::EINVAL,
            "WNOWAIT cannot be used toghther with WSTOPPED or WCONTINUED"
        );
    }

    let wait_status =
        do_wait(process_filter, wait_options, ctx).map_err(|err| match err.error() {
            Errno::EINTR => Error::new(Errno::ERESTARTSYS),
            _ => err,
        })?;
    let Some(wait_status) = wait_status else {
        return Ok(SyscallReturn::Return(0 as _));
    };

    let (return_pid, status_code) = (wait_status.pid(), calculate_status_code(&wait_status));
    // Report the child's virtual PID in the caller's PID namespace.
    let return_pid = if !caller_pid_ns.is_init() {
        wait_status.pid_in_ns(caller_pid_ns).unwrap_or(return_pid)
    } else {
        return_pid
    };
    if status_ptr != 0 {
        ctx.user_space().write_val(status_ptr as _, &status_code)?;
    }

    if rusage_addr != 0 {
        let rusage = rusage_t {
            ru_utime: wait_status.prof_clock().user_clock().read_time().into(),
            ru_stime: wait_status.prof_clock().kernel_clock().read_time().into(),
            ..Default::default()
        };

        ctx.user_space().write_val(rusage_addr, &rusage)?;
    }

    Ok(SyscallReturn::Return(return_pid as _))
}

fn calculate_status_code(wait_status: &WaitStatus) -> u32 {
    match wait_status {
        WaitStatus::Zombie(process) => process.status().exit_code(),
        WaitStatus::Stop(_, sig_num) => ((sig_num.as_u8() as u32) << 8) | 0x7f,
        WaitStatus::Continue(_) => 0xffff,
        WaitStatus::TraceeExit(thread) => thread.as_posix_thread().unwrap().exit_code(),
        WaitStatus::TraceeStop(_, status) => status.to_wait4_status(),
    }
}
