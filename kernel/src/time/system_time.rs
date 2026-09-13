// SPDX-License-Identifier: MPL-2.0

use core::{
    sync::atomic::{AtomicI64, Ordering},
    time::Duration,
};

use aster_time::{read_monotonic_time, read_start_time};
use spin::Once;
use time::{Date, Month, PrimitiveDateTime, Time};

use crate::prelude::*;

/// This struct corresponds to `SystemTime` in Rust std.
#[derive(Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct SystemTime(PrimitiveDateTime);

pub static START_TIME: Once<SystemTime> = Once::new();
pub(super) static START_TIME_AS_DURATION: Once<Duration> = Once::new();

/// The wall-clock adjustment in nanoseconds, applied on top of
/// `START_TIME + monotonic time`. It is changed by `clock_settime` and is
/// zero at boot.
static WALL_CLOCK_ADJUST_NANOS: AtomicI64 = AtomicI64::new(0);

pub(super) fn init() {
    let start_time = convert_system_time(read_start_time()).unwrap();
    START_TIME_AS_DURATION
        .call_once(|| start_time.duration_since(&SystemTime::UNIX_EPOCH).unwrap());
    START_TIME.call_once(|| start_time);
}

impl SystemTime {
    /// The unix epoch, which represents 1970-01-01 00:00:00
    pub const UNIX_EPOCH: SystemTime = SystemTime::unix_epoch();

    const fn unix_epoch() -> Self {
        // 1970-01-01 00:00:00
        let Ok(date) = Date::from_ordinal_date(1970, 1) else {
            unreachable!()
        };
        let Ok(time) = Time::from_hms_nano(0, 0, 0, 0) else {
            unreachable!()
        };

        SystemTime(PrimitiveDateTime::new(date, time))
    }

    /// Returns the current system time
    pub fn now() -> Self {
        // The get real time result should always be valid
        let base = START_TIME
            .get()
            .unwrap()
            .checked_add(read_monotonic_time())
            .unwrap();

        let adjust_nanos = WALL_CLOCK_ADJUST_NANOS.load(Ordering::Acquire);
        if adjust_nanos == 0 {
            return base;
        }
        base.0
            .checked_add(time::Duration::nanoseconds(adjust_nanos))
            .map(SystemTime)
            .expect("the wall-clock adjustment pushed the system time out of range")
    }

    /// Sets the wall clock to the given time, moving all future readings of
    /// the real-time clock (`clock_settime(CLOCK_REALTIME)` semantics).
    pub fn set(time: SystemTime) {
        let raw = START_TIME
            .get()
            .unwrap()
            .checked_add(read_monotonic_time())
            .unwrap();
        let adjust = time.0 - raw.0;
        WALL_CLOCK_ADJUST_NANOS.store(adjust.whole_nanoseconds() as i64, Ordering::Release);

        // Keep syscall and vDSO coarse clocks on the same post-adjustment
        // snapshot instead of waiting for the next timer tick.
        crate::time::clocks::update_coarse_clock();

        // Refresh the vDSO data page immediately so that vDSO-accelerated
        // `clock_gettime(CLOCK_REALTIME)` sees the new wall clock at once.
        crate::vdso::on_wall_clock_change();
    }

    /// Add a duration to self. If the result does not exceed inner bounds return Some(t), else return None.
    pub fn checked_add(&self, duration: Duration) -> Option<Self> {
        let duration = convert_to_time_duration(duration);
        self.0.checked_add(duration).map(SystemTime)
    }

    /// Subtract a duration from self. If the result does not exceed inner bounds return Some(t), else return None.
    #[expect(dead_code)]
    pub fn checked_sub(&self, duration: Duration) -> Option<Self> {
        let duration = convert_to_time_duration(duration);
        self.0.checked_sub(duration).map(SystemTime)
    }

    /// Returns the duration since an earlier time. Return error if `earlier` is later than self.
    pub fn duration_since(&self, earlier: &SystemTime) -> Result<Duration> {
        if self.0 < earlier.0 {
            return_errno_with_message!(
                Errno::EINVAL,
                "duration_since can only accept an earlier time"
            );
        }
        let duration = self.0 - earlier.0;
        Ok(convert_to_core_duration(duration))
    }

    /// Return the difference between current time and the time when self was created.
    /// Return Error if current time is earlier than creating time.
    /// The error can happen if self was created by checked_add.
    #[expect(dead_code)]
    pub fn elapsed(&self) -> Result<Duration> {
        let now = SystemTime::now();
        now.duration_since(self)
    }
}

/// Returns the current wall-clock adjustment in (signed) nanoseconds.
///
/// The vDSO data page computes the real time without going through
/// [`SystemTime::now`], so it needs the adjustment directly.
pub(crate) fn wall_clock_adjust_nanos() -> i64 {
    WALL_CLOCK_ADJUST_NANOS.load(Ordering::Acquire)
}

/// convert ostd::time::Time to System time
fn convert_system_time(system_time: aster_time::SystemTime) -> Result<SystemTime> {
    let month = match Month::try_from(system_time.month) {
        Ok(month) => month,
        Err(_) => return_errno_with_message!(Errno::EINVAL, "unknown month in system time"),
    };
    let date = match Date::from_calendar_date(system_time.year as _, month, system_time.day) {
        Ok(date) => date,
        Err(_) => return_errno_with_message!(Errno::EINVAL, "Invalid system date"),
    };
    let time_ = match Time::from_hms_nano(
        system_time.hour,
        system_time.minute,
        system_time.second,
        system_time.nanos.try_into().unwrap(),
    ) {
        Ok(time_) => time_,
        Err(_) => return_errno_with_message!(Errno::EINVAL, "Invalid system time"),
    };
    Ok(SystemTime(PrimitiveDateTime::new(date, time_)))
}

/// FIXME: need to further check precision loss
/// convert core::time::Duration to time::Duration
const fn convert_to_time_duration(duration: Duration) -> time::Duration {
    let seconds = duration.as_secs() as i64;
    let nanoseconds = duration.subsec_nanos() as i32;
    time::Duration::new(seconds, nanoseconds)
}

/// FIXME: need to further check precision loss
/// convert time::Duration to core::time::Duration
const fn convert_to_core_duration(duration: time::Duration) -> Duration {
    let seconds = duration.whole_seconds() as u64;
    let nanoseconds = duration.subsec_nanoseconds() as u32;
    Duration::new(seconds, nanoseconds)
}
