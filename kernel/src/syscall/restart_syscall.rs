// SPDX-License-Identifier: MPL-2.0

//! Per-thread restart arguments for interrupted calls with relative deadlines.

use super::{SyscallReturn, futex::FutexRestart, nanosleep::NanosleepRestart, poll::PollRestart};
use crate::prelude::*;

/// Saved restart work. Only the current thread accesses this state.
#[derive(Clone, Copy, Default)]
pub(crate) enum RestartBlock {
    #[default]
    None,
    Nanosleep(NanosleepRestart),
    Futex(FutexRestart),
    Poll(PollRestart),
}

pub(super) fn sys_restart_syscall(ctx: &Context) -> Result<SyscallReturn> {
    match ctx.thread_local.restart_block().take() {
        RestartBlock::None => return_errno_with_message!(Errno::EINTR, "no syscall to restart"),
        RestartBlock::Nanosleep(request) => request.restart(ctx),
        RestartBlock::Futex(request) => request.restart(ctx),
        RestartBlock::Poll(request) => request.restart(ctx),
    }
}
