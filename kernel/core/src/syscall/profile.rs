// SPDX-License-Identifier: MPL-2.0

//! Opt-in phase timing for system calls.
//!
//! Enable with `asterinas.stat_profile` on the kernel command line, then read
//! `/proc/syscall_profile` for the accumulated breakdown and write to it to
//! clear the counters.
//!
//! The first version logged one line per call. That is unusable on the board:
//! a `stat` loop floods the 115200-baud console, the kernel drops log lines,
//! and the logging itself dominates the timings it measures. This version
//! accumulates into a table instead, so the cost while enabled is one short
//! lock per call and nothing is written to the console.

use alloc::{collections::BTreeMap, format, string::String, vec::Vec};
use core::sync::atomic::{AtomicBool, Ordering};

use crate::prelude::Mutex;

static STAT_PROFILE: AtomicBool = AtomicBool::new(false);

aster_cmdline::define_flag_param!("asterinas.stat_profile", STAT_PROFILE);

#[derive(Clone, Copy, Default)]
struct Stat {
    count: u64,
    total_us: u64,
    max_us: u64,
}

impl Stat {
    fn record(&mut self, us: u64) {
        self.count += 1;
        self.total_us += us;
        self.max_us = self.max_us.max(us);
    }

    fn mean_us(&self) -> u64 {
        if self.count == 0 {
            0
        } else {
            self.total_us / self.count
        }
    }
}

/// Per-syscall, per-phase totals. Keyed by static names so recording never
/// allocates after the first call of each `(syscall, phase)` pair.
static SAMPLES: Mutex<BTreeMap<&'static str, BTreeMap<&'static str, Stat>>> =
    Mutex::new(BTreeMap::new());

fn record(syscall: &'static str, phase: &'static str, us: u64) {
    let mut samples = SAMPLES.lock();
    samples
        .entry(syscall)
        .or_default()
        .entry(phase)
        .or_default()
        .record(us);
}

/// Renders `/proc/syscall_profile`.
pub(crate) fn render_report() -> String {
    let samples = SAMPLES.lock();
    let mut text = String::new();
    if !STAT_PROFILE.load(Ordering::Relaxed) {
        text.push_str("disabled (boot with asterinas.stat_profile)\n");
    }
    if samples.is_empty() {
        text.push_str("no samples\n");
        return text;
    }
    text.push_str("syscall phase count mean_us max_us\n");
    for (syscall, phases) in samples.iter() {
        for (phase, stat) in phases.iter() {
            text.push_str(&format!(
                "{} {} {} {} {}\n",
                syscall,
                phase,
                stat.count,
                stat.mean_us(),
                stat.max_us
            ));
        }
    }
    text
}

/// Drops every sample.
pub(crate) fn reset() {
    SAMPLES.lock().clear();
}

/// Switches sampling on or off at run time.
///
/// The boot flag only sets the initial state. Being able to turn the profiler
/// on from `/proc/syscall_profile` means a measurement no longer needs a reboot
/// with a different command line.
pub(crate) fn set_enabled(enabled: bool) {
    STAT_PROFILE.store(enabled, Ordering::Relaxed);
}

/// Whether the profiler is switched on for this boot.
pub(crate) fn enabled() -> bool {
    STAT_PROFILE.load(Ordering::Relaxed)
}

/// Times one nested operation and files it under `group`/`phase`.
///
/// Unlike [`PhaseProfiler`], this has no notion of a per-syscall sequence: it
/// is for code that runs many times inside one syscall (a path lookup visits
/// one directory per component), where the interesting number is the mean cost
/// of the operation itself. When the profiler is off the closure runs with a
/// single relaxed load of overhead.
pub(crate) fn measure<T>(group: &'static str, phase: &'static str, body: impl FnOnce() -> T) -> T {
    if !STAT_PROFILE.load(Ordering::Relaxed) {
        return body();
    }
    let start = ostd::arch::read_tsc();
    let result = body();
    let end = ostd::arch::read_tsc();
    let frequency = ostd::arch::tsc_freq().max(1);
    record(group, phase, ticks_to_us(end, start, frequency));
    result
}

/// Collects phase boundaries for one system call.
///
/// Marks are wall-clock positions in the platform timebase, so a phase
/// includes any time the task spent descheduled inside it.
pub(crate) struct PhaseProfiler {
    syscall: &'static str,
    enabled: bool,
    start: u64,
    marks: Vec<(&'static str, u64)>,
}

impl PhaseProfiler {
    pub(crate) fn start(syscall: &'static str) -> Self {
        let enabled = STAT_PROFILE.load(Ordering::Relaxed);
        Self {
            syscall,
            enabled,
            start: if enabled { ostd::arch::read_tsc() } else { 0 },
            marks: Vec::new(),
        }
    }

    /// Records the end of the phase that started at the previous mark.
    pub(crate) fn mark(&mut self, name: &'static str) {
        if self.enabled {
            self.marks.push((name, ostd::arch::read_tsc()));
        }
    }

    /// Accounts the collected marks and stops recording.
    pub(crate) fn finish(&mut self) {
        if !self.enabled {
            return;
        }
        // Read the end of the call *before* doing anything else: string
        // building and locking allocate, and doing them first put their cost
        // into "total" while every phase read as one or two microseconds.
        let end = ostd::arch::read_tsc();
        let frequency = ostd::arch::tsc_freq().max(1);
        let mut previous = self.start;
        for (name, at) in self.marks.drain(..) {
            record(self.syscall, name, ticks_to_us(at, previous, frequency));
            previous = at;
        }
        let total_us = ticks_to_us(end, self.start, frequency);
        record(self.syscall, "total", total_us);
        // Prevent the `Drop` impl from reporting the same marks twice.
        self.enabled = false;
    }
}

fn ticks_to_us(end: u64, start: u64, frequency: u64) -> u64 {
    end.saturating_sub(start) * 1_000_000 / frequency
}

impl Drop for PhaseProfiler {
    fn drop(&mut self) {
        // A profiler dropped without `finish` is a syscall that returned an
        // error halfway through; keep the phases it did reach.
        if self.enabled && !self.marks.is_empty() {
            let end = ostd::arch::read_tsc();
            let frequency = ostd::arch::tsc_freq().max(1);
            let syscall = self.syscall;
            let marks = core::mem::take(&mut self.marks);
            let mut previous = self.start;
            for (name, at) in marks {
                record(syscall, name, ticks_to_us(at, previous, frequency));
                previous = at;
            }
            let total_us = ticks_to_us(end, self.start, frequency);
            record(syscall, "aborted_total", total_us);
        }
    }
}
