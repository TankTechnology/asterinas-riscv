// SPDX-License-Identifier: MPL-2.0

//! The sampling profiler: one sample per timer tick, into the trace ring.
//!
//! Enabling `events/profile/sample` starts it and disabling stops it; there is
//! no other switch. A sample is the interrupted program counter, so the timer
//! interrupt's own work never shows up. User-mode samples carry no symbol (this
//! kernel does not read user symbol tables) but they still attribute time to a
//! process, which `/proc/trace/profile` uses.
//!
//! The timestamp is Jiffies, not the monotonic clock: a sample is taken inside
//! the timer interrupt, and the clocksource's own update runs there too -- the
//! same hazard the function tracer has.

use alloc::{string::String, vec::Vec};

use aster_util::printer::VmPrinter;

use super::{event::TraceEvent, events::profile::SAMPLE, kallsyms, ring};
use crate::prelude::*;

/// Installs the sampling hook.
pub fn init() {
    ostd::irq::inject_timer_sample_hook(sample);
}

fn sample(pc: usize, user: bool) {
    if !SAMPLE.enabled() {
        return;
    }
    let (pid, tid) = ostd::task::Task::current()
        .map(|task| crate::thread::task_ids(&task))
        .unwrap_or((0, 0));

    let mut payload = [0u8; 9];
    payload[..8].copy_from_slice(&(pc as u64).to_le_bytes());
    payload[8] = u8::from(user);

    let elapsed = ostd::timer::Jiffies::elapsed().as_duration();
    if crate::bpf::attach::is_attached(SAMPLE.id()) {
        crate::bpf::attach::run_at(
            SAMPLE.id(),
            pid,
            tid,
            [pc as u64, u64::from(user), 0],
            elapsed.as_nanos() as u64,
        );
    }
    ring::push_at(SAMPLE.id(), pid, tid, &payload, elapsed);
}

/// Decodes a sample record into `(pc, user)`.
pub fn decode(data: &[u8]) -> Option<(u64, bool)> {
    if data.len() < 9 {
        return None;
    }
    let mut bytes = [0u8; 8];
    bytes.copy_from_slice(&data[..8]);
    Some((u64::from_le_bytes(bytes), data[8] != 0))
}

/// Renders a histogram of the samples currently in the ring.
///
/// `offset` makes the file readable in chunks like every other procfs file:
/// the whole histogram is rendered into memory, then skipped to the offset.
pub fn render_histogram(offset: usize, writer: &mut VmWriter) -> Result<usize> {
    let mut kernel: Vec<(&'static str, usize)> = Vec::new();
    let mut user: Vec<(u32, usize)> = Vec::new();
    let mut total = 0usize;

    ring::for_each_record(|record| {
        if record.event != SAMPLE.id() {
            return true;
        }
        let Some((pc, is_user)) = decode(record.data) else {
            return true;
        };
        total += 1;
        if is_user {
            match user.iter_mut().find(|(pid, _)| *pid == record.pid) {
                Some((_, count)) => *count += 1,
                None => user.push((record.pid, 1)),
            }
        } else {
            let name = kallsyms::lookup_with_offset(pc)
                .map(|(name, _)| name)
                .unwrap_or("<unknown>");
            match kernel.iter_mut().find(|(candidate, _)| *candidate == name) {
                Some((_, count)) => *count += 1,
                None => kernel.push((name, 1)),
            }
        }
        true
    });

    kernel.sort_by(|a, b| b.1.cmp(&a.1));
    user.sort_by(|a, b| b.1.cmp(&a.1));

    let user_total: usize = user.iter().map(|(_, count)| *count).sum();
    let mut text = String::new();
    let _ = core::fmt::write(
        &mut text,
        format_args!(
            "# samples: {} (kernel {}, user {})\n",
            total,
            total - user_total,
            user_total
        ),
    );
    // Kernel symbols first: that is what the profile is usually read for.
    for (name, count) in &kernel {
        let _ = core::fmt::write(
            &mut text,
            format_args!(
                "{:>8} {:>6.1}% {}\n",
                count,
                *count as f64 * 100.0 / total.max(1) as f64,
                name
            ),
        );
    }
    // User samples have no symbols here, so they are summarized per process.
    for (pid, count) in &user {
        let _ = core::fmt::write(
            &mut text,
            format_args!(
                "{:>8} {:>6.1}% <user> pid {}\n",
                count,
                *count as f64 * 100.0 / total.max(1) as f64,
                pid
            ),
        );
    }

    let mut printer = VmPrinter::new_skip(writer, offset);
    write!(printer, "{}", text)?;
    Ok(printer.bytes_written())
}
