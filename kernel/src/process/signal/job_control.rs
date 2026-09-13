// SPDX-License-Identifier: MPL-2.0

//! Serialization of signal generation, action selection, and stop commitment.
//!
//! Lock order: task membership (when needed) or dispositions, then the process
//! coordinator, then a queue, participation, or selected-stop lock. Ptrace paths
//! acquire tracee state before the coordinator. Never acquire task membership,
//! dispositions, or ptrace state while holding the coordinator. Notifications
//! and wakeups happen after releasing it.

use super::{constants::*, sig_num::SigNum};
use crate::process::status::{ProcessStatus, StopWaitStatus};

/// Per-process coordinator, protected by the process's signal mutex.
#[derive(Default)]
pub(in crate::process) struct SignalJobControl {
    phase: GroupStopPhase,
    notification: Option<StopWaitStatus>,
}

#[derive(Default)]
enum GroupStopPhase {
    #[default]
    Running,
    Stopping {
        signum: SigNum,
        remaining: usize,
        // A tracer-resumed member may start another participation round
        // without SIGCONT. Preserve the completed group's original signal
        // and do not publish a second group-parent completion notification.
        completed_signum: Option<SigNum>,
    },
    Stopped {
        signum: SigNum,
    },
    Exiting,
}

/// A thread's obligation in the current stop, accessed under the coordinator.
/// No acknowledgement token may be saved across an unlock or a wait.
#[derive(Default)]
pub(in crate::process) enum GroupStopParticipant {
    #[default]
    Inactive,
    Pending {
        counted: bool,
        signum: SigNum,
    },
    Acknowledged,
    /// Participation has transferred to a ptrace group-stop. Only the tracer
    /// controls that stop; its return must not overwrite a later Pending marker.
    PtraceControlled,
}

impl GroupStopParticipant {
    pub fn must_stop(&self) -> bool {
        matches!(self, Self::Pending { .. } | Self::Acknowledged)
    }
}

/// A delivery selection, not a pending signal and not a completed group stop.
#[derive(Default)]
pub(in crate::process) struct SelectedStop {
    valid: bool,
}

impl SignalJobControl {
    pub fn select(&mut self, selected: &mut SelectedStop, may_stop: bool) {
        selected.valid = may_stop && !matches!(self.phase, GroupStopPhase::Exiting);
    }

    pub fn cancel(&mut self, selected: &mut SelectedStop) {
        selected.valid = false;
    }

    pub fn commit_exit(&mut self, status: &ProcessStatus) {
        self.phase = GroupStopPhase::Exiting;
        status.stop_status().cancel();
        self.notification = None;
        status.stop_status().set_notification_pending(false);
    }

    /// Checks eligibility and commits the actual stop state in one critical section.
    pub fn begin_stop(
        &mut self,
        selected: &mut SelectedStop,
        status: &ProcessStatus,
        signum: SigNum,
        kill_pending: bool,
    ) -> bool {
        let valid = core::mem::take(&mut selected.valid);
        if !valid || kill_pending {
            return false;
        }
        let completed_signum = match self.phase {
            GroupStopPhase::Running => None,
            GroupStopPhase::Stopped { signum } => Some(signum),
            // A traced member may be resumed before its siblings acknowledge.
            // The initiating member has selected a new STOP, so rebuild the
            // outstanding count just as for a completed tracer-started round.
            GroupStopPhase::Stopping {
                completed_signum, ..
            } => completed_signum,
            GroupStopPhase::Exiting => return false,
        };
        self.phase = GroupStopPhase::Stopping {
            signum,
            remaining: 0,
            completed_signum,
        };
        status.stop_status().request_stop();
        true
    }

    /// Enrolls a live member before publication, while task membership is locked.
    pub fn enroll(&mut self, participant: &mut GroupStopParticipant) {
        // Already parked members do not participate again in a tracer-started
        // round. SIGCONT clears this marker before an ordinary fresh stop.
        if matches!(participant, GroupStopParticipant::Acknowledged) {
            return;
        }
        match &mut self.phase {
            GroupStopPhase::Stopping {
                remaining, signum, ..
            } => {
                *remaining += 1;
                *participant = GroupStopParticipant::Pending {
                    counted: true,
                    signum: *signum,
                };
            }
            // A late join must park before first userspace entry, but does not
            // reopen the completed stop or cause another parent report.
            GroupStopPhase::Stopped { signum } => {
                *participant = GroupStopParticipant::Pending {
                    counted: false,
                    signum: *signum,
                }
            }
            GroupStopPhase::Running | GroupStopPhase::Exiting => {}
        }
    }

    /// Acknowledges the current obligation at the thread's stop checkpoint.
    /// Returns whether this checkpoint completed the group stop.
    pub fn acknowledge(
        &mut self,
        participant: &mut GroupStopParticipant,
        status: &ProcessStatus,
    ) -> bool {
        let GroupStopParticipant::Pending { counted, .. } = *participant else {
            return false;
        };
        *participant = GroupStopParticipant::Acknowledged;
        if !counted {
            return false;
        }
        let GroupStopPhase::Stopping {
            signum,
            remaining,
            completed_signum,
        } = &mut self.phase
        else {
            return false;
        };
        *remaining -= 1;
        if *remaining != 0 {
            return false;
        }
        let notify = completed_signum.is_none();
        let signum = completed_signum.unwrap_or(*signum);
        self.phase = GroupStopPhase::Stopped { signum };
        if !notify {
            return false;
        }
        status.stop_status().complete_stop(signum);
        self.notification = Some(StopWaitStatus::Stopped(signum));
        status.stop_status().set_notification_pending(true);
        true
    }

    /// Transfers the current obligation to a published ptrace stop. The caller
    /// holds tracee state and the coordinator until that stop is visible.
    pub fn acknowledge_traced(
        &mut self,
        participant: &mut GroupStopParticipant,
        status: &ProcessStatus,
    ) -> Option<(SigNum, bool)> {
        if matches!(
            self.phase,
            GroupStopPhase::Running | GroupStopPhase::Exiting
        ) {
            return None;
        }
        let GroupStopParticipant::Pending { signum, .. } = *participant else {
            return None;
        };
        let completed = self.acknowledge(participant, status);
        *participant = GroupStopParticipant::PtraceControlled;
        Some((signum, completed))
    }

    /// Returns the original signal reported for the active group stop.
    pub fn group_stop_signal(&self) -> Option<SigNum> {
        match self.phase {
            GroupStopPhase::Stopping {
                completed_signum: Some(signum),
                ..
            }
            | GroupStopPhase::Stopping {
                signum,
                completed_signum: None,
                ..
            }
            | GroupStopPhase::Stopped { signum } => Some(signum),
            GroupStopPhase::Running | GroupStopPhase::Exiting => None,
        }
    }

    /// Schedules a ptrace checkpoint when attaching to an already parked task.
    pub fn attach(&mut self, participant: &mut GroupStopParticipant) {
        if matches!(participant, GroupStopParticipant::Acknowledged) {
            self.restore_checkpoint(participant);
        }
    }

    /// Restores group parking when tracing ends, without consuming or replacing
    /// an existing counted obligation.
    pub fn detach(&mut self, participant: &mut GroupStopParticipant) {
        if matches!(participant, GroupStopParticipant::PtraceControlled) {
            self.restore_checkpoint(participant);
        }
    }

    fn restore_checkpoint(&self, participant: &mut GroupStopParticipant) {
        let signum = match self.phase {
            GroupStopPhase::Stopping { signum, .. } | GroupStopPhase::Stopped { signum } => signum,
            GroupStopPhase::Running | GroupStopPhase::Exiting => return,
        };
        *participant = GroupStopParticipant::Pending {
            counted: false,
            signum,
        };
    }

    /// Removes an exiting member exactly once, including an unacknowledged one.
    pub fn leave(
        &mut self,
        participant: &mut GroupStopParticipant,
        status: &ProcessStatus,
    ) -> bool {
        let completed = self.acknowledge(participant, status);
        *participant = GroupStopParticipant::Inactive;
        completed
    }

    /// Continues a group. The caller clears every member's participation and
    /// selected-stop permission in the same critical section.
    pub fn resume(&mut self, status: &ProcessStatus) -> bool {
        if matches!(self.phase, GroupStopPhase::Exiting) {
            return false;
        }
        // Linux reports an interrupted stop as CLD_STOPPED, while wait's
        // continued state is still set. Notifications coalesce independently
        // of wait status and are delivered by a returning member thread.
        let notification = match self.phase {
            GroupStopPhase::Stopping {
                completed_signum: Some(_),
                ..
            }
            | GroupStopPhase::Stopped { .. } => Some(StopWaitStatus::Continue),
            GroupStopPhase::Stopping { signum, .. } => Some(StopWaitStatus::Stopped(signum)),
            GroupStopPhase::Running => None,
            GroupStopPhase::Exiting => unreachable!(),
        };
        if let Some(notification) = notification {
            self.notification = Some(notification);
            status.stop_status().set_notification_pending(true);
        }
        self.phase = GroupStopPhase::Running;
        status.stop_status().resume()
    }

    /// Claims the coalesced notification before executing cross-process callbacks.
    pub fn take_notification(&mut self, status: &ProcessStatus) -> Option<StopWaitStatus> {
        status.stop_status().set_notification_pending(false);
        self.notification.take()
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
        let stale_commit = control.begin_stop(&mut selected, &status, SIGSTOP, false);
        ostd::early_println!("SELECTED_STOP stale_commit={}", stale_commit);
        assert!(!stale_commit);
        assert!(!status.stop_status().is_stopped());

        // A new STOP after CONT is eligible, but each selection commits at most once.
        control.select(&mut selected, true);
        assert!(control.begin_stop(&mut selected, &status, SIGSTOP, false));
        control.resume(&status);
        assert!(!control.begin_stop(&mut selected, &status, SIGSTOP, false));
    }

    #[ktest]
    fn exit_and_pending_kill_prevent_stop() {
        let mut control = SignalJobControl::default();
        let mut selected = SelectedStop::default();
        let status = ProcessStatus::default();
        control.select(&mut selected, true);
        assert!(!control.begin_stop(&mut selected, &status, SIGSTOP, true));
        control.select(&mut selected, true);
        control.commit_exit(&status);
        assert!(!control.begin_stop(&mut selected, &status, SIGSTOP, false));
        control.select(&mut selected, true);
        assert!(!control.begin_stop(&mut selected, &status, SIGSTOP, false));
        assert!(!status.stop_status().is_stopped());
    }

    #[ktest]
    fn repeated_stop_preserves_completed_notification() {
        use crate::process::WaitOptions;

        let mut control = SignalJobControl::default();
        let mut selected = SelectedStop::default();
        let status = ProcessStatus::default();
        let mut member = GroupStopParticipant::default();
        let mut parked = GroupStopParticipant::default();
        control.select(&mut selected, true);
        assert!(control.begin_stop(&mut selected, &status, SIGSTOP, false));
        control.enroll(&mut member);
        control.enroll(&mut parked);
        assert_eq!(
            control.acknowledge_traced(&mut member, &status),
            Some((SIGSTOP, false))
        );
        assert!(control.acknowledge(&mut parked, &status));
        assert!(matches!(
            control.take_notification(&status),
            Some(StopWaitStatus::Stopped(SIGSTOP))
        ));
        assert!(status.stop_status().wait(WaitOptions::WSTOPPED).is_some());

        // Tracer continuation does not rewrite the consumed participation.
        // The next stop must enroll this controlled member, but not its parked sibling.
        control.select(&mut selected, true);
        let accepted = control.begin_stop(&mut selected, &status, SIGTSTP, false);
        ostd::early_println!("REPEATED_GROUP_STOP accepted={}", accepted);
        assert!(accepted);
        control.enroll(&mut member);
        control.enroll(&mut parked);
        assert_eq!(
            control.acknowledge_traced(&mut member, &status),
            Some((SIGTSTP, false))
        );
        assert!(matches!(
            control.phase,
            GroupStopPhase::Stopped { signum: SIGSTOP }
        ));
        assert!(matches!(parked, GroupStopParticipant::Acknowledged));
        assert!(control.take_notification(&status).is_none());
        assert!(status.stop_status().wait(WaitOptions::WSTOPPED).is_none());

        // A third round interrupted by CONT still continues the earlier
        // completed stop, rather than reporting a fresh interrupted STOP.
        control.select(&mut selected, true);
        assert!(control.begin_stop(&mut selected, &status, SIGTTIN, false));
        control.enroll(&mut member);
        assert!(control.resume(&status));
        assert!(matches!(
            control.take_notification(&status),
            Some(StopWaitStatus::Continue)
        ));
    }

    #[ktest]
    fn traced_member_restarts_incomplete_group_stop() {
        let mut control = SignalJobControl::default();
        let status = ProcessStatus::default();
        let mut selected = SelectedStop::default();
        let mut traced = GroupStopParticipant::default();
        let mut sibling = GroupStopParticipant::default();
        control.select(&mut selected, true);
        assert!(control.begin_stop(&mut selected, &status, SIGSTOP, false));
        control.enroll(&mut traced);
        control.enroll(&mut sibling);
        assert_eq!(
            control.acknowledge_traced(&mut traced, &status),
            Some((SIGSTOP, false))
        );
        // The tracer resumes this member before its sibling acknowledges.
        control.select(&mut selected, true);
        let accepted = control.begin_stop(&mut selected, &status, SIGTSTP, false);
        ostd::early_println!("TRACED_INCOMPLETE_RESTOP accepted={}", accepted);
        assert!(accepted);
        control.enroll(&mut traced);
        control.enroll(&mut sibling);
        assert_eq!(
            control.acknowledge_traced(&mut traced, &status),
            Some((SIGTSTP, false))
        );
        assert!(control.take_notification(&status).is_none());
        // Detach restores an uncounted obligation alongside a counted sibling.
        control.detach(&mut traced);
        assert!(!control.acknowledge(&mut traced, &status));
        assert!(control.acknowledge(&mut sibling, &status));
        assert!(matches!(
            control.take_notification(&status),
            Some(StopWaitStatus::Stopped(SIGTSTP))
        ));
    }

    #[ktest]
    fn group_stop_participation() {
        use crate::process::WaitOptions;

        let mut control = SignalJobControl::default();
        let mut selected = SelectedStop::default();
        let status = ProcessStatus::default();
        let mut first = GroupStopParticipant::default();
        let mut second = GroupStopParticipant::default();
        let mut joined = GroupStopParticipant::default();
        control.select(&mut selected, true);
        assert!(control.begin_stop(&mut selected, &status, SIGSTOP, false));
        control.enroll(&mut first);
        control.enroll(&mut second);
        assert!(status.stop_status().wait(WaitOptions::WSTOPPED).is_none());
        assert!(!control.acknowledge(&mut first, &status));
        control.enroll(&mut joined);
        assert!(!control.leave(&mut first, &status));
        assert!(!control.leave(&mut second, &status));
        assert!(control.acknowledge(&mut joined, &status));
        assert!(!control.acknowledge(&mut joined, &status));
        assert!(status.stop_status().wait(WaitOptions::WSTOPPED).is_some());

        // Joining an already completed stop does not produce another report.
        control.enroll(&mut first);
        assert!(!control.acknowledge(&mut first, &status));
        assert!(status.stop_status().wait(WaitOptions::WSTOPPED).is_none());
        ostd::early_println!("GROUP_STOP_PARTICIPATION pass");
    }

    #[ktest]
    fn group_stop_cancel_and_rejoin() {
        use crate::process::WaitOptions;

        let mut control = SignalJobControl::default();
        let mut selected = SelectedStop::default();
        let status = ProcessStatus::default();
        let mut first = GroupStopParticipant::default();
        let mut second = GroupStopParticipant::default();
        control.select(&mut selected, true);
        assert!(control.begin_stop(&mut selected, &status, SIGSTOP, false));
        control.enroll(&mut first);
        control.enroll(&mut second);
        assert!(!control.acknowledge(&mut first, &status));
        control.cancel(&mut selected);
        first = GroupStopParticipant::Inactive;
        second = GroupStopParticipant::Inactive;
        assert!(control.resume(&status));
        assert!(matches!(
            control.take_notification(&status),
            Some(StopWaitStatus::Stopped(SIGSTOP))
        ));
        assert!(matches!(
            status.stop_status().wait(WaitOptions::WCONTINUED),
            Some(StopWaitStatus::Continue)
        ));

        control.select(&mut selected, true);
        assert!(control.begin_stop(&mut selected, &status, SIGTSTP, false));
        control.enroll(&mut first);
        control.enroll(&mut second);
        // An old waiter checks the current marker after reacquiring the lock;
        // it owes one ACK in this episode, never a saved decrement from before CONT.
        assert!(!control.acknowledge(&mut first, &status));
        assert!(!control.acknowledge(&mut first, &status));
        assert!(control.acknowledge(&mut second, &status));
        assert!(control.resume(&status));
        assert!(matches!(
            control.take_notification(&status),
            Some(StopWaitStatus::Continue)
        ));
        assert!(control.take_notification(&status).is_none());

        control.commit_exit(&status);
        assert!(!control.leave(&mut first, &status));
        assert!(!control.leave(&mut second, &status));
        assert!(!control.resume(&status));
        assert!(control.take_notification(&status).is_none());
        ostd::early_println!("GROUP_STOP_CANCEL_AND_REJOIN pass");
    }
}
