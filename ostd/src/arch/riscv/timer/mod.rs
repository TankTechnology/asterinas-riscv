// SPDX-License-Identifier: MPL-2.0

//! The timer support.

use core::{
    arch::asm,
    cell::Cell,
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};

use spin::Once;

use crate::{
    arch::{self, boot::DEVICE_TREE, cpu::extension::IsaExtensions, trap::TrapFrame},
    cpu_local,
    irq::{self, IrqLine},
    timer::TIMER_FREQ,
};

pub(super) static TIMER_IRQ: Once<IrqLine> = Once::new();

static TIMEBASE_FREQ: AtomicU64 = AtomicU64::new(0);
static TIMER_INTERVAL: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy)]
struct TimerDeadlines {
    next_periodic: u64,
    requested: Option<u64>,
}

cpu_local! {
    static TIMER_DEADLINES: Cell<TimerDeadlines> = Cell::new(TimerDeadlines {
        next_periodic: 0,
        requested: None,
    });
}

/// Initializes the timer module on the BSP.
///
/// # Safety
///
/// This function is safe to call on the following conditions:
///  1. It is called once and at most once at a proper timing in the boot context
///     of the BSP.
///  2. It is called before any other public functions of this module is called.
pub(super) unsafe fn init_on_bsp() {
    TIMEBASE_FREQ.store(
        DEVICE_TREE
            .get()
            .unwrap()
            .cpus()
            .next()
            .unwrap()
            .timebase_frequency() as u64,
        Ordering::Relaxed,
    );
    TIMER_INTERVAL.store(
        (TIMEBASE_FREQ.load(Ordering::Relaxed) / TIMER_FREQ).max(1),
        Ordering::Relaxed,
    );
    if is_sstc_enabled() {
        // SAFETY: Mutating the static variable `SET_TIMER_FN` is safe here
        // because we ensure that it is only modified during the initialization
        // phase of the timer.
        unsafe {
            SET_TIMER_FN = set_timer_sstc;
        }
    }

    TIMER_IRQ.call_once(|| {
        let mut timer_irq = IrqLine::alloc().unwrap();
        timer_irq.on_active(timer_callback);

        timer_irq
    });

    // SAFETY: The caller ensures that this is only called once on the
    // bootstrapping hart.
    unsafe { init_current_hart() };
}

/// Initializes the timer on this AP.
///
/// # Safety
///
/// This function must be called on an AP that hasn't called this function.
pub(super) unsafe fn init_on_ap() {
    // SAFETY: The caller ensures that this is only called once on the
    // current application hart.
    unsafe { init_current_hart() };
}

/// Initializes the timer on the current hart.
///
/// # Safety
///
/// This function must be called on a hart that hasn't called this function.
unsafe fn init_current_hart() {
    let now = riscv::register::time::read64();
    let interval = TIMER_INTERVAL.load(Ordering::Relaxed);
    let next_periodic = now.saturating_add(interval);
    let irq_guard = irq::disable_local();
    TIMER_DEADLINES.get_with(&irq_guard).set(TimerDeadlines {
        next_periodic,
        requested: None,
    });
    program_timer_at(next_periodic);
    drop(irq_guard);

    // SAFETY: Accessing the `sie` CSR to enable the timer interrupt is safe
    // here because this function is only called during timer initialization,
    // and we ensure that only the timer interrupt bit is set without affecting
    // other interrupt sources.
    unsafe {
        riscv::register::sie::set_stimer();
    }
}

fn timer_callback(trapframe: &TrapFrame) {
    let now = riscv::register::time::read64();
    let (periodic_due, requested_due) = update_expired_deadlines(now);

    if periodic_due {
        crate::timer::call_timer_callback_functions(trapframe);
    }
    if requested_due {
        crate::timer::call_high_resolution_callback_functions();
    }

    program_next_timer();
}

pub(super) fn request_interrupt_after(duration: Duration) {
    let now = riscv::register::time::read64();
    let ticks = duration_to_ticks_ceil(duration, get_timebase_freq());
    let requested = now.saturating_add(ticks);
    let irq_guard = irq::disable_local();
    let deadlines = TIMER_DEADLINES.get_with(&irq_guard);
    let mut value = deadlines.get();

    if value.next_periodic == 0 {
        return;
    }

    value.requested = Some(value.requested.map_or(requested, |old| old.min(requested)));
    deadlines.set(value);
    program_timer_at(earliest_deadline(value));
}

fn update_expired_deadlines(now: u64) -> (bool, bool) {
    let irq_guard = irq::disable_local();
    let deadlines = TIMER_DEADLINES.get_with(&irq_guard);
    let mut value = deadlines.get();
    let periodic_due = now >= value.next_periodic;
    let requested_due = value.requested.is_some_and(|deadline| now >= deadline);

    if periodic_due {
        value.next_periodic = advance_periodic_deadline(
            value.next_periodic,
            now,
            TIMER_INTERVAL.load(Ordering::Relaxed),
        );
    }
    if requested_due {
        value.requested = None;
    }
    deadlines.set(value);

    (periodic_due, requested_due)
}

fn program_next_timer() {
    let irq_guard = irq::disable_local();
    let deadlines = TIMER_DEADLINES.get_with(&irq_guard).get();
    program_timer_at(earliest_deadline(deadlines));
}

fn earliest_deadline(deadlines: TimerDeadlines) -> u64 {
    deadlines
        .requested
        .map_or(deadlines.next_periodic, |requested| {
            requested.min(deadlines.next_periodic)
        })
}

fn duration_to_ticks_ceil(duration: Duration, frequency: u64) -> u64 {
    let ticks = duration
        .as_nanos()
        .saturating_mul(u128::from(frequency))
        .div_ceil(1_000_000_000);

    u64::try_from(ticks).unwrap_or(u64::MAX).max(1)
}

fn advance_periodic_deadline(deadline: u64, now: u64, interval: u64) -> u64 {
    if now < deadline {
        return deadline;
    }

    let elapsed_intervals = now
        .saturating_sub(deadline)
        .checked_div(interval)
        .unwrap_or(0)
        .saturating_add(1);
    deadline.saturating_add(interval.saturating_mul(elapsed_intervals))
}

fn program_timer_at(deadline: u64) {
    // SAFETY: Calling the `SET_TIMER_FN` function pointer is safe here
    // because we ensure that it is set to a valid function during the timer
    // initialization, and we never modify it after that.
    unsafe {
        SET_TIMER_FN(deadline);
    }
}

static mut SET_TIMER_FN: fn(u64) = set_timer_sbi;

fn set_timer_sbi(deadline: u64) {
    sbi_rt::set_timer(deadline);
}

fn set_timer_sstc(deadline: u64) {
    // SAFETY: Setting a timer deadline through `stimecmp` is the standard
    // operation specified by the RISC-V SSTC extension.
    unsafe {
        asm!("csrrw {}, stimecmp, {}", out(reg) _, in(reg) deadline);
    }
}

fn is_sstc_enabled() -> bool {
    arch::cpu::extension::has_extensions(IsaExtensions::SSTC)
}

pub(crate) fn get_timebase_freq() -> u64 {
    TIMEBASE_FREQ.load(Ordering::Relaxed)
}

#[cfg(ktest)]
mod tests {
    use core::time::Duration;

    use super::{
        TimerDeadlines, advance_periodic_deadline, duration_to_ticks_ceil, earliest_deadline,
    };
    use crate::prelude::ktest;

    #[ktest]
    fn rounds_relative_deadlines_up_to_timer_ticks() {
        assert_eq!(duration_to_ticks_ceil(Duration::ZERO, 10_000_000), 1);
        assert_eq!(
            duration_to_ticks_ceil(Duration::from_nanos(1), 10_000_000),
            1
        );
        assert_eq!(
            duration_to_ticks_ceil(Duration::from_nanos(101), 10_000_000),
            2
        );
        assert_eq!(
            duration_to_ticks_ceil(Duration::from_millis(1), 10_000_000),
            10_000
        );
    }

    #[ktest]
    fn selects_the_earliest_hardware_deadline() {
        assert_eq!(
            earliest_deadline(TimerDeadlines {
                next_periodic: 20,
                requested: Some(10),
            }),
            10
        );
        assert_eq!(
            earliest_deadline(TimerDeadlines {
                next_periodic: 20,
                requested: Some(30),
            }),
            20
        );
    }

    #[ktest]
    fn advances_periodic_deadline_past_the_current_time() {
        assert_eq!(advance_periodic_deadline(100, 99, 10), 100);
        assert_eq!(advance_periodic_deadline(100, 100, 10), 110);
        assert_eq!(advance_periodic_deadline(100, 135, 10), 140);
    }
}
