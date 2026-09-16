// SPDX-License-Identifier: MPL-2.0

use core::time::Duration;

use spin::Once;

use crate::{
    fs::pseudofs::{NsCommonOps, NsType, StashedDentry},
    prelude::*,
    process::UserNamespace,
};

#[derive(Clone, Copy, Debug, Default)]
struct TimeOffsets {
    monotonic_ns: i128,
    boottime_ns: i128,
}

/// A Linux-compatible time namespace.
pub struct TimeNamespace {
    offsets: SpinLock<TimeOffsets>,
    owner: Arc<UserNamespace>,
    stashed_dentry: StashedDentry,
}

impl TimeNamespace {
    /// Returns the initial namespace, whose clock offsets are zero.
    pub fn get_init_singleton() -> &'static Arc<Self> {
        static INIT: Once<Arc<TimeNamespace>> = Once::new();
        INIT.call_once(|| {
            Arc::new(Self {
                offsets: SpinLock::new(TimeOffsets::default()),
                owner: UserNamespace::get_init_singleton().clone(),
                stashed_dentry: StashedDentry::new(),
            })
        })
    }

    /// Creates a time namespace owned by `owner` with zero offsets.
    pub fn new_child(owner: Arc<UserNamespace>) -> Arc<Self> {
        Arc::new(Self {
            offsets: SpinLock::new(TimeOffsets::default()),
            owner,
            stashed_dentry: StashedDentry::new(),
        })
    }

    /// Sets the monotonic or boot-time offset in seconds and nanoseconds.
    pub fn set_offset(&self, clock_id: i32, sec: i64, nsec: i64) -> Result<()> {
        if !(0..1_000_000_000).contains(&nsec) {
            return_errno_with_message!(Errno::EINVAL, "time namespace offset is not normalized");
        }
        let offset = i128::from(sec)
            .checked_mul(1_000_000_000)
            .and_then(|value| value.checked_add(i128::from(nsec)))
            .ok_or_else(|| Error::with_message(Errno::ERANGE, "time namespace offset overflows"))?;
        let mut offsets = self.offsets.lock();
        match clock_id {
            1 => offsets.monotonic_ns = offset,
            7 => offsets.boottime_ns = offset,
            _ => return_errno_with_message!(
                Errno::EINVAL,
                "clock cannot be offset in a time namespace"
            ),
        }
        Ok(())
    }

    /// Returns `(monotonic_sec, monotonic_nsec, boottime_sec, boottime_nsec)`.
    pub fn offsets(&self) -> (i64, i64, i64, i64) {
        fn split(value: i128) -> (i64, i64) {
            (
                value.div_euclid(1_000_000_000) as i64,
                value.rem_euclid(1_000_000_000) as i64,
            )
        }
        let offsets = self.offsets.lock();
        let (monotonic_sec, monotonic_nsec) = split(offsets.monotonic_ns);
        let (boottime_sec, boottime_nsec) = split(offsets.boottime_ns);
        (monotonic_sec, monotonic_nsec, boottime_sec, boottime_nsec)
    }

    /// Applies the namespace's monotonic or boot-time offset to a clock value.
    pub fn apply_offset(&self, duration: Duration, boot_time: bool) -> Duration {
        let offset = self.offset_nanos(boot_time);
        let base =
            i128::from(duration.as_secs()) * 1_000_000_000 + i128::from(duration.subsec_nanos());
        let adjusted = base.saturating_add(offset).max(0);
        let secs = (adjusted / 1_000_000_000).min(i128::from(u64::MAX)) as u64;
        let nanos = (adjusted % 1_000_000_000) as u32;
        Duration::new(secs, nanos)
    }

    /// Returns the signed nanosecond offset for a namespaced clock.
    pub fn offset_nanos(&self, boot_time: bool) -> i128 {
        let offsets = self.offsets.lock();
        if boot_time {
            offsets.boottime_ns
        } else {
            offsets.monotonic_ns
        }
    }
}

#[cfg(ktest)]
mod tests {
    use core::time::Duration;

    use ostd::prelude::ktest;

    use super::TimeNamespace;
    use crate::process::UserNamespace;

    #[ktest]
    fn applies_signed_clock_offsets() {
        let ns = TimeNamespace::new_child(UserNamespace::get_init_singleton().clone());
        ns.set_offset(1, 10, 25).unwrap();
        ns.set_offset(7, -2, 500_000_000).unwrap();

        assert_eq!(
            ns.apply_offset(Duration::from_secs(3), false),
            Duration::new(13, 25)
        );
        assert_eq!(
            ns.apply_offset(Duration::from_secs(3), true),
            Duration::from_millis(1500)
        );
        assert_eq!(ns.offsets(), (10, 25, -2, 500_000_000));
    }
}

impl NsCommonOps for TimeNamespace {
    const TYPE: NsType = NsType::Time;

    fn owner_user_ns(&self) -> Option<&Arc<UserNamespace>> {
        Some(&self.owner)
    }

    fn parent(&self) -> Result<&Arc<Self>> {
        return_errno_with_message!(Errno::EINVAL, "a time namespace does not have a parent");
    }

    fn stashed_dentry(&self) -> &StashedDentry {
        &self.stashed_dentry
    }
}
