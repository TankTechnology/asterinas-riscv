// SPDX-License-Identifier: MPL-2.0

//! `sched` events: context switches and wakeups.

use crate::trace::event::TraceEvent;

/// The subsystem name as it appears under `events/`.
pub const NAME: &str = "sched";

/// Emitted just before a task is switched to.
///
/// The record's own pid/tid are the task being switched *away from* (it is
/// still the current task at that point); the payload names the one that runs
/// next.
pub static SCHED_SWITCH: TraceEvent = TraceEvent::new(
    4,
    NAME,
    "sched_switch",
    "field:next_pid int; field:next_tid int",
);

/// Emitted when a sleeping task is made runnable (not when it is spawned).
pub static SCHED_WAKEUP: TraceEvent =
    TraceEvent::new(5, NAME, "sched_wakeup", "field:pid int; field:tid int");
