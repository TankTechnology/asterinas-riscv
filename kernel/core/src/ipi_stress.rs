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

/// `asterinas.ipi_stress_flush`: make the target run a real `sfence.vma`.
///
/// The bare interrupt experiment showed that neither the machine-mode send
/// nor the interrupt delivery troubles the board. This mode asks the target
/// to do the page-table maintenance a real flush interrupt does, which is the
/// remaining difference between the stress and the failing workload.
static FLUSH_WORK: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.ipi_stress_flush", FLUSH_WORK);

/// `asterinas.ipi_stress_wait`: wait for every target after a flush IPI.
///
/// The kernel's real flush sequence is "change the page table, tell the
/// others, wait until they have all invalidated". Without the wait, the
/// sender runs ahead and the collaboration is never exercised, so a board
/// that survives that is not evidence about the real workload.
static WAIT_TARGETS: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.ipi_stress_wait", WAIT_TARGETS);

/// `asterinas.ipi_stress_remote_flush`: restore remote TLB flushes at start.
///
/// The board often dies during boot with real flushes enabled, so the
/// diagnostic boot turns them off to get a desktop. Restoring them here, once
/// the desktop is up, gives a controlled way to run the real flush traffic
/// from a known-good state instead of relying on the boot surviving.
static RESTORE_REMOTE_FLUSH: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.ipi_stress_remote_flush", RESTORE_REMOTE_FLUSH);

/// `asterinas.ipi_stress_cycle`: map a page, invalidate it remotely, unmap.
///
/// The pure-sfence experiment showed that invalidating pages that are not
/// being modified does not trouble the board. This mode performs the whole
/// sequence a real DMA mapping does, so the page table changes between the
/// invalidations -- the last difference between the stress and the failing
/// workload.
static CYCLE: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.ipi_stress_cycle", CYCLE);

/// `asterinas.ipi_stress_touch`: let every target hold a valid translation
/// before invalidating it.
///
/// A shared DMA buffer on a board whose interrupts move between harts is
/// usually resident in more than one TLB when it is unmapped. The plain cycle
/// invalidates ranges no target has ever translated, which the hardware can
/// satisfy without doing anything.
static TOUCH: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.ipi_stress_touch", TOUCH);

/// `asterinas.ipi_stress_delay=<seconds>`: wait before injecting.
///
/// The worker pins a hart while it paces, so injecting from the first second
/// slows boot enough that the desktop never comes up -- and a stress result
/// measured without a desktop is not the workload the board fails under.
/// Delaying the injection lets the system settle first; the worker yields
/// while it waits instead of spinning.
static DELAY_SECONDS: AtomicU64 = AtomicU64::new(0);

static REQUESTED_DELAY: Once<String> = Once::new();
define_kv_param!("asterinas.ipi_stress_delay", REQUESTED_DELAY);

static RATE: AtomicU64 = AtomicU64::new(0);
static SENT: AtomicU64 = AtomicU64::new(0);

/// `asterinas.ipi_stress_workers=<n>`: how many senders inject at once.
///
/// The single-sender stress proved that a high rate of interrupts, sends or
/// real invalidations, leaves the board running. The real workload has
/// several harts sending at each other at the same time, and nothing has
/// tested that: concurrent senders can behave differently in firmware, where
/// every send is a machine-mode entry that may arrive while another hart is
/// already in machine mode.
static WORKERS: AtomicU64 = AtomicU64::new(1);

static REQUESTED_WORKERS: Once<String> = Once::new();
define_kv_param!("asterinas.ipi_stress_workers", REQUESTED_WORKERS);

/// `asterinas.ipi_stress_pin`: bind worker `i` to CPU `i`.
///
/// Spawning three kernel threads does not prove they run on three harts, and
/// "three harts send at each other" is the condition the board fails under.
/// The default leaves placement to the scheduler; this knob forces it, so the
/// two can be told apart.
static PIN: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.ipi_stress_pin", PIN);

/// Every CPU a worker has been observed on, as a bitmask of logical CPU IDs.
///
/// Written on entry and at each report, so scheduler migration cannot hide:
/// a worker that starts on CPU 2 and later reports from CPU 3 has both bits
/// set.
static WORKER_CPU_MASK: AtomicU64 = AtomicU64::new(0);

fn note_current_cpu() {
    let cpu = u32::from(ostd::cpu::CpuId::current_racy()) as u64;
    WORKER_CPU_MASK.fetch_or(1u64 << cpu.min(63), Ordering::Relaxed);
}

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
    let workers = REQUESTED_WORKERS
        .get()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .unwrap_or(1)
        .clamp(1, 64);
    WORKERS.store(workers, Ordering::Relaxed);
    if let Some(delay) = REQUESTED_DELAY
        .get()
        .and_then(|value| value.trim().parse::<u64>().ok())
    {
        DELAY_SECONDS.store(delay, Ordering::Relaxed);
    }
    ostd::early_println!(
        "ASTERINAS_IPI_STRESS status=armed rate_per_second={} workers={} dry_run={} broadcast={} \
flush_work={} cycle={} touch={} delay_seconds={} pin={} wait={} warning=diagnostic-only",
        rate,
        workers,
        u8::from(DRY_RUN.load(Ordering::Relaxed)),
        u8::from(BROADCAST.load(Ordering::Relaxed)),
        u8::from(FLUSH_WORK.load(Ordering::Relaxed)),
        u8::from(CYCLE.load(Ordering::Relaxed)),
        u8::from(TOUCH.load(Ordering::Relaxed)),
        DELAY_SECONDS.load(Ordering::Relaxed),
        u8::from(PIN.load(Ordering::Relaxed)),
        u8::from(WAIT_TARGETS.load(Ordering::Relaxed))
    );
    let pin = PIN.load(Ordering::Relaxed);
    for index in 0..workers {
        let mut options = crate::thread::kernel_thread::ThreadOptions::new(move || worker(index));
        if pin {
            let cpu_count = ostd::cpu::num_cpus() as u64;
            let cpu = ostd::cpu::CpuId::try_from((index % cpu_count) as usize).unwrap();
            let mut affinity = ostd::cpu::CpuSet::new_empty();
            affinity.add(cpu);
            options = options.cpu_affinity(affinity);
        }
        options.spawn();
    }
}

fn worker(index: u64) {
    let workers = WORKERS.load(Ordering::Relaxed).max(1);
    let rate = (RATE.load(Ordering::Relaxed) / workers).max(1);
    let frequency = ostd::arch::tsc_freq().max(1);
    let interval = (frequency / rate).max(1);
    let mut next = ostd::arch::read_tsc();
    let mut report_at = next.saturating_add(frequency);
    let mut reported = 0u64;
    let mut reported_calls = 0u64;
    let dry_run = DRY_RUN.load(Ordering::Relaxed);
    let broadcast = BROADCAST.load(Ordering::Relaxed);
    let flush_work = FLUSH_WORK.load(Ordering::Relaxed);
    let delay = DELAY_SECONDS.load(Ordering::Relaxed);
    // Report placement before waiting: whether these threads land on distinct
    // harts is the question, and it must be answerable from the boot log even
    // if the run dies before the delay elapses.
    note_current_cpu();
    ostd::early_println!(
        "ASTERINAS_IPI_STRESS_WORKER index={} cpu={}",
        index,
        u32::from(ostd::cpu::CpuId::current_racy())
    );
    while ostd::timer::Jiffies::elapsed().as_duration().as_secs() < delay {
        crate::thread::Thread::yield_now();
    }
    // Start the reporting clock here, not before the wait: otherwise the first
    // report fires on the first iteration and reports a meaningless delta.
    next = ostd::arch::read_tsc();
    report_at = next.saturating_add(frequency);
    // Start the call-latency average from here, and drop the maximum that boot
    // traffic accumulated.
    reported_calls = ostd::smp::sbi_ipi_call_stats().0;
    if index == 0 && RESTORE_REMOTE_FLUSH.load(Ordering::Relaxed) {
        ostd::mm::tlb::set_no_remote_flush(false);
        ostd::early_println!("ASTERINAS_TLB_REMOTE_FLUSH status=restored");
    }
    if index == 0 {
        // What a measurement costs and what firmware entry costs, measured in
        // this same context: a 30 us send is only meaningful next to these.
        let (read_ticks, version_ticks, ipi_ticks) = ostd::smp::probe_sbi_costs();
        ostd::early_println!(
            "ASTERINAS_SBI_PROBE timebase_read_us={} version_us={} ipi_us={}",
            read_ticks.saturating_mul(1_000_000) / frequency,
            version_ticks.saturating_mul(1_000_000) / frequency,
            ipi_ticks.saturating_mul(1_000_000) / frequency
        );
    }
    loop {
        if !dry_run {
            let sent = if TOUCH.load(Ordering::Relaxed) {
                ostd::smp::stress_touch_flush_cycle()
            } else if CYCLE.load(Ordering::Relaxed) {
                ostd::smp::stress_shootdown_cycle()
            } else if flush_work && WAIT_TARGETS.load(Ordering::Relaxed) {
                ostd::smp::send_flush_ipi_to_others_waiting()
            } else if flush_work {
                ostd::smp::send_flush_ipi_to_others()
            } else {
                ostd::smp::send_bare_ipi_to_others(broadcast)
            };
            SENT.fetch_add(sent as u64, Ordering::Relaxed);
        }

        next = next.saturating_add(interval);
        let now = ostd::arch::read_tsc();
        if next < now {
            // Fell behind; resynchronise instead of bursting to catch up.
            next = now;
        }
        // Wait out the interval without monopolizing the hart. A pure spin
        // here is not a neutral load: with three workers at once, the kernel's
        // heartbeat and `Jiffies` stopped within a second of the injection
        // starting and the board reset -- with no interrupt sent at all in
        // the dry run. Yield while the wait is long, and spin only for the
        // last few microseconds, where a yield would overshoot the pacing.
        let yield_threshold = (frequency / 100_000).max(1); // about 10 us
        loop {
            let now = ostd::arch::read_tsc();
            let remaining = next.saturating_sub(now);
            if remaining == 0 {
                break;
            }
            if remaining > yield_threshold {
                crate::thread::Thread::yield_now();
            } else {
                core::hint::spin_loop();
            }
        }

        // Read the clock after the wait: an iteration that fell behind would
        // otherwise leave `report_at` in the past and report every pass.
        let now = ostd::arch::read_tsc();
        if now >= report_at {
            // One reporter only. Every worker shares `SENT`, so letting each
            // print its own line produced duplicate totals that read as extra
            // senders.
            if index == 0 {
                note_current_cpu();
                let total = SENT.load(Ordering::Relaxed);
                // The firmware send call's own duration. It is the continuous
                // measurement that says whether concurrent senders queue in
                // machine mode: the death of the board is binary and noisy,
                // this is a number every second.
                let (calls, call_ticks, call_max_ticks, max_cpu, max_uptime_ms) =
                    ostd::smp::sbi_ipi_call_stats();
                let delta_calls = calls.saturating_sub(reported_calls);
                let avg_us = if delta_calls > 0 {
                    call_ticks.saturating_mul(1_000_000) / frequency / delta_calls
                } else {
                    0
                };
                let max_us = call_max_ticks.saturating_mul(1_000_000) / frequency;
                reported_calls = calls;
                // Time spent waiting for the targets: a queue grows with load,
                // a silent target does not answer at all.
                let (wait_calls, wait_ticks, wait_max_ticks) = ostd::smp::take_ipi_wait_stats();
                let wait_avg_us = if wait_calls > 0 {
                    wait_ticks.saturating_mul(1_000_000) / frequency / wait_calls
                } else {
                    0
                };
                let wait_max_us = wait_max_ticks.saturating_mul(1_000_000) / frequency;
                // `elapsed_ms` is the kernel's own uptime clock. Under stress
                // the heartbeat's one-per-second cadence can stretch, so a
                // run's real duration cannot be counted in heartbeats.
                ostd::early_println!(
                    "ASTERINAS_IPI_STRESS sent={} achieved_per_second={} elapsed_ms={} \
cpu_mask={:x} sbi_avg_us={} sbi_max_us={} sbi_max_cpu={} sbi_max_uptime_ms={} \
wait_avg_us={} wait_max_us={}",
                    total,
                    total.saturating_sub(reported),
                    ostd::timer::Jiffies::elapsed().as_duration().as_millis(),
                    WORKER_CPU_MASK.load(Ordering::Relaxed),
                    avg_us,
                    max_us,
                    max_cpu,
                    max_uptime_ms,
                    wait_avg_us,
                    wait_max_us
                );
                reported = total;
            }
            report_at = now.saturating_add(frequency);
        }
    }
}
