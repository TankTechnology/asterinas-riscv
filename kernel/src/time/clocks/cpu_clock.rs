// SPDX-License-Identifier: MPL-2.0

use alloc::sync::Arc;
use core::{
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};

use ostd::timer::{Jiffies, TIMER_FREQ};

use crate::time::Clock;

/// A clock used to record the CPU time for processes and threads.
pub struct CpuClock {
    nanoseconds: AtomicU64,
}

/// A profiling clock that contains a user CPU clock and a kernel CPU clock.
///
/// These two clocks record the CPU time in user mode and kernel mode respectively.
/// Reading this clock directly returns the sum of both times.
pub struct ProfClock {
    user_clock: Arc<CpuClock>,
    kernel_clock: Arc<CpuClock>,
}

impl CpuClock {
    /// Creates a new `CpuClock`. The recorded time is initialized to 0.
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            nanoseconds: AtomicU64::new(0),
        })
    }

    /// Adds elapsed CPU time to this clock.
    pub fn add_duration(&self, duration: Duration) {
        let nanoseconds = u64::try_from(duration.as_nanos()).unwrap_or(u64::MAX);
        let _ = self
            .nanoseconds
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |old| {
                Some(old.saturating_add(nanoseconds))
            });
    }

    /// Reads the current time of this clock in [`Jiffies`].
    pub fn read_jiffies(&self) -> Jiffies {
        let nanoseconds = self.nanoseconds.load(Ordering::Relaxed);
        let jiffies = (u128::from(nanoseconds) * u128::from(TIMER_FREQ)) / 1_000_000_000;
        Jiffies::new(u64::try_from(jiffies).unwrap_or(u64::MAX))
    }
}

impl Clock for CpuClock {
    fn read_time(&self) -> Duration {
        Duration::from_nanos(self.nanoseconds.load(Ordering::Relaxed))
    }
}

impl ProfClock {
    /// Creates a new `ProfClock`. The recorded time is initialized to 0.
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            user_clock: CpuClock::new(),
            kernel_clock: CpuClock::new(),
        })
    }

    /// Returns a reference to the user CPU clock in this profiling clock.
    pub fn user_clock(&self) -> &Arc<CpuClock> {
        &self.user_clock
    }

    /// Returns a reference to the kernel CPU clock in this profiling clock.
    pub fn kernel_clock(&self) -> &Arc<CpuClock> {
        &self.kernel_clock
    }
}

impl Clock for ProfClock {
    fn read_time(&self) -> Duration {
        self.user_clock.read_time() + self.kernel_clock.read_time()
    }
}
