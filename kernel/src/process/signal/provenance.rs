// SPDX-License-Identifier: MPL-2.0

//! Temporary diagnostics for locating the source of `SIGKILL`.
//!
//! Put numeric identities before descriptive fields: bounded UART diagnostic
//! output may lose a record's suffix. Avoid thread-name locks and allocations
//! in exit paths. Routine sibling fanout is debug-only; delivery stays in the
//! info-level ring without flooding a warning-level console.

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

    let target_main_thread = target.main_thread();
    let target_thread = target_main_thread.as_posix_thread().unwrap();
    warn!(
        "A_SIGKILL_PROVENANCE stage=enqueue sender={}/{} target={}/{} origin=user callpoint={} route={}",
        ctx.process.pid(),
        ctx.posix_thread.tid(),
        target.pid(),
        target_thread.tid(),
        callpoint,
        route,
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

    warn!(
        "A_SIGKILL_PROVENANCE stage=enqueue sender={}/{} target={}/{} origin=user callpoint={} route={}",
        ctx.process.pid(),
        ctx.posix_thread.tid(),
        target.process().pid(),
        target.tid(),
        callpoint,
        route,
    );
}

pub(crate) fn trace_kernel_process_enqueue(
    reason: &'static str,
    sender: &PosixThread,
    target: &Process,
) {
    let target_main_thread = target.main_thread();
    let target_thread = target_main_thread.as_posix_thread().unwrap();
    warn!(
        "A_SIGKILL_PROVENANCE stage=enqueue sender={}/{} target={}/{} origin=kernel reason={} route=process",
        sender.process().pid(),
        sender.tid(),
        target.pid(),
        target_thread.tid(),
        reason,
    );
}

pub(crate) fn trace_kernel_thread_enqueue(
    reason: &'static str,
    sender: &PosixThread,
    target: &PosixThread,
) {
    let sender_pid = sender.process().pid();
    let sender_tid = sender.tid();
    let target_pid = target.process().pid();
    let target_tid = target.tid();
    let message = format_args!(
        "A_SIGKILL_PROVENANCE stage=enqueue sender={sender_pid}/{sender_tid} target={target_pid}/{target_tid} origin=kernel reason={reason} route=thread"
    );
    if matches!(reason, "exit-group-sibling" | "execve-sibling") {
        debug!("{message}");
    } else {
        warn!("{message}");
    }
}

pub(crate) fn trace_delivery(sig_num: SigNum, ctx: &Context) {
    if !should_trace(sig_num) {
        return;
    }

    info!(
        "A_SIGKILL_PROVENANCE stage=delivery target={}/{} origin=pending-signal",
        ctx.process.pid(),
        ctx.posix_thread.tid(),
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
