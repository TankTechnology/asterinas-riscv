// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::{AtomicBool, AtomicU64, Ordering};

/// Monotonic scheduler counters for one thread.
///
/// Scheduler transitions for a task are serialized by runqueue ownership. The
/// atomics let procfs readers take a non-blocking snapshot while the counters
/// are updated.
#[derive(Debug)]
pub(crate) struct SchedInfo {
    queued: AtomicBool,
    queued_at: AtomicU64,
    run_delay_ticks: AtomicU64,
    dispatches: AtomicU64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Snapshot {
    pub(crate) run_delay_ns: u64,
    pub(crate) dispatches: u64,
}

impl SchedInfo {
    pub(crate) fn new() -> Self {
        Self {
            queued: AtomicBool::new(false),
            queued_at: AtomicU64::new(0),
            run_delay_ticks: AtomicU64::new(0),
            dispatches: AtomicU64::new(0),
        }
    }

    /// Starts a runnable-wait interval unless one is already active.
    pub(crate) fn enqueue_at(&self, now: u64) {
        if !self.queued.load(Ordering::Relaxed) {
            self.queued_at.store(now, Ordering::Relaxed);
            self.queued.store(true, Ordering::Relaxed);
        }
    }

    /// Completes a runnable-wait interval and records one dispatch.
    pub(crate) fn dispatch_at(&self, now: u64) {
        if !self.queued.swap(false, Ordering::Relaxed) {
            return;
        }

        let queued_at = self.queued_at.load(Ordering::Relaxed);
        let delta = now.wrapping_sub(queued_at);
        let _ = self
            .run_delay_ticks
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |old| {
                Some(old.saturating_add(delta))
            });
        let _ = self
            .dispatches
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |old| {
                Some(old.saturating_add(1))
            });
    }

    pub(crate) fn snapshot_at_frequency(&self, frequency: u64) -> Option<Snapshot> {
        if frequency == 0 {
            return None;
        }

        let ticks = self.run_delay_ticks.load(Ordering::Relaxed);
        let nanos = u128::from(ticks)
            .saturating_mul(1_000_000_000)
            .checked_div(u128::from(frequency))?;
        Some(Snapshot {
            run_delay_ns: u64::try_from(nanos).unwrap_or(u64::MAX),
            dispatches: self.dispatches.load(Ordering::Relaxed),
        })
    }

    #[cfg(ktest)]
    pub(super) fn is_queued(&self) -> bool {
        self.queued.load(Ordering::Relaxed)
    }

    #[cfg(ktest)]
    pub(super) fn queued_at(&self) -> u64 {
        self.queued_at.load(Ordering::Relaxed)
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::{SchedInfo, Snapshot};

    #[ktest]
    fn sched_info() {
        duplicate_enqueue_preserves_first_wait_timestamp();
        block_and_wake_excludes_sleeping_time();
        dispatch_without_enqueue_is_a_noop();
        zero_frequency_has_no_snapshot();
        tick_conversion_saturates();
    }

    fn duplicate_enqueue_preserves_first_wait_timestamp() {
        let info = SchedInfo::new();
        info.enqueue_at(10);
        info.enqueue_at(30);
        info.dispatch_at(50);

        assert_eq!(
            info.snapshot_at_frequency(1_000_000_000),
            Some(Snapshot {
                run_delay_ns: 40,
                dispatches: 1,
            })
        );
    }

    fn block_and_wake_excludes_sleeping_time() {
        let info = SchedInfo::new();
        info.enqueue_at(10);
        info.dispatch_at(20);
        info.enqueue_at(100);
        info.dispatch_at(130);

        assert_eq!(
            info.snapshot_at_frequency(1_000_000_000),
            Some(Snapshot {
                run_delay_ns: 40,
                dispatches: 2,
            })
        );
    }

    fn dispatch_without_enqueue_is_a_noop() {
        let info = SchedInfo::new();
        info.dispatch_at(50);

        assert_eq!(
            info.snapshot_at_frequency(1_000_000_000),
            Some(Snapshot {
                run_delay_ns: 0,
                dispatches: 0,
            })
        );
    }

    fn zero_frequency_has_no_snapshot() {
        assert_eq!(SchedInfo::new().snapshot_at_frequency(0), None);
    }

    fn tick_conversion_saturates() {
        let info = SchedInfo::new();
        info.enqueue_at(0);
        info.dispatch_at(u64::MAX);

        assert_eq!(
            info.snapshot_at_frequency(1),
            Some(Snapshot {
                run_delay_ns: u64::MAX,
                dispatches: 1,
            })
        );
    }
}
