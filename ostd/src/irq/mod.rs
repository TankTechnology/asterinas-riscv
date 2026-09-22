// SPDX-License-Identifier: MPL-2.0

//! Handling of Interrupt ReQuests (IRQs).
//!
//! # Top vs bottom half
//!
//! OSTD divides the handling of an IRQ into two parts:
//! **top half** and **bottom half**.
//!
//! A driver can assign to a target device an IRQ line, [`IrqLine`],
//! to which a callback function may be registered.
//! When an IRQ arrives at an IRQ line,
//! OSTD will invoke all the callbacks registered on the line,
//! with all local IRQs on the CPU disabled.
//! Thus, the `IrqLine` callbacks should be written as short as possible,
//! performing the most critical tasks.
//! This is the so-called top half of IRQ handling.
//!
//! When the top half finishes,
//! OSTD continues on the handling of the IRQ with the bottom half.
//! The logic of the bottom half is specified
//! by a callback function registered via [`register_bottom_half_handler_l1`]
//! (or [`register_bottom_half_handler_l2`], as we will see later).
//! The implementer of this callback function may re-enable local IRQs,
//! thus allowing the less critical tasks performed in the bottom half
//! to be preempted by the more critical ones done in the top half.
//!
//! OSTD's split of IRQ handling in top and bottom halves
//! closely resembles that of Linux,
//! but with a key difference:
//! OSTD itself does not hardcode any concrete mechanisms for the bottom-half,
//! e.g., Linux's softirqs or tasklets.
//! OSTD's APIs are flexible and powerful enough to
//! enable an OSTD-based kernel to implement such mechanisms itself.
//! This design helps contain the size and complexity of OSTD.
//!
//! # Nested interrupts
//!
//! OSTD allows interrupts to be nested.
//! The top-half for handling nested interrupts are still done by `IrqLine` callbacks,
//! yet the bottom-half logic is done by a new callback
//! registered via [`register_bottom_half_handler_l2`],
//! rather than [`register_bottom_half_handler_l1`].
//!
//! We introduce the concept of **interrupt level** to
//! mark the nesting depth of interrupts.
//! [`InterruptLevel::current`] keeps track of the current nesting depth
//! on the CPU where the code is executing.
//! There are three interrupt levels:
//!
//! - **Level 0 (Task Context):**
//!   Normal execution for a kernel or user task.
//!   Code at this level can be preempted by a hardware interrupt.
//! - **Level 1 (Interrupt Context):**
//!   Entered when an interrupt preempts task context code.
//!   Interrupt handling callbacks that may be invoked at this level are:
//!   - The top-half callbacks registered via [`IrqLine`];
//!   - The bottom-half callback registered via [`register_bottom_half_handler_l1`].
//! - **Level 2 (Nested Interrupt Context):**
//!   The maximum nesting level,
//!   entered when a level 1 bottom-half callback
//!   (registered via `register_bottom_half_handler_l1`) is interrupted.
//!   `IrqLine` callbacks always have IRQ disabled;
//!   thus, they can never be preempted.
//!
//!   Interrupt handling callbacks that may be invoked at this level are:
//!   - The top-half callbacks registered via [`IrqLine`];
//!   - The bottom-half callback registered via [`register_bottom_half_handler_l2`]
//!     (not [`register_bottom_half_handler_l1`]).
//!
//!   At this level, all local IRQs are disabled to prevent further nesting.
//!

mod bottom_half;
mod guard;
mod level;
mod top_half;

pub use bottom_half::{register_bottom_half_handler_l1, register_bottom_half_handler_l2};
pub use guard::{DisabledLocalIrqGuard, disable_local};
pub use level::InterruptLevel;
pub(crate) use top_half::PhasedCallbackSnapshot;
#[cfg(target_arch = "riscv64")]
pub(crate) use top_half::snapshot_phased_callback;
pub use top_half::{IrqCallbackFunction, IrqLine};

use crate::{
    arch::{irq::HwIrqLine, trap::TrapFrame},
    cpu::{CpuId, PrivilegeLevel},
    util::id_set::Id,
};

/// The interrupt level cell exactly as stored, without decoding it.
pub fn level_raw_for_diagnosis() -> u8 {
    level::raw_for_diagnosis()
}

/// Whether local interrupts are enabled on this CPU right now.
pub fn is_local_enabled_for_diagnosis() -> bool {
    crate::arch::irq::is_local_enabled()
}

/// Puts the interrupt level back to the task context.
///
/// Returns the value it held, so the caller can report a level that was
/// abandoned by the stack that raised it rather than undone by it.
pub fn take_level_for_switch() -> u8 {
    level::take_for_switch()
}

/// Records the most recent interrupt that was taken while the CPU was already
/// handling one, together with the level it was taken at.
///
/// The encoding admits two levels only, so any interrupt that arrives at level
/// one is the one that spends the last of it. Naming it at the moment it
/// happens is what separates "a callback re-enabled interrupts" from "a level
/// was never given back", and neither can be read off the state left behind.
static LAST_NESTED_IRQ: core::sync::atomic::AtomicU64 = core::sync::atomic::AtomicU64::new(0);
static NESTED_IRQ_COUNT: core::sync::atomic::AtomicUsize =
    core::sync::atomic::AtomicUsize::new(0);

/// The last interrupt taken while already handling one, if any.
///
/// Packed as `cpu << 32 | irq_num << 16 | raw_level_before`.
pub fn last_nested_irq_for_diagnosis() -> u64 {
    LAST_NESTED_IRQ.load(core::sync::atomic::Ordering::Relaxed)
}

/// How many interrupts have been taken while the CPU was already handling one.
pub fn nested_irq_count_for_diagnosis() -> usize {
    NESTED_IRQ_COUNT.load(core::sync::atomic::Ordering::Relaxed)
}

pub(crate) fn call_irq_callback_functions(
    trap_frame: &TrapFrame,
    hw_irq_line: &HwIrqLine,
    cpu_priv_at_irq: PrivilegeLevel,
) {
    // Name the interrupt that would push the level to three. The encoding
    // models at most two levels, and the value observed at the panic is
    // always exactly three, so the third one is not an overflow but a path
    // that re-enables local IRQs while already at level two.
    if InterruptLevel::current().as_u8() >= 2 {
        crate::warn!(
            "IRQ {} entered while already at level 2, which the encoding cannot represent",
            hw_irq_line.irq_num(),
        );
    }
    // Recorded undecoded and via `early_println!`: this is the report that has
    // to survive the failure it describes. Printing on every entry is far too
    // loud to be usable --- this path runs hundreds of thousands of times a
    // minute --- so the loud report is saved for the one event that matters:
    // an interrupt that gives its level back wrong, or does not give it back.
    let raw_level = level::raw_for_diagnosis();
    if raw_level >> 1 != 0 {
        NESTED_IRQ_COUNT.fetch_add(1, core::sync::atomic::Ordering::Relaxed);
        LAST_NESTED_IRQ.store(
            (crate::cpu::CpuId::current_racy().as_usize() as u64) << 32
                | (hw_irq_line.irq_num() as u64) << 16
                | raw_level as u64,
            core::sync::atomic::Ordering::Relaxed,
        );
    }
    let cpu = crate::cpu::CpuId::current_racy().as_usize();
    let irq_num = hw_irq_line.irq_num();
    level::enter(
        move || {
            top_half::process(trap_frame, hw_irq_line);
            bottom_half::process(irq_num);
        },
        cpu_priv_at_irq,
    );
    // The level is per-CPU and is only ever undoed by the same stack that set
    // it, so a handler that ends somewhere other than where it started leaves
    // the value one increment high for good. That is the state every later
    // report of this defect has shown, and this is the moment it is created.
    let raw_after = level::raw_for_diagnosis();
    if raw_after != raw_level {
        crate::early_println!(
            "IRQ_LEVEL_IMBALANCE cpu={} irq={} raw_before={:#010b} raw_after={:#010b}",
            cpu,
            irq_num,
            raw_level,
            raw_after,
        );
    }
}
