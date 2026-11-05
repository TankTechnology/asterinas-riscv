// SPDX-License-Identifier: MPL-2.0

//! `rt_sigqueueinfo(2)` and `rt_tgsigqueueinfo(2)`.
//!
//! Both queue a signal carrying a caller-supplied `siginfo_t`. Linux lets the
//! caller choose the `si_code` as long as it is not a kernel or `kill()` /
//! `tgkill()` code, so a process cannot impersonate either of those.

use ostd::mm::VmIo;

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::{
        Pid, kill,
        signal::{
            c_types::siginfo_t,
            constants::SI_TKILL,
            sig_num::SigNum,
            signals::{Signal, raw::RawSignal},
        },
        tgkill,
    },
    thread::Tid,
};

/// Queues a signal with a caller-supplied `siginfo` to the process `pid`.
pub fn sys_rt_sigqueueinfo(
    pid: Pid,
    sig_num: u8,
    info_ptr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let signal = queued_signal(sig_num, info_ptr, ctx)?;
    kill(pid, signal, ctx)?;
    Ok(SyscallReturn::Return(0))
}

/// Queues a signal with a caller-supplied `siginfo` to the thread `tid`.
pub fn sys_rt_tgsigqueueinfo(
    tgid: Pid,
    tid: Tid,
    sig_num: u8,
    info_ptr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let signal = queued_signal(sig_num, info_ptr, ctx)?;
    tgkill(tid, Some(tgid), signal, ctx)?;
    Ok(SyscallReturn::Return(0))
}

fn queued_signal(sig_num: u8, info_ptr: Vaddr, ctx: &Context) -> Result<Option<Box<dyn Signal>>> {
    if sig_num == 0 {
        return Ok(None);
    }
    let sig_num = SigNum::try_from(sig_num)?;

    // Unlike `pidfd_send_signal(2)`, `rt_sigqueueinfo(2)` requires the
    // `siginfo` (a null pointer is EFAULT), and Linux takes `si_signo` from
    // the syscall argument rather than trusting the caller's copy.
    let mut siginfo = ctx.user_space().read_val::<siginfo_t>(info_ptr)?;
    siginfo.si_signo = sig_num.as_u8() as i32;

    // Reference: <https://elixir.bootlin.com/linux/v6.18/source/kernel/signal.c#L3940>.
    // Not even root may pretend to send a kernel signal, a `kill()` or a
    // `tgkill()` one; those codes are the kernel's to assign.
    if siginfo.si_code >= 0 || siginfo.si_code == SI_TKILL {
        return_errno_with_message!(
            Errno::EPERM,
            "the caller may not send a signal with a kernel or kill() si_code"
        );
    }

    Ok(Some(Box::new(RawSignal::new(siginfo))))
}
