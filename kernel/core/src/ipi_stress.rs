// SPDX-License-Identifier: MPL-2.0

//! Raise the machine-mode interrupt-send rate on purpose.
//!
//! Every inter-processor interrupt costs a trap into machine mode on the
//! sending hart. The board resets under a burst of page-table flushes, and
//! those flushes always bring both of their ingredients at once: work on the
//! page tables and machine-mode traffic. This switch separates them. It sends
//! bare interrupts at a chosen rate -- no callback is queued, so the target
//! harts take the interrupt, find nothing to do and return -- and nothing
//! touches a page table.
//!
//! `asterinas.ipi_stress=<per second>`, default 0 (off). It paces itself with
//! the time counter instead of a timer because a timer would add its own
//! machine-mode traffic (the SBI time call) to the thing being measured. The
//! worker spins on one hart while it paces, which is the price of that
//! purity: a board that fails with the stress on could be failing because one
//! hart is busy, not because of the interrupts.
//!
//! DIAGNOSTIC ONLY.

use alloc::string::String;
use core::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use aster_cmdline::define_kv_param;
use spin::Once;

static REQUESTED: Once<String> = Once::new();
define_kv_param!("asterinas.ipi_stress", REQUESTED);

/// `asterinas.ipi_stress_dry`: pace the worker but send nothing.
///
/// The worker spins on one hart. Without this control, a board that dies
/// under the stress could be dying because a hart is busy rather than
/// because of the interrupts, and the two explanations cannot be told apart.
static DRY_RUN: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.ipi_stress_dry", DRY_RUN);

/// `asterinas.ipi_stress_broadcast`: one firmware call for all harts.
///
/// The default is one call per hart, which is how the kernel sent them before
/// the mask batching. Holding the delivery count fixed while changing the
/// number of machine-mode entries is what separates the two explanations for
/// a board that dies under interrupt load.
static BROADCAST: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.ipi_stress_broadcast", BROADCAST);

static RATE: AtomicU64 = AtomicU64::new(0);
static SENT: AtomicU64 = AtomicU64::new(0);

/// Returns how many bare interrupts the stress worker has sent.
pub(crate) fn sent() -> u64 {
    SENT.load(Ordering::Relaxed)
}

/// Starts the stress worker when `asterinas.ipi_stress` asks for one.
pub(crate) fn spawn_if_requested() {
    let Some(rate) = REQUESTED
        .get()
        .and_then(|value| value.trim().parse::<u64>().ok())
    else {
        return;
    };
    if rate == 0 {
        return;
    }
    RATE.store(rate, Ordering::Relaxed);
    ostd::early_println!(
        "ASTERINAS_IPI_STRESS status=armed rate_per_second={} dry_run={} broadcast={} \
warning=diagnostic-only",
        rate,
        u8::from(DRY_RUN.load(Ordering::Relaxed)),
        u8::from(BROADCAST.load(Ordering::Relaxed))
    );
    crate::thread::kernel_thread::ThreadOptions::new(worker).spawn();
}

fn worker() {
    let rate = RATE.load(Ordering::Relaxed).max(1);
    let frequency = ostd::arch::tsc_freq().max(1);
    let interval = (frequency / rate).max(1);
    let mut next = ostd::arch::read_tsc();
    let mut report_at = next.saturating_add(frequency);
    let mut reported = 0u64;
    let dry_run = DRY_RUN.load(Ordering::Relaxed);
    let broadcast = BROADCAST.load(Ordering::Relaxed);
    loop {
        if !dry_run {
            let sent = ostd::smp::send_bare_ipi_to_others(broadcast);
            SENT.fetch_add(sent as u64, Ordering::Relaxed);
        }

        next = next.saturating_add(interval);
        let now = ostd::arch::read_tsc();
        if next < now {
            // Fell behind; resynchronise instead of bursting to catch up.
            next = now;
        }
        while ostd::arch::read_tsc() < next {
            core::hint::spin_loop();
        }

        if now >= report_at {
            let total = SENT.load(Ordering::Relaxed);
            ostd::early_println!(
                "ASTERINAS_IPI_STRESS sent={} achieved_per_second={}",
                total,
                total.saturating_sub(reported)
            );
            reported = total;
            report_at = now.saturating_add(frequency);
        }
    }
}
