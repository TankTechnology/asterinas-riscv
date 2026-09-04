// SPDX-License-Identifier: MPL-2.0

use alloc::{
    boxed::Box,
    collections::BinaryHeap,
    sync::{Arc, Weak},
    vec::Vec,
};
use core::{
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

use ostd::sync::{LocalIrqDisabled, SpinLock, SpinLockGuard};

use super::Clock;

/// A timeout, represented in one of the two ways.
#[derive(Clone, Debug)]
pub enum Timeout {
    /// The timeout is reached _after_ the `Duration` time is elapsed.
    After(Duration),
    /// The timeout is reached _when_ the clock's time is equal to `Duration`.
    When(Duration),
}

/// A timer with periodic functionality.
///
/// Setting the timer will trigger a callback function upon expiration of
/// the set time. To enable its periodic functionality, users should set
/// its `interval` field with [`TimerGuard::set_interval`]. By doing this,
/// the timer will use the interval time to configure a new timing after expiration.
pub struct Timer {
    inner: SpinLock<TimerInner>,
    timer_manager: Arc<TimerManager>,
    registered_callback: Box<dyn Fn(TimerGuard) + Send + Sync>,
}

#[derive(Default)]
struct TimerInner {
    interval: Duration,
    timer_callback: Weak<TimerCallback>,
}

/// A guard that provides exclusive access to a `Timer`.
pub struct TimerGuard<'a> {
    inner: SpinLockGuard<'a, TimerInner, LocalIrqDisabled>,
    timer: &'a Arc<Timer>,
    overrun: u64,
}

impl TimerGuard<'_> {
    /// Sets the interval time for this timer.
    ///
    /// The timer will be reset with the interval time upon expiration.
    pub fn set_interval(&mut self, interval: Duration) {
        self.inner.interval = interval;
    }

    /// Sets the timer with a timeout.
    ///
    /// The registered callback function of this timer will be invoked
    /// when reaching timeout. If the timer has a valid interval, this timer
    /// will be set again with the interval when reaching timeout.
    pub fn set_timeout(&mut self, timeout: Timeout) {
        let expired_time = match timeout {
            Timeout::After(timeout) => {
                let now = self.timer.timer_manager.clock.read_time();
                now + timeout
            }
            Timeout::When(timeout) => timeout,
        };

        let timer_weak = Arc::downgrade(self.timer);
        let new_timer_callback = Arc::new(TimerCallback::new(expired_time, timer_weak));

        if let Some(timer_callback) = self.inner.timer_callback.upgrade() {
            timer_callback.cancel();
        }

        self.inner.timer_callback = Arc::downgrade(&new_timer_callback);
        self.timer.timer_manager.insert(new_timer_callback);
    }

    /// Cancels the currently set `TimerCallback`.
    ///
    /// Once cancelled, the current `TimerCallback` will not be triggered again.
    pub fn cancel(&self) {
        if let Some(timer_callback) = self.inner.timer_callback.upgrade() {
            timer_callback.cancel();
        }
    }

    /// Returns the current expired time of this timer.
    pub fn expired_time(&self) -> Duration {
        let timer_callback = self.inner.timer_callback.upgrade();
        timer_callback
            .and_then(|callback| (!callback.is_cancelled()).then_some(callback.expired_time))
            .unwrap_or(Duration::ZERO)
    }

    /// Returns the remain time to expiration of this timer.
    ///
    /// If the timer has not been set, this method
    /// will return `Duration::ZERO`.
    pub fn remain(&self) -> Duration {
        let now = self.timer.timer_manager.clock.read_time();
        let expired_time = self.expired_time();
        if expired_time > now {
            expired_time - now
        } else {
            Duration::ZERO
        }
    }

    /// Returns the interval time of the current timer.
    pub fn interval(&self) -> Duration {
        self.inner.interval
    }

    /// Returns the number of additional interval expirations represented by
    /// the current callback.
    ///
    /// This is zero for one-shot timers and for guards acquired outside an
    /// expiration callback.
    pub fn overrun(&self) -> u64 {
        self.overrun
    }
}

impl Timer {
    /// Creates a `Timer` instance from a [`TimerManager`].
    /// This timer will be managed by the `TimerManager`.
    ///
    /// Note that if the callback instructions involves sleep, users should put these instructions
    /// into something like `WorkQueue` to avoid sleeping during system timer interruptions.
    fn new<F>(registered_callback: F, timer_manager: Arc<TimerManager>) -> Arc<Self>
    where
        F: Fn(TimerGuard) + Send + Sync + 'static,
    {
        Arc::new(Self {
            inner: SpinLock::new(TimerInner::default()),
            timer_manager,
            registered_callback: Box::new(registered_callback),
        })
    }

    /// Locks the timer and returns a [`TimerGuard`] for exclusive access.
    pub fn lock(self: &Arc<Self>) -> TimerGuard<'_> {
        TimerGuard {
            inner: self.inner.disable_irq().lock(),
            timer: self,
            overrun: 0,
        }
    }

    /// Returns a reference to the [`TimerManager`] which manages
    /// the current timer.
    pub fn timer_manager(&self) -> &Arc<TimerManager> {
        &self.timer_manager
    }
}

/// `TimerManager` is used to create timers and manage their expiries. It holds a clock and can
/// create [`Timer`]s based on this clock.
///
/// These created `Timer`s will hold an `Arc` pointer to this manager, hence this manager
/// will be actually dropped after all the created timers have been dropped.
pub struct TimerManager {
    clock: Arc<dyn Clock>,
    timer_callbacks: SpinLock<BinaryHeap<Arc<TimerCallback>>>,
    /// Avoid taking the heap lock on hot paths when this manager has never
    /// been armed or has already drained all callbacks.
    may_have_callbacks: AtomicBool,
}

impl TimerManager {
    /// Creates a `TimerManager` instance from a clock.
    pub fn new(clock: Arc<dyn Clock>) -> Arc<Self> {
        Arc::new(Self {
            clock,
            timer_callbacks: SpinLock::new(BinaryHeap::new()),
            may_have_callbacks: AtomicBool::new(false),
        })
    }

    /// Returns the clock associated with this timer manager.
    pub fn clock(&self) -> &Arc<dyn Clock> {
        &self.clock
    }

    /// Returns whether a given `timeout` is expired.
    pub fn is_expired_timeout(&self, timeout: &Timeout) -> bool {
        match timeout {
            Timeout::After(duration) => *duration == Duration::ZERO,
            Timeout::When(duration) => {
                let now = self.clock.read_time();
                now >= *duration
            }
        }
    }

    fn insert(&self, timer_callback: Arc<TimerCallback>) {
        {
            let mut callbacks = self.timer_callbacks.disable_irq().lock();
            callbacks.push(timer_callback);
            self.may_have_callbacks.store(true, Ordering::Release);
        }
    }

    /// Checks and processes the managed timers.
    ///
    /// If any of the timers have timed out, call the corresponding callback functions.
    pub fn process_expired_timers(&self) {
        if !self.may_have_callbacks.load(Ordering::Acquire) {
            return;
        }

        let callbacks = {
            let mut timeout_list = self.timer_callbacks.disable_irq().lock();
            if timeout_list.is_empty() {
                self.may_have_callbacks.store(false, Ordering::Release);
                return;
            }

            let mut callbacks = Vec::new();
            let current_time = self.clock.read_time();
            while let Some(t) = timeout_list.peek() {
                if t.is_cancelled() {
                    // Just ignore the cancelled callback
                    timeout_list.pop();
                } else if t.expired_time <= current_time {
                    callbacks.push(timeout_list.pop().unwrap());
                } else {
                    break;
                }
            }
            self.may_have_callbacks
                .store(!timeout_list.is_empty(), Ordering::Release);
            callbacks
        };

        for callback in callbacks {
            callback.call();
        }
    }

    /// Creates an [`Timer`], which will be managed by this `TimerManager`.
    pub fn create_timer<F>(self: &Arc<Self>, function: F) -> Arc<Timer>
    where
        F: Fn(TimerGuard) + Send + Sync + 'static,
    {
        Timer::new(function, self.clone())
    }

}

/// A `TimerCallback` can be used to execute a timer callback function.
struct TimerCallback {
    expired_time: Duration,
    timer: Weak<Timer>,
    is_cancelled: AtomicBool,
}

impl TimerCallback {
    /// Creates an instance of `TimerCallback`.
    fn new(timeout: Duration, timer: Weak<Timer>) -> Self {
        Self {
            expired_time: timeout,
            timer,
            is_cancelled: AtomicBool::new(false),
        }
    }

    /// Cancels a `TimerCallback`. If the callback function has not been called,
    /// it will never be called again.
    fn cancel(&self) {
        self.is_cancelled.store(true, Ordering::Release);
    }

    // Returns whether the `TimerCallback` is cancelled.
    fn is_cancelled(&self) -> bool {
        self.is_cancelled.load(Ordering::Acquire)
    }

    fn call(&self) {
        let Some(timer) = self.timer.upgrade() else {
            return;
        };

        let mut timer_guard = timer.lock();

        if self.is_cancelled() {
            // The callback is cancelled.
            return;
        }

        let interval = timer_guard.interval();
        if interval != Duration::ZERO {
            let (overrun, next_expired_time) = periodic_expiration(
                timer.timer_manager.clock.read_time(),
                self.expired_time,
                interval,
            );
            timer_guard.overrun = overrun;
            timer_guard.set_timeout(Timeout::When(next_expired_time));
        }

        // Pass the `timer_guard` guard to the callback, allowing it to prevent race conditions.
        // The callback may choose to use the guard or drop it if not needed.
        (timer.registered_callback)(timer_guard);
    }
}

fn periodic_expiration(
    now: Duration,
    expired_time: Duration,
    interval: Duration,
) -> (u64, Duration) {
    debug_assert_ne!(interval, Duration::ZERO);
    let lateness = now.saturating_sub(expired_time);
    let overrun = lateness.as_nanos() / interval.as_nanos();

    // Rearm relative to the previous deadline, rather than relative to
    // callback execution. This preserves the periodic phase and folds all
    // missed intervals into this callback's overrun count.
    let intervals = overrun.saturating_add(1);
    let offset_nanos = interval.as_nanos().saturating_mul(intervals);
    let offset_secs = offset_nanos / 1_000_000_000;
    let offset = if offset_secs > u128::from(u64::MAX) {
        Duration::MAX
    } else {
        Duration::new(offset_secs as u64, (offset_nanos % 1_000_000_000) as u32)
    };
    let next_expired_time = expired_time.checked_add(offset).unwrap_or(Duration::MAX);
    (
        u64::try_from(overrun).unwrap_or(u64::MAX),
        next_expired_time,
    )
}

impl PartialEq for TimerCallback {
    fn eq(&self, other: &Self) -> bool {
        self.expired_time == other.expired_time
    }
}

impl Eq for TimerCallback {}

impl PartialOrd for TimerCallback {
    fn partial_cmp(&self, other: &Self) -> Option<core::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for TimerCallback {
    fn cmp(&self, other: &Self) -> core::cmp::Ordering {
        // We want `TimerCallback`s to be processed in ascending order of `expired_time`,
        // and the in-order management of `TimerCallback`s currently relies on a maximum heap,
        // so we need the reverse instruction here.
        self.expired_time.cmp(&other.expired_time).reverse()
    }
}

#[cfg(ktest)]
mod tests {
    use core::time::Duration;

    use ostd::prelude::ktest;

    use super::periodic_expiration;

    #[ktest]
    fn posix_timer_periodic_phase_and_overrun_count() {
        let (overrun, next) = periodic_expiration(
            Duration::from_millis(37),
            Duration::from_millis(10),
            Duration::from_millis(5),
        );
        assert_eq!(overrun, 5);
        assert_eq!(next, Duration::from_millis(40));
    }

    #[ktest]
    fn posix_timer_on_time_has_no_overrun() {
        let (overrun, next) = periodic_expiration(
            Duration::from_millis(10),
            Duration::from_millis(10),
            Duration::from_millis(5),
        );
        assert_eq!(overrun, 0);
        assert_eq!(next, Duration::from_millis(15));
    }
}
