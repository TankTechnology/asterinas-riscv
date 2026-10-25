// SPDX-License-Identifier: MPL-2.0

//! `/proc/syscall_time`: per-syscall CPU time counters.
//!
//! Format, one line per syscall with `count > 0`, sorted by `cpu_ns`:
//!
//! ```text
//! # enabled=1 (columns: nr count total_ns blocked_ns cpu_ns avg_cpu_ns)
//! <nr> <count> <total_ns> <blocked_ns> <cpu_ns> <avg_cpu_ns>
//! ```
//!
//! `total_ns` is the wall time spent inside the syscall, `blocked_ns` the
//! part of it spent descheduled by the task scheduler, and
//! `cpu_ns = total_ns - blocked_ns` the actual CPU consumption.
//!
//! Writing `on`/`off` enables or disables the timing, and `reset` clears the
//! counters.  Timing is off by default so a normal run pays no rdtsc cost.

use alloc::{format, string::String, vec::Vec};
use core::sync::atomic::Ordering;

use super::template::{ProcFile, ProcFileOps};
use crate::{
    fs::{file::mkmod, vfs::inode::Inode},
    prelude::*,
    syscall::{
        SYSCALL_TIME_BLOCKED, SYSCALL_TIME_COUNTS, SYSCALL_TIME_CYCLES, SYSCALL_TIME_SLOTS,
        syscall_time_enabled, syscall_time_reset, syscall_time_set_enabled,
    },
};

/// The inode at `/proc/syscall_time`.
pub(super) struct SyscallTimeFileOps;

impl SyscallTimeFileOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

fn format_rows() -> String {
    let freq = ostd::arch::tsc_freq().max(1) as u128;
    let to_ns = |cycles: u64| (cycles as u128 * 1_000_000_000u128 / freq) as u64;

    let mut rows: Vec<(u64, u64, u64, u64, u64)> = Vec::new();
    for nr in 0..SYSCALL_TIME_SLOTS {
        let count = SYSCALL_TIME_COUNTS[nr].load(Ordering::Relaxed);
        if count == 0 {
            continue;
        }
        let cycles = SYSCALL_TIME_CYCLES[nr].load(Ordering::Relaxed);
        let blocked = SYSCALL_TIME_BLOCKED[nr].load(Ordering::Relaxed).min(cycles);
        rows.push((nr as u64, count, cycles, blocked, cycles - blocked));
    }
    // Sort by CPU cycles (total - blocked), the useful attribution order.
    rows.sort_by(|a, b| b.4.cmp(&a.4));

    let mut out = String::new();
    out.push_str(&format!(
        "# enabled={} (columns: nr count total_ns blocked_ns cpu_ns avg_cpu_ns)\n",
        u8::from(syscall_time_enabled())
    ));
    for (nr, count, cycles, blocked, cpu) in rows {
        let total_ns = to_ns(cycles);
        let blocked_ns = to_ns(blocked);
        let cpu_ns = to_ns(cpu);
        let avg_cpu_ns = cpu_ns / count.max(1);
        out.push_str(&format!(
            "{nr} {count} {total_ns} {blocked_ns} {cpu_ns} {avg_cpu_ns}\n"
        ));
    }
    out
}

impl ProcFileOps for SyscallTimeFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let text = format_rows();
        let bytes = text.as_bytes();
        let mut reader = VmReader::from(&bytes[offset.min(bytes.len())..]);
        let bytes_read = writer.write_fallible(&mut reader)?;
        Ok(bytes_read)
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        let mut buf = [0u8; 16];
        let mut writer = VmWriter::from(&mut buf[..]);
        let len = reader.read_fallible(&mut writer)?;
        let text = core::str::from_utf8(&buf[..len])
            .map_err(|_| Error::with_message(Errno::EINVAL, "not UTF-8"))?
            .trim();
        match text {
            "on" => syscall_time_set_enabled(true),
            "off" => syscall_time_set_enabled(false),
            "reset" => syscall_time_reset(),
            _ => {
                return_errno_with_message!(
                    Errno::EINVAL,
                    "write 'on', 'off' or 'reset' to /proc/syscall_time"
                )
            }
        }
        Ok(len)
    }
}
