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
            inner.overrun = callback_overrun;
            true
        }
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

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::TimerSignalState;

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
}
