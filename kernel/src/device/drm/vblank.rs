// SPDX-License-Identifier: MPL-2.0

//! Display refresh clock used by KMS completion paths.
//!
//! [`VblankClock`] derives a refresh sequence from the active mode
//! because the virtio-gpu transport has no physical vertical-blank interrupt.
//! [`VblankSnapshot`] carries the sequence and timestamp used for KMS events.
//! A future display backend can replace the clock source without changing
//! page-flip, event, or fence ownership.

use core::time::Duration;

use ostd::sync::WaitQueue;

use super::DEFAULT_REFRESH_HZ;
use crate::prelude::*;

const FRAME_DURATION: Duration = Duration::from_nanos(1_000_000_000 / DEFAULT_REFRESH_HZ as u64);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct VblankSnapshot {
    pub(super) sequence: u64,
    pub(super) timestamp: Duration,
    active: bool,
}

#[derive(Debug)]
struct VblankState {
    active: bool,
    base_sequence: u64,
    epoch: Duration,
}

/// A monotonically increasing display-refresh sequence and timestamp source.
pub(super) struct VblankClock {
    state: SpinLock<VblankState>,
    waiters: WaitQueue,
}

impl VblankClock {
    pub(super) fn new() -> Self {
        Self {
            state: SpinLock::new(VblankState {
                active: false,
                base_sequence: 0,
                epoch: aster_time::read_monotonic_time(),
            }),
            waiters: WaitQueue::new(),
        }
    }

    /// Starts refresh accounting without resetting the sequence.
    pub(super) fn start(&self) {
        self.transition(true);
    }

    /// Stops refresh accounting without resetting the sequence.
    pub(super) fn stop(&self) {
        self.transition(false);
    }

    fn transition(&self, active: bool) {
        let now = aster_time::read_monotonic_time();
        let mut state = self.state.lock();
        if state.active == active {
            return;
        }
        state.transition_at(active, now);
        drop(state);
        self.waiters.wake_all();
    }

    pub(super) fn snapshot(&self) -> VblankSnapshot {
        self.state
            .lock()
            .snapshot_at(aster_time::read_monotonic_time())
    }

    /// Parks until the next refresh boundary, or returns immediately if off.
    pub(super) fn wait_for_next(&self) -> VblankSnapshot {
        let target = {
            let snapshot = self.snapshot();
            if !snapshot.active {
                return snapshot;
            }
            snapshot.sequence.saturating_add(1)
        };

        loop {
            let timeout = {
                let now = aster_time::read_monotonic_time();
                let state = self.state.lock();
                let snapshot = state.snapshot_at(now);
                if !snapshot.active || snapshot.sequence >= target {
                    return snapshot;
                }
                state.deadline_for(target).saturating_sub(now)
            };
            let _ = self.waiters.wait_until_or_timeout(
                || {
                    let snapshot = self.snapshot();
                    (!snapshot.active || snapshot.sequence >= target).then_some(snapshot)
                },
                &timeout,
            );
        }
    }
}

impl VblankState {
    fn transition_at(&mut self, active: bool, now: Duration) {
        let snapshot = self.snapshot_at(now);
        self.active = active;
        self.base_sequence = snapshot.sequence;
        self.epoch = if active { now } else { snapshot.timestamp };
    }

    fn snapshot_at(&self, now: Duration) -> VblankSnapshot {
        if !self.active {
            return VblankSnapshot {
                sequence: self.base_sequence,
                timestamp: self.epoch,
                active: false,
            };
        }

        let elapsed = now.saturating_sub(self.epoch);
        let elapsed_frames = elapsed.as_nanos() / FRAME_DURATION.as_nanos();
        let elapsed_frames = u64::try_from(elapsed_frames).unwrap_or(u64::MAX);
        let sequence = self.base_sequence.saturating_add(elapsed_frames);
        let timestamp = frame_timestamp(self.epoch, elapsed_frames);
        VblankSnapshot {
            sequence,
            timestamp,
            active: true,
        }
    }

    fn deadline_for(&self, sequence: u64) -> Duration {
        let frames = sequence.saturating_sub(self.base_sequence);
        frame_timestamp(self.epoch, frames)
    }
}

fn frame_timestamp(epoch: Duration, frames: u64) -> Duration {
    let nanos = FRAME_DURATION.as_nanos().saturating_mul(u128::from(frames));
    let nanos = u64::try_from(nanos).unwrap_or(u64::MAX);
    epoch.saturating_add(Duration::from_nanos(nanos))
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn inactive_clock_does_not_advance() {
        let state = VblankState {
            active: false,
            base_sequence: 7,
            epoch: Duration::from_secs(2),
        };
        let snapshot = state.snapshot_at(Duration::from_secs(10));
        assert_eq!(snapshot.sequence, 7);
        assert_eq!(snapshot.timestamp, Duration::from_secs(2));
        assert!(!snapshot.active);
    }

    #[ktest]
    fn active_clock_derives_sequence_and_timestamp() {
        let epoch = Duration::from_secs(2);
        let state = VblankState {
            active: true,
            base_sequence: 11,
            epoch,
        };
        let snapshot = state.snapshot_at(epoch + FRAME_DURATION * 3);
        assert_eq!(snapshot.sequence, 14);
        assert_eq!(snapshot.timestamp, epoch + FRAME_DURATION * 3);
        assert!(snapshot.active);
    }

    #[ktest]
    fn stopping_clock_preserves_last_refresh_timestamp() {
        let epoch = Duration::from_secs(2);
        let mut state = VblankState {
            active: true,
            base_sequence: 11,
            epoch,
        };
        state.transition_at(false, epoch + FRAME_DURATION * 3 + Duration::from_millis(1));

        let snapshot = state.snapshot_at(Duration::from_secs(10));
        assert_eq!(snapshot.sequence, 14);
        assert_eq!(snapshot.timestamp, epoch + FRAME_DURATION * 3);
        assert!(!snapshot.active);
    }
}
