// SPDX-License-Identifier: MPL-2.0

//! Per-process I/O accounting.
//!
//! The counters are exposed through `/proc/[pid]/io` and the page-fault fields
//! of `/proc/[pid]/stat`, following the Linux task I/O accounting model
//! (<https://www.kernel.org/doc/html/latest/filesystems/proc.html>).
//!
//! The character and syscall counters (`rchar`, `wchar`, `syscr`, `syscw`) are
//! updated by the read/write syscall family. A successful call increments its
//! `sysc*` counter once and adds the returned byte count to `*char` when it is
//! positive. Failed calls are not counted here, even though Linux also counts
//! some failures that reach the file operations.
//!
//! The block counters (`read_bytes`, `write_bytes`) are charged when the
//! filesystem submits block I/O while a process is running. Linux charges a
//! request to the task that submits it, so I/O issued from a kernel thread
//! (writeback) or outside any process (mount) is not charged here either.
//! This is an approximation in two respects: metadata I/O performed on behalf
//! of a task is included, and I/O that Linux would charge at a different
//! moment (for example, dirtying a page rather than writing it back) is
//! accounted at submission.
//!
//! `cancelled_write_bytes` is part of the Linux interface but no subsystem
//! tracks truncated pages back to their writer yet, so it stays zero.

use core::sync::atomic::{AtomicU64, Ordering};

/// The per-process I/O accounting state.
///
/// All counters are monotonically increasing. They are updated from several
/// CPUs and read from `/proc`, so atomics are used rather than a lock; each
/// counter is exact on its own, while a snapshot may see counters from
/// slightly different instants.
#[derive(Debug, Default)]
pub struct IoAccounting {
    min_flt: AtomicU64,
    maj_flt: AtomicU64,
    rchar: AtomicU64,
    wchar: AtomicU64,
    syscr: AtomicU64,
    syscw: AtomicU64,
    read_bytes: AtomicU64,
    write_bytes: AtomicU64,
    cancelled_write_bytes: AtomicU64,
}

/// A consistent-enough reading of every [`IoAccounting`] counter.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct IoStats {
    pub min_flt: u64,
    pub maj_flt: u64,
    pub rchar: u64,
    pub wchar: u64,
    pub syscr: u64,
    pub syscw: u64,
    pub read_bytes: u64,
    pub write_bytes: u64,
    pub cancelled_write_bytes: u64,
}

impl IoAccounting {
    pub fn new() -> Self {
        Self::default()
    }

    /// Records one page fault resolved by the VMAR.
    ///
    /// A major fault needed the backing object to be read from storage; a
    /// minor fault was resolved from memory that was already there or is
    /// being allocated.
    pub fn account_page_fault(&self, major: bool) {
        let counter = if major { &self.maj_flt } else { &self.min_flt };
        counter.fetch_add(1, Ordering::Relaxed);
    }

    /// Records bytes returned by a read-family syscall.
    pub fn account_read_chars(&self, bytes: usize) {
        self.rchar.fetch_add(bytes as u64, Ordering::Relaxed);
    }

    /// Records bytes accepted by a write-family syscall.
    pub fn account_write_chars(&self, bytes: usize) {
        self.wchar.fetch_add(bytes as u64, Ordering::Relaxed);
    }

    /// Records one call to a read-family syscall.
    pub fn account_read_syscall(&self) {
        self.syscr.fetch_add(1, Ordering::Relaxed);
    }

    /// Records one call to a write-family syscall.
    pub fn account_write_syscall(&self) {
        self.syscw.fetch_add(1, Ordering::Relaxed);
    }

    /// Records bytes fetched from the storage layer on behalf of the process.
    pub fn account_read_bytes(&self, bytes: usize) {
        self.read_bytes.fetch_add(bytes as u64, Ordering::Relaxed);
    }

    /// Records bytes sent to the storage layer on behalf of the process.
    pub fn account_write_bytes(&self, bytes: usize) {
        self.write_bytes.fetch_add(bytes as u64, Ordering::Relaxed);
    }

    /// Returns the `(minor, major)` page-fault counts.
    pub fn faults(&self) -> (u64, u64) {
        (
            self.min_flt.load(Ordering::Relaxed),
            self.maj_flt.load(Ordering::Relaxed),
        )
    }

    /// Reads every counter.
    pub fn snapshot(&self) -> IoStats {
        let (min_flt, maj_flt) = self.faults();
        IoStats {
            min_flt,
            maj_flt,
            rchar: self.rchar.load(Ordering::Relaxed),
            wchar: self.wchar.load(Ordering::Relaxed),
            syscr: self.syscr.load(Ordering::Relaxed),
            syscw: self.syscw.load(Ordering::Relaxed),
            read_bytes: self.read_bytes.load(Ordering::Relaxed),
            write_bytes: self.write_bytes.load(Ordering::Relaxed),
            cancelled_write_bytes: self.cancelled_write_bytes.load(Ordering::Relaxed),
        }
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn counters_accumulate_independently() {
        let accounting = IoAccounting::new();
        accounting.account_page_fault(false);
        accounting.account_page_fault(false);
        accounting.account_page_fault(true);
        accounting.account_read_syscall();
        accounting.account_read_chars(4096);
        accounting.account_read_chars(12);
        accounting.account_write_syscall();
        accounting.account_write_chars(7);
        accounting.account_read_bytes(512);
        accounting.account_write_bytes(1024);

        let stats = accounting.snapshot();
        assert_eq!(stats.min_flt, 2);
        assert_eq!(stats.maj_flt, 1);
        assert_eq!(stats.syscr, 1);
        assert_eq!(stats.rchar, 4108);
        assert_eq!(stats.syscw, 1);
        assert_eq!(stats.wchar, 7);
        assert_eq!(stats.read_bytes, 512);
        assert_eq!(stats.write_bytes, 1024);
        assert_eq!(stats.cancelled_write_bytes, 0);
    }
}
