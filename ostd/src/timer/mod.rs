// SPDX-License-Identifier: MPL-2.0

//! The timer support.

mod jiffies;

use alloc::{boxed::Box, vec::Vec};
use core::{cell::RefCell, sync::atomic::Ordering, time::Duration};

pub use jiffies::Jiffies;

use crate::{
    arch::trap::TrapFrame,
    cpu::{CpuId, PinCurrentCpu},
    cpu_local, irq,
};

/// The timer frequency in Hz.
///
/// Here we choose 1000Hz since 1000Hz is easier for unit conversion and convenient for timer.
/// What's more, the frequency cannot be set too high or too low, 1000Hz is a modest choice.
///
/// For system performance reasons, this rate cannot be set too high, otherwise most of the time is
/// spent in executing timer code.
pub const TIMER_FREQ: u64 = 1000;

type InterruptCallback = Box<dyn Fn() + Sync + Send>;

cpu_local! {
    static INTERRUPT_CALLBACKS: RefCell<Vec<InterruptCallback>> = RefCell::new(Vec::new());
}

#[cfg(target_arch = "riscv64")]
cpu_local! {
    static HIGH_RESOLUTION_CALLBACKS: RefCell<Vec<InterruptCallback>> = RefCell::new(Vec::new());
}

/// Registers a function that will be executed during the timer interrupt on the current CPU.
pub fn register_callback_on_cpu<F>(func: F)
where
    F: Fn() + Sync + Send + 'static,
{
    let irq_guard = irq::disable_local();
    INTERRUPT_CALLBACKS
        .get_with(&irq_guard)
        .borrow_mut()
        .push(Box::new(func));
}

/// Registers a function that will run when a one-shot timer expires on the current RISC-V CPU.
///
/// Unlike [`register_callback_on_cpu`], the callback also runs for one-shot interrupts that
/// happen before the next periodic timer tick.
#[cfg(target_arch = "riscv64")]
pub fn register_high_resolution_callback_on_cpu<F>(func: F)
where
    F: Fn() + Sync + Send + 'static,
{
    let irq_guard = irq::disable_local();
    HIGH_RESOLUTION_CALLBACKS
        .get_with(&irq_guard)
        .borrow_mut()
        .push(Box::new(func));
}

/// Requests a timer interrupt after at most the given duration on the current CPU.
///
/// The timer manager is architecture-independent and always reports its earliest deadline
/// through this hook. The architecture backend may program a one-shot hardware deadline or keep
/// using its periodic tick until an equivalent deadline source is available. Keeping that choice
/// below this interface is important: POSIX timers and sleep/wakeup code must not grow
/// architecture-specific branches.
pub fn request_interrupt_after(duration: Duration) {
    crate::arch::request_timer_interrupt_after(duration);
}

pub(crate) fn call_timer_callback_functions(_: &TrapFrame) {
    let irq_guard = irq::disable_local();

    if irq_guard.current_cpu() == CpuId::bsp() {
        jiffies::ELAPSED.fetch_add(1, Ordering::Relaxed);
    }

    let callbacks_guard = INTERRUPT_CALLBACKS.get_with(&irq_guard);
    for callback in callbacks_guard.borrow().iter() {
        (callback)();
    }
    drop(callbacks_guard);

    // A CPU that is sitting in its idle loop may not perform a task context
    // switch for a long time.  Timer interrupts are nevertheless a regular
    // quiescent point, so report one here to let SMP RCU/TLB reclamation make
    // progress while other CPUs create and destroy address spaces.
    //
    // SAFETY: Timer IRQ callbacks run outside an RCU read-side critical
    // section. The local IRQ guard remains held, which is stronger than the
    // preemption/IRQ exclusion required by the RCU monitor.
    unsafe { crate::sync::finish_grace_period() };
}

#[cfg(target_arch = "riscv64")]
pub(crate) fn call_high_resolution_callback_functions() {
    let irq_guard = irq::disable_local();
    let callbacks_guard = HIGH_RESOLUTION_CALLBACKS.get_with(&irq_guard);
    for callback in callbacks_guard.borrow().iter() {
        (callback)();
    }
    drop(callbacks_guard);
}
