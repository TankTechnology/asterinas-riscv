// SPDX-License-Identifier: MPL-2.0

//! This module maintains preemption-related information for the current task
//! on a CPU with a single 32-bit, CPU-local integer value.
//!
//! * Bits from 0 to 30 represents an unsigned counter called `guard_count`,
//!   which is the number of `DisabledPreemptGuard` instances held by the
//!   current CPU;
//! * Bit 31 is set to `!need_preempt`, where `need_preempt` is a boolean value
//!   that will be set by the scheduler when it decides that the current task
//!   _needs_ to be preempted.
//!
//! Thus, the current task on a CPU _should_ be preempted if and only if this
//! integer is equal to zero.
//!
//! The initial value of this integer is equal to `1 << 31`.
//!
//! This module provides a set of functions to access and manipulate
//! `guard_count` and `need_preempt`.

use crate::cpu_local_cell;

/// Returns whether the current task _should_ be preempted or not.
///
/// `should_preempt() == need_preempt() && get_guard_count() == 0`.
pub(in crate::task) fn should_preempt() -> bool {
    PREEMPT_INFO.load() == 0
}

pub(in crate::task) fn need_preempt() -> bool {
    PREEMPT_INFO.load() & NEED_PREEMPT_MASK == 0
}

pub(in crate::task) fn set_need_preempt() {
    PREEMPT_INFO.bitand_assign(!NEED_PREEMPT_MASK);
}

pub(in crate::task) fn clear_need_preempt() {
    PREEMPT_INFO.bitor_assign(NEED_PREEMPT_MASK);
}

pub(in crate::task) fn get_guard_count() -> u32 {
    PREEMPT_INFO.load() & GUARD_COUNT_MASK
}

#[track_caller]
pub(in crate::task) fn inc_guard_count() {
    // Record only the guard that lifts the count off zero. That is the guard
    // which is still outstanding; recording every take would usually name the
    // most recent one, which has already been dropped by the time a count is
    // read somewhere it cannot be alive.
    if get_guard_count() == 0 {
        LAST_TAKER.store(Some(core::panic::Location::caller()));
        // Record who was running too. A guard that outlives its task and a
        // guard whose task identity was read wrong produce the same count, and
        // only the pointer distinguishes them.
        TAKER_TASK.store(
            crate::task::processor::current_task()
                .map(|task| task.as_ptr() as usize)
                .unwrap_or(0),
        );
    }
    PREEMPT_INFO.add_assign(1);
}

pub(in crate::task) fn dec_guard_count() {
    debug_assert!(get_guard_count() > 0);
    PREEMPT_INFO.sub_assign(1);
}

/// The task that was current when the outstanding guard was taken.
pub(in crate::task) fn outstanding_taker_task() -> usize {
    TAKER_TASK.load()
}

/// Where the guard that is currently outstanding was taken.
pub(in crate::task) fn outstanding_taker() -> Option<&'static core::panic::Location<'static>> {
    LAST_TAKER.load()
}

pub(in crate::task) fn get_guard_count_for_diagnosis() -> u32 {
    get_guard_count()
}

cpu_local_cell! {
    static PREEMPT_INFO: u32 = NEED_PREEMPT_MASK;
    static LAST_TAKER: Option<&'static core::panic::Location<'static>> = None;
    static TAKER_TASK: usize = 0;
}

const NEED_PREEMPT_MASK: u32 = 1 << 31;
const GUARD_COUNT_MASK: u32 = (1 << 31) - 1;
