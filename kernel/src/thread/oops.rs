// SPDX-License-Identifier: MPL-2.0

//! Kernel "oops" handling.
//!
//! In Asterinas, a Rust panic leads to a kernel "oops". A kernel oops behaves
//! as an exceptional control flow event. If kernel oopses happened too many
//! times, the kernel panics and the system gets halted. Kernel oops are per-
//! thread, so one thread's oops does not affect other threads.
//!
//! Though we can recover from the Rust panics. It is generally not recommended
//! to make Rust panics as a general exception handling mechanism. Handling
//! exceptions with [`Result`] is more idiomatic.

use alloc::format;
use core::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use ostd::{cpu::PinCurrentCpu, panic, task::disable_preempt, util::id_set::Id};

use super::Thread;
use crate::prelude::*;

// TODO: Control the kernel commandline parsing from the kernel crate.
// In Linux it can be dynamically changed by writing to
// `/proc/sys/kernel/panic`.
static PANIC_ON_OOPS: AtomicBool = AtomicBool::new(true);

/// The kernel "oops" information.
pub struct OopsInfo {
    /// The "oops" message.
    pub message: String,
    /// The thread where the "oops" happened.
    #[expect(unused)]
    pub thread: Arc<Thread>,
}

/// Executes the given function and catches any panics that occur.
///
/// All the panics in the given function will be regarded as oops. If a oops
/// happens, this function returns `None`. Otherwise, it returns the return
/// value of the given function.
///
/// If the kernel is configured to panic on oops, this function will not return
/// when a oops happens.
pub fn catch_panics_as_oops<F, R>(f: F) -> Result<R, OopsInfo>
where
    F: FnOnce() -> R,
{
    let result = panic::catch_unwind(f);

    match result {
        Ok(result) => Ok(result),
        Err(err) => {
            let info = err.downcast::<OopsInfo>().unwrap();

            ostd::error!("Oops! {}", info.message);

            let count = OOPS_COUNT.fetch_add(1, Ordering::Relaxed);
            if count >= MAX_OOPS_COUNT {
                // Too many oops. Abort the kernel.
                ostd::error!("Too many oops. The kernel panics.");
                panic::abort();
            }

            Err(*info)
        }
    }
}

/// The maximum number of oops allowed before the kernel panics.
///
/// It is the same as Linux's default value.
const MAX_OOPS_COUNT: usize = 10_000;

static OOPS_COUNT: AtomicUsize = AtomicUsize::new(0);

/// How many times the panic handler has run on this kernel.
static PANIC_HANDLER_ENTRIES: AtomicUsize = AtomicUsize::new(0);

#[ostd::panic_handler]
fn panic_handler(info: &core::panic::PanicInfo) -> ! {
    let message = info.message();

    // Written to the serial port directly, and before anything else in the
    // handler can fail. A guard taken further down this function is the one the
    // atomic-mode check reports as outstanding when a later panic names it, so
    // the ordinal says whether the panic being printed is the first event or a
    // consequence of an earlier one that never got its report out.
    let entry = PANIC_HANDLER_ENTRIES.fetch_add(1, Ordering::Relaxed);
    ostd::early_println!(
        "PANIC_ENTRY seq={} cpu={} raw_level={:#010b} irq_enabled={} unwind={} nested_irqs={} last_nested={:#018x}",
        entry,
        ostd::cpu::CpuId::current_racy().as_usize(),
        ostd::irq::level_raw_for_diagnosis(),
        ostd::irq::is_local_enabled_for_diagnosis(),
        info.can_unwind(),
        ostd::irq::nested_irq_count_for_diagnosis(),
        ostd::irq::last_nested_irq_for_diagnosis(),
    );

    if let Some(thread) = Thread::current() {
        let panic_on_oops = PANIC_ON_OOPS.load(Ordering::Relaxed);
        if !panic_on_oops && info.can_unwind() {
            // TODO: eliminate the need for heap allocation.
            let message = if let Some(location) = info.location() {
                format!("{} at {}:{}", message, location.file(), location.line())
            } else {
                message.to_string()
            };
            // Raise the panic and expect it to be caught.
            panic::begin_panic(Box::new(OopsInfo { message, thread }));
        }
    }

    #[cfg(target_arch = "riscv64")]
    if crate::boot_reboot::is_armed() {
        panic::abort();
    }

    let preempt_guard = disable_preempt();
    let thread = Thread::current();
    let cpu = preempt_guard.current_cpu();

    // Halt the system if the panic is not caught.
    if let Some(location) = info.location() {
        ostd::error!(
            "Uncaught panic:\n\t{}\n\tat {}:{}\n\ton CPU {} by thread {:?}",
            message,
            location.file(),
            location.line(),
            cpu.as_usize(),
            thread,
        );
    } else {
        ostd::error!(
            "Uncaught panic:\n\t{}\n\ton CPU {} by thread {:?}",
            message,
            cpu.as_usize(),
            thread,
        );
    }

    if info.can_unwind() {
        panic::print_stack_trace();
    } else {
        ostd::error!("Backtrace is disabled.");
    }

    panic::abort();
}
