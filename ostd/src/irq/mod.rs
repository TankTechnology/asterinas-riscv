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

use core::sync::atomic::{AtomicU32, AtomicU64, AtomicUsize, Ordering};

pub use bottom_half::{register_bottom_half_handler_l1, register_bottom_half_handler_l2};
pub use guard::{DisabledLocalIrqGuard, disable_local};

/// The longest interval local IRQs were disabled on each CPU, and where.
///
/// This is the universal form of the failure being chased: while local IRQs
/// are off the CPU takes no traps at all, so a TLB flush from any other CPU
/// waits for however long the window lasts. Measuring at the guard catches
/// the long section wherever it is -- the console, the scheduler, a lock --
/// instead of depending on a guess about the site.
const MAX_IRQ_CPUS: usize = 64;
static IRQ_OFF_MAX_US: [AtomicU64; MAX_IRQ_CPUS] = [const { AtomicU64::new(0) }; MAX_IRQ_CPUS];
static IRQ_OFF_SITE: [AtomicUsize; MAX_IRQ_CPUS] = [const { AtomicUsize::new(0) }; MAX_IRQ_CPUS];

/// When each CPU entered its current IRQ-off window (timebase ticks; 0 = not
/// in one). A window that never ends never reaches the "longest" statistic at
/// exit, so the current state has to be readable on its own.
static IRQ_OFF_START: [AtomicU64; MAX_IRQ_CPUS] = [const { AtomicU64::new(0) }; MAX_IRQ_CPUS];
/// IRQ-off windows completed in the current heartbeat period, per CPU.
static IRQ_OFF_DONE: [AtomicU32; MAX_IRQ_CPUS] = [const { AtomicU32::new(0) }; MAX_IRQ_CPUS];

/// Where each CPU entered the IRQ-off window it is currently in.
static IRQ_OFF_SITE_CUR: [AtomicUsize; MAX_IRQ_CPUS] =
    [const { AtomicUsize::new(0) }; MAX_IRQ_CPUS];

/// Records that the current CPU entered an IRQ-off window.
pub(crate) fn note_irq_off_enter(ticks: u64, site: &'static core::panic::Location<'static>) {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_IRQ_CPUS {
        IRQ_OFF_SITE_CUR[cpu].store(site as *const _ as usize, Ordering::Relaxed);
        IRQ_OFF_START[cpu].store(ticks, Ordering::Relaxed);
    }
}

/// Records that the current CPU left its IRQ-off window.
pub(crate) fn note_irq_off_exit() {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_IRQ_CPUS {
        // Idempotent: `halt_cpu` forgets its guard and re-enables IRQs through
        // the arch path, so the exit can be recorded twice for one window.
        // Without this, an idle CPU would look like it had been inside an
        // IRQ-off section since it last halted.
        if IRQ_OFF_START[cpu].swap(0, Ordering::Relaxed) != 0 {
            IRQ_OFF_SITE_CUR[cpu].store(0, Ordering::Relaxed);
            IRQ_OFF_DONE[cpu].fetch_add(1, Ordering::Relaxed);
        }
    }
}

/// Returns the site of `cpu`'s current IRQ-off window, if it is in one.
pub fn current_irq_off_site(cpu: usize) -> Option<(&'static str, u32)> {
    let site = IRQ_OFF_SITE_CUR.get(cpu)?.load(Ordering::Relaxed);
    irq_off_site_name(site)
}

/// How long `cpu` has been inside its current IRQ-off window, in microseconds.
/// Returns `None` when the CPU is not in one.
pub fn current_irq_off_us(cpu: usize) -> Option<u64> {
    if cpu >= MAX_IRQ_CPUS {
        return None;
    }
    let start = IRQ_OFF_START[cpu].load(Ordering::Relaxed);
    if start == 0 {
        return None;
    }
    let now = crate::arch::read_tsc();
    let frequency = crate::arch::tsc_freq().max(1);
    Some(now.saturating_sub(start).saturating_mul(1_000_000) / frequency)
}

/// Returns and resets the number of IRQ-off windows completed on every CPU
/// in the period.
pub fn take_irq_off_done() -> u32 {
    let mut total = 0u32;
    for cell in IRQ_OFF_DONE.iter() {
        total = total.saturating_add(cell.swap(0, Ordering::Relaxed));
    }
    total
}

pub(crate) fn note_irq_off(ticks: u64, site: &'static core::panic::Location<'static>) {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu >= MAX_IRQ_CPUS {
        return;
    }
    let frequency = crate::arch::tsc_freq().max(1);
    let us = ticks.saturating_mul(1_000_000) / frequency;
    let previous = IRQ_OFF_MAX_US[cpu].fetch_max(us, Ordering::Relaxed);
    if us > previous {
        IRQ_OFF_SITE[cpu].store(site as *const _ as usize, Ordering::Relaxed);
    }
}

/// Returns `(max_us, site_address)` for `cpu` and resets the maximum.
pub fn take_irq_off_max(cpu: usize) -> (u64, usize) {
    if cpu >= MAX_IRQ_CPUS {
        return (0, 0);
    }
    (
        IRQ_OFF_MAX_US[cpu].swap(0, Ordering::Relaxed),
        IRQ_OFF_SITE[cpu].swap(0, Ordering::Relaxed),
    )
}

/// Resolves a site address returned by [`take_irq_off_max`] to file and line.
///
/// Safe by construction: the only values ever stored are `&'static Location`
/// references recorded by `note_irq_off` in this module, and zero.
pub fn irq_off_site_name(site: usize) -> Option<(&'static str, u32)> {
    if site == 0 {
        return None;
    }
    // SAFETY: the address comes from `IRQ_OFF_SITE`, which only stores
    // `&'static Location` values written by `note_irq_off`.
    let location = unsafe { &*(site as *const core::panic::Location<'static>) };
    Some((location.file(), location.line()))
}

pub use level::InterruptLevel;
use spin::Once;
pub(crate) use top_half::PhasedCallbackSnapshot;
#[cfg(target_arch = "riscv64")]
pub(crate) use top_half::snapshot_phased_callback;
pub use top_half::{IrqCallbackFunction, IrqLine};

use crate::{
    arch::{irq::HwIrqLine, trap::TrapFrame},
    cpu::PrivilegeLevel,
};

/// Number of IRQ lines the counter table covers.
///
/// It matches the RISC-V controller's line range; other architectures simply
/// use the first entries.
pub const IRQ_LINE_COUNT: usize = crate::arch::irq::IRQ_NUM_MAX as usize + 1;

/// How many times each IRQ line has been delivered, system-wide.
///
/// `/proc/interrupts` reports these. They are per line rather than per CPU:
/// the board has four harts and an interrupt is claimed by one of them, but
/// none of the tools this kernel targets (`htop`, `irqtop`, `sar -I`) depends
/// on the split, and a per-CPU table would need a CPU id in the dispatch path
/// that is not portable across the architectures OSTD supports.
static IRQ_COUNTS: [AtomicU64; IRQ_LINE_COUNT] = [const { AtomicU64::new(0) }; IRQ_LINE_COUNT];

/// Records one delivery of `irq_num`.
fn record_irq(irq_num: u8) {
    if let Some(counter) = IRQ_COUNTS.get(irq_num as usize) {
        counter.fetch_add(1, Ordering::Relaxed);
    }
}

/// Returns how many times `irq_num` has been delivered since boot.
pub fn irq_count(irq_num: u8) -> u64 {
    IRQ_COUNTS
        .get(irq_num as usize)
        .map(|counter| counter.load(Ordering::Relaxed))
        .unwrap_or(0)
}

/// Returns how many deliveries each IRQ line has seen since boot.
pub fn irq_counts() -> &'static [AtomicU64; IRQ_LINE_COUNT] {
    &IRQ_COUNTS
}

pub(crate) fn call_irq_callback_functions(
    trap_frame: &TrapFrame,
    hw_irq_line: &HwIrqLine,
    cpu_priv_at_irq: PrivilegeLevel,
) {
    record_irq(hw_irq_line.irq_num());
    call_irq_hook(hw_irq_line.irq_num(), true);
    level::enter(
        move || {
            top_half::process(trap_frame, hw_irq_line);
            bottom_half::process(hw_irq_line.irq_num());
        },
        cpu_priv_at_irq,
    );
    call_irq_hook(hw_irq_line.irq_num(), false);
}

/// A hook run on every timer interrupt, for sampling profilers.
///
/// It receives the interrupted program counter and whether the interrupted
/// context was user mode. It runs in interrupt context: no allocation, no
/// sleeping, and it must not touch the clocksource (the timer handler itself
/// may).
static TIMER_SAMPLE_HOOK: Once<fn(usize, bool)> = Once::new();

/// Injects the timer sampling hook.
pub fn inject_timer_sample_hook(hook: fn(usize, bool)) {
    TIMER_SAMPLE_HOOK.call_once(|| hook);
}

/// Runs the timer sampling hook, if one was injected.
pub fn call_timer_sample_hook(pc: usize, user: bool) {
    if let Some(hook) = TIMER_SAMPLE_HOOK.get() {
        hook(pc, user);
    }
}

/// A hook around every interrupt handler invocation, for tracing.
///
/// `entering` distinguishes the call before the handlers from the one after
/// them. It runs in interrupt context with interrupts disabled: no allocation,
/// no sleeping.
static IRQ_HOOK: Once<fn(u8, bool)> = Once::new();

/// Injects the hook run around interrupt handlers.
pub fn inject_irq_hook(hook: fn(u8, bool)) {
    IRQ_HOOK.call_once(|| hook);
}

fn call_irq_hook(irq_num: u8, entering: bool) {
    if let Some(hook) = IRQ_HOOK.get() {
        hook(irq_num, entering);
    }
}
