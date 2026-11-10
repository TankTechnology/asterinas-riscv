// SPDX-License-Identifier: MPL-2.0

//! Opt-in phase timing for the exec path.
//!
//! Enable with `asterinas.exec_profile` on the kernel command line. Each exec
//! logs one line with the time spent in each phase, including the ELF
//! loader's subphases. This is a diagnostic aid: leaving it on during a fork
//! loop floods the serial console and distorts the timings it measures.
//!
//! The phase names are:
//! `lk` executable lookup, `av` argv/env copy, `pg` program build (shebang
//! and ELF headers), `vm` new VMAR, `ld` interpreter lookup/parse, `sg`
//! segment mapping and aux vector, `vd` vDSO, `sk` initial stack, `hp` heap,
//! `tk`/`kl` task-set transition and sibling threads, `wt` waiting for other
//! threads, `ac` VMAR activation and capabilities, `cx` CPU context, `fd`
//! file-table unsharing and `O_CLOEXEC` closes, `si` signal state, `pt`
//! ptrace stop check, `fe` finishing the exec, and `tt` the total.

use core::sync::atomic::{AtomicBool, Ordering};

use crate::prelude::*;

pub(super) static EXEC_PROFILE: AtomicBool = AtomicBool::new(false);

aster_cmdline::define_flag_param!("asterinas.exec_profile", EXEC_PROFILE);

/// Collects phase boundaries for one exec.
///
/// Creating a profiler is cheap when profiling is off: it records no marks
/// and allocates nothing. Marks are wall-clock positions in the platform
/// timebase so a phase includes any time the task spent sleeping or
/// descheduled.
pub(crate) struct ExecProfiler {
    enabled: bool,
    start: u64,
    marks: Vec<(&'static str, u64)>,
}

impl ExecProfiler {
    pub(crate) fn start() -> Self {
        let enabled = EXEC_PROFILE.load(Ordering::Relaxed);
        Self {
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

    /// Logs the phase lengths and discards the collected marks.
    ///
    /// The line is deliberately compact: a long diagnostic message can be
    /// partly dropped by the UART's send queue. All values are microseconds;
    /// the short phase names are documented at the call sites. Every mark and
    /// the total are converted before the message is logged, so the console
    /// write itself never appears in a phase.
    pub(crate) fn finish(&mut self) {
        if !self.enabled {
            return;
        }
        let frequency = ostd::arch::tsc_freq().max(1);
        let mut previous = self.start;
        let mut text = alloc::format!("EXEC_PROFILE");
        for (name, at) in self.marks.drain(..) {
            let delta_us = at.saturating_sub(previous) * 1_000_000 / frequency;
            text.push_str(&alloc::format!(" {}={}", name, delta_us));
            previous = at;
        }
        let total_us = ostd::arch::read_tsc().saturating_sub(self.start) * 1_000_000 / frequency;
        text.push_str(&alloc::format!(" tt={}us", total_us));
        ostd::info!("{}", text);
    }
}
