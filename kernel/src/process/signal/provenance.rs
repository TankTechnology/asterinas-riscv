// SPDX-License-Identifier: MPL-2.0

//! Temporary low-volume diagnostics for locating the source of `SIGKILL`.

use super::{constants::SIGKILL, sig_num::SigNum, signals::Signal};
use crate::{
    prelude::*,
    process::{
        Process,
        posix_thread::{AsPosixThread, PosixThread},
    },
};

fn should_trace(sig_num: SigNum) -> bool {
    sig_num == SIGKILL
}

fn comm(thread: &PosixThread) -> String {
    thread
        .thread_name()
        .lock()
        .name()
        .to_string_lossy()
        .into_owned()
}

pub(crate) fn trace_user_process_enqueue(
    signal: &dyn Signal,
    callpoint: &'static str,
    route: &'static str,
    ctx: &Context,
    target: &Process,
) {
    if !should_trace(signal.num()) {
        return;
    }

    let sender_comm = comm(ctx.posix_thread);
    let target_main_thread = target.main_thread();
    let target_thread = target_main_thread.as_posix_thread().unwrap();
    let target_comm = comm(target_thread);
    warn!(
        "A_SIGKILL_PROVENANCE stage=enqueue origin=user callpoint={} route={} sender_pid={} sender_tid={} sender_comm={:?} target_pid={} target_tid={} target_comm={:?}",
        callpoint,
        route,
        ctx.process.pid(),
        ctx.posix_thread.tid(),
        sender_comm,
        target.pid(),
        target_thread.tid(),
        target_comm,
    );
}

pub(crate) fn trace_user_thread_enqueue(
    signal: &dyn Signal,
    callpoint: &'static str,
    route: &'static str,
    ctx: &Context,
    target: &PosixThread,
) {
    if !should_trace(signal.num()) {
        return;
    }

    let sender_comm = comm(ctx.posix_thread);
    let target_comm = comm(target);
    warn!(
        "A_SIGKILL_PROVENANCE stage=enqueue origin=user callpoint={} route={} sender_pid={} sender_tid={} sender_comm={:?} target_pid={} target_tid={} target_comm={:?}",
        callpoint,
        route,
        ctx.process.pid(),
        ctx.posix_thread.tid(),
        sender_comm,
        target.process().pid(),
        target.tid(),
        target_comm,
    );
}

pub(crate) fn trace_kernel_process_enqueue(
    reason: &'static str,
    sender: &PosixThread,
    target: &Process,
) {
    let sender_comm = comm(sender);
    let target_main_thread = target.main_thread();
    let target_thread = target_main_thread.as_posix_thread().unwrap();
    let target_comm = comm(target_thread);
    warn!(
        "A_SIGKILL_PROVENANCE stage=enqueue origin=kernel reason={} route=process sender_pid={} sender_tid={} sender_comm={:?} target_pid={} target_tid={} target_comm={:?}",
        reason,
        sender.process().pid(),
        sender.tid(),
        sender_comm,
        target.pid(),
        target_thread.tid(),
        target_comm,
    );
}

pub(crate) fn trace_kernel_thread_enqueue(
    reason: &'static str,
    sender: &PosixThread,
    target: &PosixThread,
) {
    let sender_comm = comm(sender);
    let target_comm = comm(target);
    warn!(
        "A_SIGKILL_PROVENANCE stage=enqueue origin=kernel reason={} route=thread sender_pid={} sender_tid={} sender_comm={:?} target_pid={} target_tid={} target_comm={:?}",
        reason,
        sender.process().pid(),
        sender.tid(),
        sender_comm,
        target.process().pid(),
        target.tid(),
        target_comm,
    );
}

pub(crate) fn trace_delivery(sig_num: SigNum, ctx: &Context) {
    if !should_trace(sig_num) {
        return;
    }

    let target_comm = comm(ctx.posix_thread);
    warn!(
        "A_SIGKILL_PROVENANCE stage=delivery origin=pending-signal target_pid={} target_tid={} target_comm={:?}",
        ctx.process.pid(),
        ctx.posix_thread.tid(),
        target_comm,
    );
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;
    use crate::process::signal::constants::SIGTERM;

    #[ktest]
    fn only_sigkill_takes_provenance_path() {
        assert!(should_trace(SIGKILL));
        assert!(!should_trace(SIGTERM));
    }
}
