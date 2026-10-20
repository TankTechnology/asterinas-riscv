// SPDX-License-Identifier: MPL-2.0

pub(super) mod cpu_local;
mod guard;

pub use self::guard::{DisabledPreemptGuard, disable_preempt};

/// The number of preemption guards currently held on this CPU.
///
/// Exposed so the interrupt path can check that a guard it took is still the
/// only one outstanding after it has run code with local IRQs re-enabled.
pub(crate) fn guard_count_for_diagnosis() -> u32 {
    cpu_local::get_guard_count_for_diagnosis()
}

/// Halts the CPU until interrupts if no preemption is required.
///
/// This function will return if:
///  - preemption is required when calling this function,
///  - preemption is required during halting the CPU, or
///  - interrupts occur during halting the CPU.
///
/// This function will perform preemption before returning if
/// preemption is required.
///
/// # Panics
///
/// This function will panic if it is called in the atomic mode
/// ([`crate::task::atomic_mode`]).
#[track_caller]
pub fn halt_cpu() {
    crate::task::atomic_mode::might_sleep();

    let irq_guard = crate::irq::disable_local();

    if cpu_local::need_preempt() {
        drop(irq_guard);
    } else {
        core::mem::forget(irq_guard);
        // IRQs were previously enabled (checked by `might_sleep`). So we can re-enable them now.
        crate::arch::irq::enable_local_and_halt();
    }

    super::scheduler::might_preempt();
}
