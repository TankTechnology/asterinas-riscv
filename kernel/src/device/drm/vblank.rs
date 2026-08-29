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

use super::{
    CRTC_ID, DEFAULT_REFRESH_HZ, DRM_CRTC_SEQUENCE_NEXT_ON_MISS, DRM_CRTC_SEQUENCE_RELATIVE,
    DRM_CRTC_SEQUENCE_SUPPORTED_MASK, DRM_VBLANK_EVENT, DRM_VBLANK_HIGH_CRTC_MASK,
    DRM_VBLANK_NEXT_ON_MISS, DRM_VBLANK_RELATIVE, DRM_VBLANK_SECONDARY, DRM_VBLANK_SIGNAL,
    DRM_VBLANK_SUPPORTED_MASK, DriHandle, DrmCrtcGetSequence, DrmCrtcQueueSequence, DrmWaitVblank,
    VBLANK_WAIT_TIMEOUT,
};
use crate::{
    prelude::*,
    util::ioctl::{InOutData, Ioctl},
};

const FRAME_DURATION: Duration = Duration::from_nanos(1_000_000_000 / DEFAULT_REFRESH_HZ as u64);

struct LegacyVblankTarget {
    normalized_type: u32,
    sequence: u64,
}

/// Implements `DRM_IOCTL_WAIT_VBLANK` for the single virtual CRTC.
pub(super) fn wait_vblank(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0x3a, true, InOutData<DrmWaitVblank>>,
) -> Result<i32> {
    if handle.is_render_node() {
        return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
    }

    let mut request = cmd.read()?;
    validate_legacy_vblank_type(request.type_)?;
    let initial = handle.gpu_manager.vblank_clock.snapshot();
    if !initial.is_active() {
        return_errno_with_message!(Errno::EINVAL, "the CRTC is not active");
    }
    let target = resolve_legacy_target(request.type_, request.sequence, initial.sequence);
    request.type_ = target.normalized_type;
    request.sequence = target.sequence as u32;

    if request.type_ & DRM_VBLANK_EVENT != 0 {
        let user_data = request.signal();
        let event_slot = handle.event_queue.reserve()?;
        cmd.write(&request)?;

        let current = handle.gpu_manager.vblank_clock.snapshot();
        if !current.is_active() || current.sequence >= target.sequence {
            event_slot.queue_vblank(current, user_data);
        } else {
            handle.vblank_completion_queue.submit_at(
                target.sequence,
                Box::new(move |snapshot| event_slot.queue_vblank(snapshot, user_data)),
            );
        }
        return Ok(0);
    }

    match handle
        .gpu_manager
        .vblank_clock
        .pause_until(target.sequence, VBLANK_WAIT_TIMEOUT)
    {
        Ok(snapshot) => {
            request.set_reply(snapshot);
            cmd.write(&request)?;
            Ok(0)
        }
        Err(error) if error.error() == Errno::ETIME => {
            request.set_reply(handle.gpu_manager.vblank_clock.snapshot());
            cmd.write(&request)?;
            return_errno_with_message!(Errno::EBUSY, "the vblank wait timed out");
        }
        Err(error) => Err(error),
    }
}

/// Implements `DRM_IOCTL_CRTC_GET_SEQUENCE`.
pub(super) fn get_crtc_sequence(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0x3b, true, InOutData<DrmCrtcGetSequence>>,
) -> Result<i32> {
    if handle.is_render_node() {
        return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
    }

    let mut request = cmd.read()?;
    if request.crtc_id != CRTC_ID {
        return_errno_with_message!(Errno::ENOENT, "unknown CRTC id");
    }
    let snapshot = handle.gpu_manager.vblank_clock.snapshot();
    request.active = u32::from(snapshot.is_active());
    request.sequence = snapshot.sequence;
    request.sequence_ns = i64::try_from(snapshot.timestamp.as_nanos()).unwrap_or(i64::MAX);
    cmd.write(&request)?;
    Ok(0)
}

/// Implements `DRM_IOCTL_CRTC_QUEUE_SEQUENCE`.
pub(super) fn queue_crtc_sequence(
    handle: &DriHandle,
    cmd: Ioctl<b'd', 0x3c, true, InOutData<DrmCrtcQueueSequence>>,
) -> Result<i32> {
    if handle.is_render_node() {
        return_errno_with_message!(Errno::EOPNOTSUPP, "KMS ioctl not available on render node");
    }

    let mut request = cmd.read()?;
    if request.crtc_id != CRTC_ID {
        return_errno_with_message!(Errno::ENOENT, "unknown CRTC id");
    }
    if request.flags & !DRM_CRTC_SEQUENCE_SUPPORTED_MASK != 0 {
        return_errno_with_message!(Errno::EINVAL, "unsupported CRTC sequence flags");
    }

    let initial = handle.gpu_manager.vblank_clock.snapshot();
    if !initial.is_active() {
        return_errno_with_message!(Errno::EINVAL, "the CRTC is not active");
    }
    let mut target_sequence = request.sequence;
    if request.flags & DRM_CRTC_SEQUENCE_RELATIVE != 0 {
        target_sequence = initial.sequence.saturating_add(target_sequence);
    }
    if request.flags & DRM_CRTC_SEQUENCE_NEXT_ON_MISS != 0 && initial.sequence >= target_sequence {
        target_sequence = initial.sequence.saturating_add(1);
    }
    if initial.sequence >= target_sequence {
        target_sequence = initial.sequence;
    }

    let user_data = request.user_data;
    let event_slot = handle.event_queue.reserve()?;
    request.sequence = target_sequence;
    cmd.write(&request)?;

    let current = handle.gpu_manager.vblank_clock.snapshot();
    if !current.is_active() || current.sequence >= target_sequence {
        event_slot.queue_crtc_sequence(current, user_data);
    } else {
        handle.vblank_completion_queue.submit_at(
            target_sequence,
            Box::new(move |snapshot| event_slot.queue_crtc_sequence(snapshot, user_data)),
        );
    }
    Ok(0)
}

fn validate_legacy_vblank_type(request_type: u32) -> Result<()> {
    if request_type & DRM_VBLANK_SIGNAL != 0 {
        return_errno_with_message!(Errno::EINVAL, "vblank signals are not supported");
    }
    if request_type & !DRM_VBLANK_SUPPORTED_MASK != 0 {
        return_errno_with_message!(Errno::EINVAL, "unsupported vblank request type");
    }

    let high_crtc = request_type & DRM_VBLANK_HIGH_CRTC_MASK;
    let selects_secondary = request_type & DRM_VBLANK_SECONDARY != 0;
    if high_crtc != 0 || selects_secondary {
        return_errno_with_message!(Errno::EINVAL, "unknown CRTC index");
    }
    Ok(())
}

fn resolve_legacy_target(
    request_type: u32,
    requested_sequence: u32,
    current_sequence: u64,
) -> LegacyVblankTarget {
    let mut normalized_type = request_type;
    let mut sequence = if request_type & DRM_VBLANK_RELATIVE != 0 {
        normalized_type &= !DRM_VBLANK_RELATIVE;
        current_sequence.saturating_add(u64::from(requested_sequence))
    } else {
        let delta = requested_sequence.wrapping_sub(current_sequence as u32) as i32;
        if delta > 0 {
            current_sequence.saturating_add(delta as u64)
        } else {
            current_sequence
        }
    };

    let request_was_missed = sequence <= current_sequence;
    if request_type & DRM_VBLANK_NEXT_ON_MISS != 0 && request_was_missed {
        normalized_type &= !DRM_VBLANK_NEXT_ON_MISS;
        sequence = current_sequence.saturating_add(1);
    }
    LegacyVblankTarget {
        normalized_type,
        sequence,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct VblankSnapshot {
    pub(super) sequence: u64,
    pub(super) timestamp: Duration,
    active: bool,
}

impl VblankSnapshot {
    pub(super) fn is_active(self) -> bool {
        self.active
    }

    #[cfg(ktest)]
    pub(super) const fn active_for_test(sequence: u64, timestamp: Duration) -> Self {
        Self {
            sequence,
            timestamp,
            active: true,
        }
    }
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
        let snapshot = self.snapshot();
        if !snapshot.active {
            return snapshot;
        }
        self.wait_until(snapshot.sequence.saturating_add(1))
    }

    /// Parks until `target_sequence`, or returns immediately if scanout stops.
    pub(super) fn wait_until(&self, target_sequence: u64) -> VblankSnapshot {
        self.wait_until_or(target_sequence, || false)
    }

    /// Parks until `target_sequence`, scanout stops, or `should_wake` changes.
    pub(super) fn wait_until_or(
        &self,
        target_sequence: u64,
        mut should_wake: impl FnMut() -> bool,
    ) -> VblankSnapshot {
        loop {
            if should_wake() {
                return self.snapshot();
            }
            let timeout = {
                let now = aster_time::read_monotonic_time();
                let state = self.state.lock();
                let snapshot = state.snapshot_at(now);
                if !snapshot.active || snapshot.sequence >= target_sequence {
                    return snapshot;
                }
                state.deadline_for(target_sequence).saturating_sub(now)
            };
            let _ = self.waiters.wait_until_or_timeout(
                || {
                    let snapshot = self.snapshot();
                    (should_wake() || !snapshot.active || snapshot.sequence >= target_sequence)
                        .then_some(snapshot)
                },
                &timeout,
            );
        }
    }

    /// Wakes workers whose earliest target may have changed.
    pub(super) fn notify_waiters(&self) {
        self.waiters.wake_all();
    }

    /// Pauses interruptibly for a sequence, bounded by `max_wait`.
    pub(super) fn pause_until(
        &self,
        target_sequence: u64,
        max_wait: Duration,
    ) -> Result<VblankSnapshot> {
        let wait_deadline = aster_time::read_monotonic_time().saturating_add(max_wait);

        loop {
            let timeout = {
                let now = aster_time::read_monotonic_time();
                let state = self.state.lock();
                let snapshot = state.snapshot_at(now);
                if !snapshot.active || snapshot.sequence >= target_sequence {
                    return Ok(snapshot);
                }
                if now >= wait_deadline {
                    return_errno_with_message!(Errno::ETIME, "the vblank wait timed out");
                }
                state
                    .deadline_for(target_sequence)
                    .min(wait_deadline)
                    .saturating_sub(now)
            };

            match self.waiters.pause_until_or_timeout(
                || {
                    let snapshot = self.snapshot();
                    (!snapshot.active || snapshot.sequence >= target_sequence).then_some(snapshot)
                },
                &timeout,
            ) {
                Ok(snapshot) => return Ok(snapshot),
                Err(error) if error.error() == Errno::ETIME => continue,
                Err(error) => return Err(error),
            }
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

    #[ktest]
    fn relative_wait_becomes_an_absolute_target() {
        let target = resolve_legacy_target(DRM_VBLANK_RELATIVE | DRM_VBLANK_EVENT, 3, 11);

        assert_eq!(target.sequence, 14);
        assert_eq!(target.normalized_type, DRM_VBLANK_EVENT);
    }

    #[ktest]
    fn missed_absolute_wait_returns_the_current_sequence() {
        let target = resolve_legacy_target(0, u32::MAX, 0);

        assert_eq!(target.sequence, 0);
    }

    #[ktest]
    fn next_on_miss_advances_one_refresh() {
        let target = resolve_legacy_target(DRM_VBLANK_NEXT_ON_MISS, 7, 7);

        assert_eq!(target.sequence, 8);
        assert_eq!(target.normalized_type, 0);
    }

    #[ktest]
    fn legacy_wait_rejects_unsupported_crtcs_and_flags() {
        assert!(validate_legacy_vblank_type(DRM_VBLANK_SECONDARY).is_err());
        assert!(validate_legacy_vblank_type(2).is_err());
        assert!(validate_legacy_vblank_type(DRM_VBLANK_SIGNAL).is_err());
        assert!(validate_legacy_vblank_type(0x08000000).is_err());
        assert!(validate_legacy_vblank_type(DRM_VBLANK_EVENT).is_ok());
    }

    #[ktest]
    fn vblank_uapi_layouts_match_linux_64_bit() {
        assert_eq!(size_of::<DrmWaitVblank>(), 24);
        assert_eq!(size_of::<DrmCrtcGetSequence>(), 24);
        assert_eq!(size_of::<DrmCrtcQueueSequence>(), 24);
        assert_eq!(size_of::<super::super::DrmEventVblank>(), 32);
        assert_eq!(size_of::<super::super::DrmEventCrtcSequence>(), 32);
    }
}
