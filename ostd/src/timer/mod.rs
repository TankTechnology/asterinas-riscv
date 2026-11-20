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
/// 250 Hz is enough for scheduler preemption and keeps the per-CPU periodic
/// interrupt cost low: at 1000 Hz an idle four-hart board took ~4,000 timer
/// interrupts and about a tenth of one core in system time every second.
/// Sub-tick wakeups still use the RISC-V one-shot deadline
/// (`request_interrupt_after`), so timer precision is unchanged.
pub const TIMER_FREQ: u64 = 250;

/// The effective tick frequency, settable with `asterinas.timer_hz=<n>`.
///
/// Each tick costs a machine-mode timer trap on this platform (no SSTC), so
/// the frequency is also a firmware-traffic knob: 25 Hz means a tenth of the
/// invisible machine-mode entries per CPU.
static TIMER_HZ: core::sync::atomic::AtomicU64 = core::sync::atomic::AtomicU64::new(TIMER_FREQ);

/// Returns the effective tick frequency in Hz.
pub fn timer_freq() -> u64 {
    TIMER_HZ.load(core::sync::atomic::Ordering::Relaxed)
}

/// Sets the tick frequency (clamped to 1..=1000).
pub fn set_timer_freq(hz: u64) {
    TIMER_HZ.store(hz.clamp(1, 1000), core::sync::atomic::Ordering::Relaxed);
}

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

/// Requests a timer interrupt after at most the given duration.
///
/// RISC-V uses the hardware timer as a one-shot deadline source. Other architectures retain
/// their periodic-tick behavior until they provide an equivalent one-shot implementation.
pub fn request_interrupt_after(duration: Duration) {
    #[cfg(target_arch = "riscv64")]
    crate::arch::request_timer_interrupt_after(duration);

    #[cfg(not(target_arch = "riscv64"))]
    let _ = duration;
}

pub(crate) fn call_timer_callback_functions(_: &TrapFrame) {
    let irq_guard = irq::disable_local();

    if irq_guard.current_cpu() == CpuId::bsp() {
        jiffies::ELAPSED.fetch_add(1, Ordering::Relaxed);
    }

    let callbacks_guard = INTERRUPT_CALLBACKS.get_with(&irq_guard);
    crate::smp::note_activity(crate::smp::ACT_TIMER_CB);
    for callback in callbacks_guard.borrow().iter() {
        (callback)();
    }
    let _ = &callbacks_guard;
    drop(callbacks_guard);
    crate::smp::note_activity(0);
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
