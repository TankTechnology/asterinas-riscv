// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::{AtomicU64, Ordering};

use aster_bigtcp::iface::ScheduleNextPoll;
use ostd::sync::WaitQueue;

pub struct PollScheduler {
    /// The time when we should do the next poll.
    /// We store the total number of milliseconds since the system booted.
    next_poll_at_ms: AtomicU64,
    /// The wait queue that the background polling thread will sleep on.
    polling_wait_queue: WaitQueue,
}

impl PollScheduler {
    /// Sentinel stored in [`Self::next_poll_at_ms`] to mean "no poll scheduled".
    ///
    /// It must not collide with `0`, which `aster-bigtcp` uses as
    /// `PollKey::IMMEDIATE_VAL` ("poll now"). Using `0` for "no poll" here would
    /// silently drop an immediate-poll request and leave a socket that needs a
    /// `PollAt::Now` poll stranded until some unrelated event wakes the thread.
    const NO_POLL: u64 = u64::MAX;

    pub(super) fn new() -> Self {
        Self {
            next_poll_at_ms: AtomicU64::new(Self::NO_POLL),
            polling_wait_queue: WaitQueue::new(),
        }
    }

    pub(super) fn next_poll_at_ms(&self) -> Option<u64> {
        let millis = self.next_poll_at_ms.load(Ordering::Relaxed);
        if millis == Self::NO_POLL {
            None
        } else {
            Some(millis)
        }
    }

    pub(super) fn polling_wait_queue(&self) -> &WaitQueue {
        &self.polling_wait_queue
    }
}

impl ScheduleNextPoll for PollScheduler {
    fn schedule_next_poll(&self, poll_at: Option<u64>) {
        let new_instant = poll_at.unwrap_or(Self::NO_POLL);

        let old_instant = self.next_poll_at_ms.load(Ordering::Relaxed);
        self.next_poll_at_ms.store(new_instant, Ordering::Relaxed);

        if old_instant == Self::NO_POLL || new_instant < old_instant {
            self.polling_wait_queue.wake_all();
        }
    }
}
