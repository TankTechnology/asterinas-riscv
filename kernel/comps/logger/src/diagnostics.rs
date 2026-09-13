// SPDX-License-Identifier: MPL-2.0

//! Opt-in boot probe of the real user-record logging path.
//!
//! Statistics belong to the probe's stack, not the logger or a global sampler.
//! Normal log calls use `Unmeasured`, whose hooks inline to nothing. No new
//! lock, queue, worker, or synchronization protocol participates in logging.

use core::sync::atomic::{AtomicBool, Ordering};

use component::{ComponentInitError, init_component};

#[path = "diagnostics_stats.rs"]
mod stats;
use stats::DurationStats;

static REQUESTED: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.log_profile", REQUESTED);

pub(super) trait Observer {
    const COUNT_BYTES: bool;
    fn timestamp(&self) -> Option<u64>;
    fn memory(&mut self, start: Option<u64>, end: Option<u64>);
    fn console(&mut self, start: Option<u64>, acquired: Option<u64>, end: Option<u64>, bytes: u64);
}

pub(super) struct Unmeasured;

impl Observer for Unmeasured {
    const COUNT_BYTES: bool = false;
    #[inline(always)]
    fn timestamp(&self) -> Option<u64> {
        None
    }
    #[inline(always)]
    fn memory(&mut self, _: Option<u64>, _: Option<u64>) {}
    #[inline(always)]
    fn console(&mut self, _: Option<u64>, _: Option<u64>, _: Option<u64>, _: u64) {}
}

struct Measurement {
    memory: DurationStats,
    lock_wait: DurationStats,
    locked_send: DurationStats,
    attempted_bytes: u64,
}

impl Measurement {
    fn new() -> Self {
        Self {
            memory: DurationStats::new(),
            lock_wait: DurationStats::new(),
            locked_send: DurationStats::new(),
            attempted_bytes: 0,
        }
    }
}

impl Observer for Measurement {
    const COUNT_BYTES: bool = true;
    fn timestamp(&self) -> Option<u64> {
        timestamp()
    }
    fn memory(&mut self, start: Option<u64>, end: Option<u64>) {
        self.memory.observe(start, end);
    }
    fn console(&mut self, start: Option<u64>, acquired: Option<u64>, end: Option<u64>, bytes: u64) {
        self.lock_wait.observe(start, acquired);
        self.locked_send.observe(acquired, end);
        self.attempted_bytes = self.attempted_bytes.saturating_add(bytes);
    }
}

fn timestamp() -> Option<u64> {
    // CSR reads (including TIME) are device input for RISC-V FENCE ordering.
    // Use the existing safe I/O fence on both sides, also a compiler memory
    // barrier. See https://docs.riscv.org/reference/isa/unpriv/zicsr.html.
    // TIME clocks are synchronized within one tick across harts. A migrated
    // sub-tick sample may reverse; the accumulator rejects it, never wraps.
    #[cfg(target_arch = "riscv64")]
    {
        if ostd::arch::tsc_freq() == 0 {
            return None;
        }
        ostd::arch::device::io_mem::fence();
        let ticks = ostd::arch::read_tsc();
        ostd::arch::device::io_mem::fence();
        Some(ticks)
    }
    // Do not substitute an unserialized x86 TSC or assume a calibrated clock.
    #[cfg(not(target_arch = "riscv64"))]
    {
        None
    }
}

#[init_component(kthread)]
fn run_if_requested() -> Result<(), ComponentInitError> {
    if !REQUESTED.load(Ordering::Relaxed) {
        return Ok(());
    }
    if timestamp().is_none() {
        ostd::early_println!("LOG_PROFILE unavailable=clock");
        return Ok(());
    }

    struct RestoreConsole(u8);
    impl Drop for RestoreConsole {
        fn drop(&mut self) {
            super::klog::klog().set_console_level(self.0);
        }
    }
    let log = super::klog::klog();
    let _restore = RestoreConsole(log.console_level());
    let frequency = ostd::arch::tsc_freq();
    let mut overhead = DurationStats::new();
    for _ in 0..128 {
        overhead.observe(timestamp(), timestamp());
    }
    ostd::early_println!(
        "LOG_PROFILE overhead frequency={} stats={:?}",
        frequency,
        overhead
    );

    // Keep record contents/volume and memory capture constant. Alternate the
    // order of levels and instrumentation to avoid a fixed warmup/order bias.
    // This runs before userspace, so no user can concurrently change policy.
    for repetition in 0..5 {
        for level_index in 0..2 {
            let level = if (repetition + level_index) % 2 == 0 {
                4
            } else {
                7
            };
            log.set_console_level(level);
            for mode_index in 0..2 {
                let measured = (repetition + mode_index) % 2 == 0;
                let mut measurement = Measurement::new();
                let start = timestamp();
                for _ in 0..32 {
                    const MESSAGE: &[u8] =
                        b"LOG_PROFILE fixed-size user record for synchronous console measurement";
                    if measured {
                        log.push_observed(14, MESSAGE, &mut measurement);
                    } else {
                        log.push(14, MESSAGE);
                    }
                }
                let end = timestamp();
                let mut elapsed = DurationStats::new();
                elapsed.observe(start, end);
                // Summaries bypass the logger and are outside all timed work.
                ostd::early_println!(
                    "LOG_PROFILE batch rep={} level={} measured={} records=32 elapsed={:?} memory={:?} lock_wait={:?} locked_send={:?} attempted_bytes={}",
                    repetition,
                    level,
                    measured,
                    elapsed,
                    measurement.memory,
                    measurement.lock_wait,
                    measurement.locked_send,
                    measurement.attempted_bytes,
                );
            }
        }
    }
    ostd::early_println!("LOG_PROFILE end records=640 source=user");
    Ok(())
}
