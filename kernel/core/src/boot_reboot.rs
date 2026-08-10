// SPDX-License-Identifier: MPL-2.0

//! Opt-in RISC-V software reboot recovery.

use core::sync::atomic::{AtomicBool, AtomicU32, Ordering};

use ostd::{
    panic,
    power::{self, ExitCode},
    timer::{self, Jiffies},
};
use spin::Once;

static REBOOT_AFTER_SECONDS: AtomicU32 = AtomicU32::new(0);
static RECOVERY_STATE: RecoveryState = RecoveryState::new();

aster_cmdline::define_kv_param!("asterinas.reboot_after", REBOOT_AFTER_SECONDS);

pub(super) fn arm_if_requested() {
    let seconds = REBOOT_AFTER_SECONDS.load(Ordering::Relaxed);
    let Some(deadline) = deadline_after_seconds(Jiffies::elapsed(), seconds) else {
        return;
    };

    if !panic::inject_fatal_abort_restart_policy(is_armed) {
        ostd::error!(
            "failed to arm software reboot: fatal-abort restart policy is already installed"
        );
        return;
    }

    RECOVERY_STATE.freeze_deadline(deadline);
    timer::register_callback_on_cpu(on_timer_tick);
    RECOVERY_STATE.publish_armed();

    ostd::early_println!("ASTERINAS_SOFTWARE_REBOOT_ARMED seconds={}", seconds);
}

pub(super) fn is_armed() -> bool {
    RECOVERY_STATE.is_armed()
}

fn on_timer_tick() {
    if deadline_reached(Jiffies::elapsed(), RECOVERY_STATE.armed_deadline()) {
        power::emergency_restart(ExitCode::Failure);
    }
}

fn deadline_after_seconds(now: Jiffies, seconds: u32) -> Option<Jiffies> {
    if seconds == 0 {
        return None;
    }

    let timeout_jiffies = u64::from(seconds).saturating_mul(timer::TIMER_FREQ);
    let mut deadline = now;
    deadline.add(timeout_jiffies);
    Some(deadline)
}

fn deadline_reached(now: Jiffies, deadline: Option<Jiffies>) -> bool {
    let Some(deadline) = deadline else {
        return false;
    };

    now.as_u64() >= deadline.as_u64()
}

struct RecoveryState {
    deadline: Once<Jiffies>,
    is_armed: AtomicBool,
}

impl RecoveryState {
    const fn new() -> Self {
        Self {
            deadline: Once::new(),
            is_armed: AtomicBool::new(false),
        }
    }

    fn freeze_deadline(&self, deadline: Jiffies) {
        self.deadline.call_once(|| deadline);
    }

    fn publish_armed(&self) {
        debug_assert!(self.deadline.get().is_some());
        self.is_armed.store(true, Ordering::Release);
    }

    fn is_armed(&self) -> bool {
        self.is_armed.load(Ordering::Acquire)
    }

    fn armed_deadline(&self) -> Option<Jiffies> {
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
        assert!(deadline_after_seconds(Jiffies::new(10), 0).is_none());
    }

    #[ktest]
    fn nonzero_seconds_creates_future_deadline() {
        let deadline = deadline_after_seconds(Jiffies::new(10), 2).unwrap();
        assert_eq!(deadline.as_u64(), 10 + 2 * timer::TIMER_FREQ);
    }

    #[ktest]
    fn deadline_calculation_saturates() {
        let now = Jiffies::new(u64::MAX - timer::TIMER_FREQ / 2);
        let deadline = deadline_after_seconds(now, 1).unwrap();
        assert_eq!(deadline.as_u64(), u64::MAX);
    }

    #[ktest]
    fn time_before_deadline_does_not_request_restart() {
        let deadline = Jiffies::new(100);
        assert!(!deadline_reached(Jiffies::new(99), Some(deadline)));
    }

    #[ktest]
    fn time_at_deadline_requests_restart() {
        let deadline = Jiffies::new(100);
        assert!(deadline_reached(Jiffies::new(100), Some(deadline)));
    }

    #[ktest]
    fn time_after_deadline_requests_restart() {
        let deadline = Jiffies::new(100);
        assert!(deadline_reached(Jiffies::new(101), Some(deadline)));
    }

    #[ktest]
    fn fatal_restart_is_selected_only_after_recovery_is_armed() {
        let state = RecoveryState::new();
        state.freeze_deadline(Jiffies::new(100));
        assert!(!state.is_armed());
        assert!(state.armed_deadline().is_none());

        state.publish_armed();
        assert!(state.is_armed());
        assert_eq!(state.armed_deadline().unwrap().as_u64(), 100);
    }
}
