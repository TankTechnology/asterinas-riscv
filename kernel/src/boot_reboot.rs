// SPDX-License-Identifier: MPL-2.0

//! Opt-in RISC-V software reboot recovery.

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

static REBOOT_AFTER_SECONDS: AtomicU32 = AtomicU32::new(0);
static RECOVERY_STATE: RecoveryState = RecoveryState::new();

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

    RECOVERY_STATE.freeze_deadline(deadline);
    timer::register_high_resolution_callback_on_cpu(on_timer_interrupt);
    RECOVERY_STATE.publish_armed();
    timer::request_interrupt_after(deadline.saturating_sub(read_monotonic_time()));

    ostd::early_println!("ASTERINAS_SOFTWARE_REBOOT_ARMED seconds={}", seconds);
}

pub(super) fn is_armed() -> bool {
    RECOVERY_STATE.is_armed()
}

fn on_timer_interrupt() {
    let Some(remaining) =
        remaining_before_deadline(read_monotonic_time(), RECOVERY_STATE.armed_deadline())
    else {
        return;
    };
    if remaining.is_zero() {
        power::emergency_restart(ExitCode::Failure);
    } else {
        // Another one-shot timer may expire before the recovery deadline.
        // Re-arm the shared hardware deadline for the remaining interval.
        timer::request_interrupt_after(remaining);
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
    is_armed: AtomicBool,
}

impl RecoveryState {
    const fn new() -> Self {
        Self {
            deadline: Once::new(),
            is_armed: AtomicBool::new(false),
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
    fn time_at_deadline_requests_restart() {
        let deadline = Duration::from_secs(100);
        assert_eq!(
            remaining_before_deadline(deadline, Some(deadline)),
            Some(Duration::ZERO)
        );
    }

    #[ktest]
    fn delayed_timer_interrupt_still_requests_restart() {
        let deadline = Duration::from_secs(100);
        assert_eq!(
            remaining_before_deadline(Duration::from_secs(140), Some(deadline)),
            Some(Duration::ZERO)
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
}
