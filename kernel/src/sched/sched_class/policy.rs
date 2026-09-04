// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::{AtomicU8, Ordering::Relaxed};

use atomic_integer_wrapper::define_atomic_version_of_integer_like_type;
use int_to_c_enum::TryFromInt;
use ostd::sync::SpinLock;

pub use super::real_time::{RealTimePolicy, RealTimePriority};
use crate::sched::nice::Nice;

/// The User-chosen scheduling policy.
///
/// The scheduling policies are specified by the user, usually through its priority.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SchedPolicy {
    #[expect(dead_code)]
    Stop,
    RealTime {
        rt_prio: RealTimePriority,
        rt_policy: RealTimePolicy,
    },
    /// A deadline reservation exposed through `sched_{set,get}attr`.
    ///
    /// Deadline tasks currently reuse the real-time run queue; the reservation
    /// values are retained exactly for Linux ABI compatibility.
    Deadline {
        runtime: u64,
        deadline: u64,
        period: u64,
    },
    Fair(Nice),
    Batch(Nice),
    Idle,
}

impl Default for SchedPolicy {
    fn default() -> Self {
        Self::Fair(Nice::default())
    }
}

/// The Linux scheduling policy code.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17.7/source/include/uapi/linux/sched.h#L112>.
#[repr(u32)]
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, TryFromInt)]
pub enum LinuxSchedPolicy {
    Normal = 0,
    Fifo = 1,
    RoundRobin = 2,
    Batch = 3,
    Iso = 4, // Reserved but not implemented yet on Linux.
    Idle = 5,
    Deadline = 6, // Not supported.
    Ext = 7,      // Not supported.
}

/// Projects internal scheduling policies onto Linux's user-visible policy codes.
///
/// This projection is many-to-one: `Stop` is reported as `Fifo`, and the
/// kernel-only `Idle` policy is reported as `Normal`.
impl From<SchedPolicy> for LinuxSchedPolicy {
    fn from(policy: SchedPolicy) -> Self {
        match policy {
            SchedPolicy::Stop => LinuxSchedPolicy::Fifo,
            SchedPolicy::RealTime {
                rt_policy: RealTimePolicy::Fifo,
                ..
            } => LinuxSchedPolicy::Fifo,
            SchedPolicy::RealTime {
                rt_policy: RealTimePolicy::RoundRobin { .. },
                ..
            } => LinuxSchedPolicy::RoundRobin,
            SchedPolicy::Deadline { .. } => LinuxSchedPolicy::Deadline,
            SchedPolicy::Fair(_) | SchedPolicy::Idle => LinuxSchedPolicy::Normal,
            SchedPolicy::Batch(_) => LinuxSchedPolicy::Batch,
        }
    }
}

#[repr(u8)]
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, TryFromInt)]
pub(super) enum SchedPolicyKind {
    Stop = 0,
    RealTime = 1,
    Fair = 2,
    Idle = 3,
}

impl SchedPolicy {
    pub(super) fn kind(&self) -> SchedPolicyKind {
        match self {
            SchedPolicy::Stop => SchedPolicyKind::Stop,
            SchedPolicy::RealTime { .. } => SchedPolicyKind::RealTime,
            SchedPolicy::Deadline { .. } => SchedPolicyKind::RealTime,
            SchedPolicy::Fair(_) | SchedPolicy::Batch(_) => SchedPolicyKind::Fair,
            SchedPolicy::Idle => SchedPolicyKind::Idle,
        }
    }

    /// Returns whether this policy has a higher runnable priority than another.
    pub(super) fn outranks(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Stop, Self::Stop) => false,
            (Self::Stop, _) => true,
            (_, Self::Stop) => false,
            (
                Self::RealTime {
                    rt_prio: this_prio, ..
                },
                Self::RealTime {
                    rt_prio: other_prio,
                    ..
                },
            ) => this_prio < other_prio,
            (Self::RealTime { .. }, _) => true,
            (_, Self::RealTime { .. }) => false,
            (
                Self::Deadline {
                    deadline: this_deadline,
                    ..
                },
                Self::Deadline {
                    deadline: other_deadline,
                    ..
                },
            ) => this_deadline < other_deadline,
            (Self::Deadline { .. }, _) => true,
            (_, Self::Deadline { .. }) => false,
            (
                Self::Fair(this_nice) | Self::Batch(this_nice),
                Self::Fair(other_nice) | Self::Batch(other_nice),
            ) => this_nice < other_nice,
            (Self::Fair(_) | Self::Batch(_), Self::Idle) => true,
            (Self::Idle, _) => false,
        }
    }
}

define_atomic_version_of_integer_like_type!(SchedPolicyKind, try_from = true, {
    #[derive(Debug)]
    pub struct AtomicSchedPolicyKind(AtomicU8);
});

impl From<SchedPolicyKind> for u8 {
    fn from(value: SchedPolicyKind) -> Self {
        value as _
    }
}

#[derive(Debug)]
pub(super) struct SchedPolicyState {
    kind: AtomicSchedPolicyKind,
    policy: SpinLock<SchedPolicy>,
}

impl SchedPolicyState {
    pub fn new(policy: SchedPolicy) -> Self {
        Self {
            kind: AtomicSchedPolicyKind::new(policy.kind()),
            policy: SpinLock::new(policy),
        }
    }

    pub fn kind(&self) -> SchedPolicyKind {
        self.kind.load(Relaxed)
    }

    pub fn get(&self) -> SchedPolicy {
        *self.policy.disable_irq().lock()
    }

    pub fn set(&self, mut policy: SchedPolicy, update: impl FnOnce(SchedPolicy)) {
        let mut this = self.policy.disable_irq().lock();

        // Keep the old base slice factor if the new policy doesn't specify one.
        if let (
            SchedPolicy::RealTime {
                rt_policy:
                    RealTimePolicy::RoundRobin {
                        base_slice_factor: slot,
                    },
                ..
            },
            SchedPolicy::RealTime {
                rt_policy: RealTimePolicy::RoundRobin { base_slice_factor },
                ..
            },
        ) = (*this, &mut policy)
        {
            *base_slice_factor = slot.or(*base_slice_factor);
        }

        update(policy);
        self.kind.store(policy.kind(), Relaxed);
        *this = policy;
    }
}
