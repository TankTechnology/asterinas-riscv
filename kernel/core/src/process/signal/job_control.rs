// SPDX-License-Identifier: MPL-2.0

//! Serialization of signal generation, action selection, and stop commitment.
//!
//! Lock order: task membership (when needed) or dispositions, then the process
//! coordinator, then a queue or selected-stop lock. Never acquire task membership,
//! dispositions, or ptrace state while holding the coordinator. Notifications
//! and wakeups happen after releasing it.

use super::{constants::*, sig_num::SigNum};
use crate::process::status::ProcessStatus;

/// Per-process coordinator, protected by the process's signal mutex.
#[derive(Default)]
pub(in crate::process) struct SignalJobControl {
    exiting: bool,
}

/// A delivery selection, not a pending signal and not a completed group stop.
#[derive(Default)]
pub(in crate::process) struct SelectedStop {
    valid: bool,
}

impl SignalJobControl {
    pub fn select(&mut self, selected: &mut SelectedStop, may_stop: bool) {
        selected.valid = may_stop && !self.exiting;
    }

    pub fn cancel(&mut self, selected: &mut SelectedStop) {
        selected.valid = false;
    }

    pub fn commit_exit(&mut self) {
        self.exiting = true;
    }

    /// Checks eligibility and commits the actual stop state in one critical section.
    pub fn commit_stop(
        &mut self,
        selected: &mut SelectedStop,
        status: &ProcessStatus,
        signum: SigNum,
        kill_pending: bool,
    ) -> bool {
        let valid = core::mem::take(&mut selected.valid);
        valid && !self.exiting && !kill_pending && status.stop_status().stop(signum)
    }
}

pub(in crate::process) fn is_stop_signal(signum: SigNum) -> bool {
    matches!(signum, SIGSTOP | SIGTSTP | SIGTTIN | SIGTTOU)
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::*;

    use super::*;

    #[ktest]
    fn continue_cancels_selected_stop() {
        let mut control = SignalJobControl::default();
        let mut selected = SelectedStop::default();
        let status = ProcessStatus::default();
        control.select(&mut selected, true);
        control.cancel(&mut selected);
        let stale_commit = control.commit_stop(&mut selected, &status, SIGSTOP, false);
        ostd::early_println!("SELECTED_STOP stale_commit={}", stale_commit);
        assert!(!stale_commit);
        assert!(!status.stop_status().is_stopped());

        // A new STOP after CONT is eligible, but each selection commits at most once.
        control.select(&mut selected, true);
        assert!(control.commit_stop(&mut selected, &status, SIGSTOP, false));
        status.stop_status().resume();
        assert!(!control.commit_stop(&mut selected, &status, SIGSTOP, false));
    }

    #[ktest]
    fn exit_and_pending_kill_prevent_stop() {
        let mut control = SignalJobControl::default();
        let mut selected = SelectedStop::default();
        let status = ProcessStatus::default();
        control.select(&mut selected, true);
        assert!(!control.commit_stop(&mut selected, &status, SIGSTOP, true));
        control.select(&mut selected, true);
        control.commit_exit();
        assert!(!control.commit_stop(&mut selected, &status, SIGSTOP, false));
        control.select(&mut selected, true);
        assert!(!control.commit_stop(&mut selected, &status, SIGSTOP, false));
        assert!(!status.stop_status().is_stopped());
    }
}
