// SPDX-License-Identifier: MPL-2.0

//! Opt-in RISC-V software reboot recovery.

use alloc::{boxed::Box, sync::Arc};
use core::{
    sync::atomic::{AtomicBool, AtomicU32, Ordering},
    time::Duration,
};

use aster_time::read_monotonic_time;
use ostd::{
    panic,
    power::{self, ExitCode},
    timer,
};
use spin::Once;

use crate::{
    fs::vfs::path::MountNamespace,
    thread::work_queue::{self, work_item::WorkItem},
};

const GRACEFUL_REBOOT_GRACE: Duration = Duration::from_secs(10);

static REBOOT_AFTER_SECONDS: AtomicU32 = AtomicU32::new(0);
static RECOVERY_STATE: RecoveryState = RecoveryState::new();
static GRACEFUL_REBOOT_WORK: Once<Arc<WorkItem>> = Once::new();

aster_cmdline::define_kv_param!("asterinas.reboot_after", REBOOT_AFTER_SECONDS);

pub(super) fn arm_if_requested() {
    let seconds = REBOOT_AFTER_SECONDS.load(Ordering::Relaxed);
    let now = read_monotonic_time();
    let Some(deadline) = deadline_after_seconds(now, seconds) else {
        return;
    };

    if !panic::inject_fatal_abort_restart_policy(is_armed) {
        ostd::error!(
            "failed to arm software reboot: fatal-abort restart policy is already installed"
        );
        return;
    }

    GRACEFUL_REBOOT_WORK.call_once(|| WorkItem::new(Box::new(graceful_reboot)));
    RECOVERY_STATE.freeze_deadline(deadline);
    timer::register_high_resolution_callback_on_cpu(on_timer_interrupt);
    RECOVERY_STATE.publish_armed();
    timer::request_interrupt_after(deadline.saturating_sub(read_monotonic_time()));

    ostd::early_println!("ASTERINAS_SOFTWARE_REBOOT_ARMED seconds={}", seconds);
}

pub(crate) fn is_armed() -> bool {
    RECOVERY_STATE.is_armed()
}

pub(crate) fn disarm() -> bool {
    let was_armed = RECOVERY_STATE.disarm();
    if was_armed {
        ostd::early_println!("ASTERINAS_SOFTWARE_REBOOT_DISARMED");
    }
    was_armed
}

fn on_timer_interrupt() {
    let Some(action) = timer_action(&RECOVERY_STATE, read_monotonic_time()) else {
        return;
    };
    match action {
        TimerAction::BeginGraceful => {
            if !RECOVERY_STATE.try_begin_graceful(read_monotonic_time()) {
                return;
            }
            let Some(work) = GRACEFUL_REBOOT_WORK.get() else {
                if RECOVERY_STATE.is_armed() {
                    power::emergency_restart(ExitCode::Failure);
                }
                return;
            };
            if !work_queue::try_submit_high_priority_work_item(work.clone()) {
                if RECOVERY_STATE.is_armed() {
                    power::emergency_restart(ExitCode::Failure);
                }
                return;
            }
            timer::request_interrupt_after(GRACEFUL_REBOOT_GRACE);
        }
        TimerAction::Emergency => {
            if RECOVERY_STATE.is_armed() {
                power::emergency_restart(ExitCode::Failure);
            }
        }
        TimerAction::Rearm(remaining) => {
            // Another one-shot timer may expire before the recovery deadline.
            // Re-arm the shared hardware deadline for the remaining interval.
            timer::request_interrupt_after(remaining);
        }
    }
}

fn graceful_reboot() {
    if !RECOVERY_STATE.is_armed() {
        return;
    }
    ostd::early_println!("ASTERINAS_SOFTWARE_REBOOT_SYNC_START");
    if let Err(error) = MountNamespace::get_init_singleton().sync() {
        ostd::error!("software reboot filesystem sync failed: {:?}", error);
        return;
    }
    if RECOVERY_STATE.is_armed() {
        ostd::early_println!("ASTERINAS_SOFTWARE_REBOOT_SYNC_COMPLETE");
        power::restart(ExitCode::Failure);
    }
}

#[derive(Debug, PartialEq)]
enum TimerAction {
    BeginGraceful,
    Emergency,
    Rearm(Duration),
}

fn timer_action(state: &RecoveryState, now: Duration) -> Option<TimerAction> {
    let deadline = state.armed_deadline()?;
    let next_deadline = if state.graceful_started() {
        state
            .graceful_fallback_deadline()
            .unwrap_or(deadline.saturating_add(GRACEFUL_REBOOT_GRACE))
    } else {
        deadline
    };
    let remaining = remaining_before_deadline(now, Some(next_deadline))?;
    if remaining.is_zero() {
        if state.graceful_started() {
            Some(TimerAction::Emergency)
        } else {
            Some(TimerAction::BeginGraceful)
        }
    } else {
        Some(TimerAction::Rearm(remaining))
    }
}

fn deadline_after_seconds(now: Duration, seconds: u32) -> Option<Duration> {
    if seconds == 0 {
        return None;
    }

    Some(now.saturating_add(Duration::from_secs(u64::from(seconds))))
}

fn remaining_before_deadline(now: Duration, deadline: Option<Duration>) -> Option<Duration> {
    deadline.map(|deadline| deadline.saturating_sub(now))
}

struct RecoveryState {
    deadline: Once<Duration>,
    fallback_deadline: Once<Duration>,
    is_armed: AtomicBool,
    graceful_started: AtomicBool,
}

impl RecoveryState {
    const fn new() -> Self {
        Self {
            deadline: Once::new(),
            fallback_deadline: Once::new(),
            is_armed: AtomicBool::new(false),
            graceful_started: AtomicBool::new(false),
        }
    }

    fn freeze_deadline(&self, deadline: Duration) {
        self.deadline.call_once(|| deadline);
    }

    fn publish_armed(&self) {
        debug_assert!(self.deadline.get().is_some());
        self.is_armed.store(true, Ordering::Release);
    }

    fn is_armed(&self) -> bool {
        self.is_armed.load(Ordering::Acquire)
    }

    fn disarm(&self) -> bool {
        self.is_armed.swap(false, Ordering::AcqRel)
    }

    fn graceful_started(&self) -> bool {
        self.graceful_started.load(Ordering::Acquire)
    }

    fn try_begin_graceful(&self, now: Duration) -> bool {
        self.fallback_deadline
            .call_once(|| now.saturating_add(GRACEFUL_REBOOT_GRACE));
        self.graceful_started
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
    }

    fn graceful_fallback_deadline(&self) -> Option<Duration> {
        self.fallback_deadline.get().copied()
    }

    fn armed_deadline(&self) -> Option<Duration> {
        if !self.is_armed() {
            return None;
        }

        self.deadline.get().copied()
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::*;

    use super::*;

    #[ktest]
    fn zero_seconds_leaves_recovery_disabled() {
        assert!(deadline_after_seconds(Duration::from_secs(10), 0).is_none());
    }

    #[ktest]
    fn nonzero_seconds_creates_future_deadline() {
        let deadline = deadline_after_seconds(Duration::from_secs(10), 2).unwrap();
        assert_eq!(deadline, Duration::from_secs(12));
    }

    #[ktest]
    fn deadline_calculation_saturates() {
        let now = Duration::MAX - Duration::from_millis(500);
        let deadline = deadline_after_seconds(now, 1).unwrap();
        assert_eq!(deadline, Duration::MAX);
    }

    #[ktest]
    fn time_before_deadline_rearms_the_remaining_duration() {
        let deadline = Duration::from_secs(100);
        assert_eq!(
            remaining_before_deadline(Duration::from_secs(99), Some(deadline)),
            Some(Duration::from_secs(1))
        );
    }

    #[ktest]
    fn time_at_deadline_starts_orderly_reboot() {
        let state = RecoveryState::new();
        state.freeze_deadline(Duration::from_secs(100));
        state.publish_armed();

        assert_eq!(
            timer_action(&state, Duration::from_secs(100)),
            Some(TimerAction::BeginGraceful)
        );
    }

    #[ktest]
    fn orderly_reboot_retains_an_emergency_deadline() {
        let state = RecoveryState::new();
        state.freeze_deadline(Duration::from_secs(100));
        state.publish_armed();
        assert!(state.try_begin_graceful(Duration::from_secs(140)));

        assert_eq!(
            timer_action(&state, Duration::from_secs(149)),
            Some(TimerAction::Rearm(Duration::from_secs(1)))
        );
        assert_eq!(
            timer_action(&state, Duration::from_secs(150)),
            Some(TimerAction::Emergency)
        );
    }

    #[ktest]
    fn fatal_restart_is_selected_only_after_recovery_is_armed() {
        let state = RecoveryState::new();
        state.freeze_deadline(Duration::from_secs(100));
        assert!(!state.is_armed());
        assert!(state.armed_deadline().is_none());

        state.publish_armed();
        assert!(state.is_armed());
        assert_eq!(state.armed_deadline(), Some(Duration::from_secs(100)));
    }

    #[ktest]
    fn first_disarm_clears_an_armed_recovery() {
        let state = RecoveryState::new();
        state.freeze_deadline(Duration::from_secs(100));
        state.publish_armed();

        assert!(state.disarm());
        assert!(!state.is_armed());
        assert!(state.armed_deadline().is_none());
    }

    #[ktest]
    fn repeated_disarm_is_a_one_way_noop() {
        let state = RecoveryState::new();
        state.freeze_deadline(Duration::from_secs(100));
        state.publish_armed();

        assert!(state.disarm());
        assert!(!state.disarm());
        assert_eq!(state.deadline.get(), Some(&Duration::from_secs(100)));
    }

    #[ktest]
    fn disarm_of_disabled_recovery_is_a_noop() {
        let state = RecoveryState::new();

        assert!(!state.disarm());
        assert!(!state.is_armed());
        assert!(state.armed_deadline().is_none());
    }

    #[ktest]
    fn stale_timer_action_is_ignored_after_disarm() {
        let state = RecoveryState::new();
        state.freeze_deadline(Duration::from_secs(100));
        state.publish_armed();
        assert!(state.try_begin_graceful(Duration::from_secs(100)));

        assert!(state.disarm());
        assert_eq!(timer_action(&state, Duration::from_secs(110)), None);
    }
}
