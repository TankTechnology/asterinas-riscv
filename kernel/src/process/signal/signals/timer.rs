// SPDX-License-Identifier: MPL-2.0

use alloc::sync::Arc;
use core::fmt::Debug;

use ostd::sync::SpinLock;

use super::Signal;
use crate::process::signal::{
    c_types::{siginfo_t, sigval_t},
    constants::SI_TIMER,
    sig_num::SigNum,
};

#[derive(Default)]
struct TimerSignalInner {
    pending: bool,
    overrun: i32,
    last_overrun: i32,
}

/// Delivery state shared by a POSIX timer and its pending signal.
pub struct TimerSignalState {
    inner: SpinLock<TimerSignalInner>,
}

impl TimerSignalState {
    pub fn new() -> Self {
        Self {
            inner: SpinLock::new(TimerSignalInner::default()),
        }
    }

    /// Records one callback and any additional expirations it represents.
    /// Returns true when a new signal must be queued.
    pub fn record_expirations(&self, callback_overrun: u64) -> bool {
        let callback_overrun = i32::try_from(callback_overrun).unwrap_or(i32::MAX);
        let mut inner = self.inner.disable_irq().lock();
        if inner.pending {
            inner.overrun = inner
                .overrun
                .saturating_add(callback_overrun.saturating_add(1));
            false
        } else {
            inner.pending = true;
            // A previously generated notification may have been discarded
            // because another standard signal with the same number was
            // already queued. Keep those expirations as overruns for the next
            // notification that can actually be delivered.
            inner.overrun = inner.overrun.saturating_add(callback_overrun);
            true
        }
    }

    /// Reopens delivery after a generated notification was discarded.
    ///
    /// Standard signals do not queue. A POSIX timer notification can therefore
    /// be dropped when an unrelated signal with the same number is already
    /// pending. The expiration represented by the dropped notification must
    /// be carried into the next successful notification, and future timer
    /// callbacks must be allowed to enqueue that notification.
    fn discard_delivery(&self) {
        let mut inner = self.inner.disable_irq().lock();
        if !inner.pending {
            return;
        }
        inner.pending = false;
        inner.overrun = inner.overrun.saturating_add(1);
    }

    fn complete_delivery(&self) -> i32 {
        let mut inner = self.inner.disable_irq().lock();
        let overrun = inner.overrun;
        inner.pending = false;
        inner.overrun = 0;
        inner.last_overrun = overrun;
        overrun
    }

    pub fn last_overrun(&self) -> i32 {
        self.inner.disable_irq().lock().last_overrun
    }
}

impl Default for TimerSignalState {
    fn default() -> Self {
        Self::new()
    }
}

/// A POSIX timer signal carrying Linux-compatible `siginfo_t` fields.
pub struct TimerSignal {
    num: SigNum,
    timer_id: i32,
    value: sigval_t,
    state: Arc<TimerSignalState>,
}

impl TimerSignal {
    pub fn new(num: SigNum, timer_id: i32, value: sigval_t, state: Arc<TimerSignalState>) -> Self {
        Self {
            num,
            timer_id,
            value,
            state,
        }
    }
}

impl Debug for TimerSignal {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("TimerSignal")
            .field("num", &self.num)
            .field("timer_id", &self.timer_id)
            .finish_non_exhaustive()
    }
}

impl Signal for TimerSignal {
    fn num(&self) -> SigNum {
        self.num
    }

    fn to_info(&self) -> siginfo_t {
        let mut info = siginfo_t::new(self.num, SI_TIMER);
        info.set_timer(self.timer_id, self.state.complete_delivery(), self.value);
        info
    }
}

impl Drop for TimerSignal {
    fn drop(&mut self) {
        // `to_info` completes a delivered notification first, making this a
        // no-op. If the signal queue discarded the notification, this resets
        // the timer-specific pending state so a later expiration is not lost
        // forever.
        self.state.discard_delivery();
    }
}

#[cfg(ktest)]
mod tests {
    use alloc::sync::Arc;

    use ostd::prelude::ktest;

    use super::{TimerSignal, TimerSignalState};
    use crate::process::signal::{c_types::sigval_t, constants::SIGALRM};

    #[ktest]
    fn posix_timer_coalesced_expirations_accumulate_overrun() {
        let state = TimerSignalState::new();
        assert!(state.record_expirations(2));
        assert!(!state.record_expirations(3));
        assert_eq!(state.complete_delivery(), 6);
        assert_eq!(state.last_overrun(), 6);
        assert!(state.record_expirations(0));
    }

    #[ktest]
    fn posix_timer_overrun_saturates_at_int_max() {
        let state = TimerSignalState::new();
        assert!(state.record_expirations(u64::MAX));
        assert!(!state.record_expirations(u64::MAX));
        assert_eq!(state.complete_delivery(), i32::MAX);
    }

    #[ktest]
    fn discarded_posix_timer_signal_can_be_retried() {
        let state = Arc::new(TimerSignalState::new());
        assert!(state.record_expirations(2));

        let signal = TimerSignal::new(SIGALRM, 7, sigval_t::from_int(0), state.clone());
        drop(signal);

        assert!(state.record_expirations(3));
        assert_eq!(state.complete_delivery(), 6);
    }
}
