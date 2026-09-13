// SPDX-License-Identifier: MPL-2.0

//! Opt-in per-thread syscall snapshots and bounded process lifecycle observations.
//!
//! Records contain scalar registers only. The VM identity captured at entry
//! prevents snapshots from exposing an earlier executable's register values.

use core::sync::atomic::{AtomicBool, Ordering};

use ostd::timer::Jiffies;
use spin::Once;

use self::state::{ExitObservation, LifecycleBudget, LifecycleEvent, State};
pub(crate) use self::state::{Outcome, Snapshot, snapshot_slice};
use super::arch;
use crate::{
    prelude::*,
    process::{TermStatus, VmarSnapshot},
};

mod state;

static ENABLED: AtomicBool = AtomicBool::new(false);
static LIFECYCLE_BUDGET: LifecycleBudget = LifecycleBudget::new();

aster_cmdline::define_flag_param!("asterinas.syscall_diag", ENABLED);

/// Lazily allocated, constant-size storage owned by one POSIX thread.
#[derive(Default)]
pub(crate) struct ThreadDiagnostics(Once<Box<SpinLock<ThreadState>>>);

struct ThreadState {
    vm: VmarSnapshot,
    calls: State,
    entered_tid: u32,
}

impl ThreadDiagnostics {
    /// Copies only records belonging to the caller's locked VM identity.
    pub(crate) fn snapshot(&self, vm: &VmarSnapshot, pid: u32, tid: u32) -> Snapshot {
        let enabled = ENABLED.load(Ordering::Relaxed);
        let calls = if enabled {
            self.0
                .get()
                .map(|state| {
                    let state = state.lock();
                    if state.vm.ptr_eq(vm) {
                        state.calls
                    } else {
                        State::default()
                    }
                })
                .unwrap_or_default()
        } else {
            State::default()
        };
        Snapshot::new(enabled, pid, tid, Jiffies::elapsed().as_u64(), calls)
    }
}

/// Records entry before seccomp or dispatch, without touching user memory.
pub(super) fn enter(ctx: &Context, number: u64, args: [u64; 6]) {
    if !ENABLED.load(Ordering::Relaxed) {
        return;
    }

    // Lock order: process VMAR -> diagnostic state. Allocate before taking
    // the diagnostic spinlock; neither formatting nor logging runs under it.
    let vmar_guard = ctx.process.lock_vmar();
    let vm = vmar_guard.snapshot();
    let diagnostics = ctx.posix_thread.syscall_diagnostics();
    let state = diagnostics.0.call_once(|| {
        Box::new(SpinLock::new(ThreadState {
            vm: vm.clone(),
            calls: State::default(),
            entered_tid: ctx.posix_thread.tid(),
        }))
    });
    let entered_jiffies = Jiffies::elapsed().as_u64();
    let old_vm = {
        let mut state = state.lock();
        let old_vm = if !state.vm.ptr_eq(&vm) {
            state.calls.clear_calls();
            Some(core::mem::replace(&mut state.vm, vm))
        } else {
            None
        };
        state.entered_tid = ctx.posix_thread.tid();
        state.calls.enter(number, args, entered_jiffies);
        old_vm
    };
    // Dropping the final weak reference can free memory.
    drop(old_vm);
}

/// Completes a call, including seccomp rejection and changed user contexts.
pub(super) fn complete(ctx: &Context, outcome: Outcome) {
    if !ENABLED.load(Ordering::Relaxed) {
        return;
    }
    let Some(state) = ctx.posix_thread.syscall_diagnostics().0.get() else {
        return;
    };
    let finished_jiffies = Jiffies::elapsed().as_u64();
    let (completion, entered_tid) = {
        let mut state = state.lock();
        (
            state.calls.complete(outcome, finished_jiffies),
            state.entered_tid,
        )
    };
    let Some(completion) = completion else {
        return;
    };
    let Some(kind) = lifecycle_kind(completion.call.number) else {
        return;
    };
    if !reserve_lifecycle_log() {
        return;
    }
    ostd::info!(
        "syscall_diag lifecycle={} pid={} tid={} entered_tid={} ppid={} sequence={} number={} result={} outcome={} finished_jiffies={}",
        kind,
        ctx.process.pid(),
        ctx.posix_thread.tid(),
        entered_tid,
        ctx.process.parent().pid(),
        completion.call.sequence,
        completion.call.number,
        outcome.result_display(),
        outcome.label(),
        finished_jiffies,
    );
}

/// Finishes exit syscalls and observes the completed exit transition once.
pub(crate) fn on_exit(ctx: &Context, term_status: TermStatus) {
    if !ENABLED.load(Ordering::Relaxed) {
        return;
    }
    complete(ctx, Outcome::NoReturn);
    if !reserve_lifecycle_log() {
        return;
    }
    let observation = ExitObservation {
        pid: ctx.process.pid(),
        tid: ctx.posix_thread.tid(),
        ppid: ctx.process.parent().pid(),
        termination_status: term_status.as_u32(),
        signal: match term_status {
            TermStatus::Exited(_) => None,
            TermStatus::Killed(signal) => Some(signal.as_u8()),
        },
        finished_jiffies: Jiffies::elapsed().as_u64(),
    };
    ostd::info!("{}", observation);
}

fn lifecycle_kind(number: u64) -> Option<&'static str> {
    match number {
        arch::SYS_CLONE | arch::SYS_CLONE3 => Some("clone"),
        #[cfg(target_arch = "x86_64")]
        arch::SYS_FORK | arch::SYS_VFORK => Some("clone"),
        arch::SYS_EXECVE | arch::SYS_EXECVEAT => Some("exec"),
        arch::SYS_WAIT4 | arch::SYS_WAITID => Some("wait"),
        _ => None,
    }
}

fn reserve_lifecycle_log() -> bool {
    match LIFECYCLE_BUDGET.next() {
        LifecycleEvent::Record => true,
        LifecycleEvent::Suppressed(count) => {
            ostd::info!(
                "syscall_diag lifecycle=cap_exhausted record_limit=1024 suppressed={}",
                count
            );
            false
        }
        LifecycleEvent::Quiet => false,
    }
}
