// SPDX-License-Identifier: MPL-2.0

//! Diagnostic counters that say which code path issues TLB flushes.
//!
//! The Megrez board resets under a burst of inter-processor flush calls, and
//! the IPI counters alone cannot say who sends them: every path funnels into
//! the same `dispatch_tlb_flush`. These counters are bumped at each
//! interesting call site and printed by the watchdog heartbeat, so a board
//! log names the hot path instead of leaving it to be guessed.
//!
//! They are diagnostics only. Reading them costs one relaxed atomic add per
//! flush, and nothing behaves differently because of them.

use alloc::string::String;
use core::sync::atomic::{AtomicU32, Ordering};

use aster_cmdline::define_kv_param;
use spin::Once;

/// Flush sites, in the order they are printed.
pub(crate) mod site {
    /// The accessed/dirty fixup in `handle_single_page_fault`.
    ///
    /// Counts fixups, not flushes: since the fixup stopped broadcasting, a
    /// nonzero value with `tx_flush` near zero is the expected shape.
    pub(crate) const AD: usize = 0;
    /// The "shared mapping, or the only reference left" write-fault path.
    pub(crate) const COW_SHARED: usize = 1;
    /// The not-present write/read fault that maps a fresh frame.
    ///
    /// Also counts faults rather than flushes: that path flushes locally.
    pub(crate) const FRESH: usize = 2;
    /// Unmapping a whole mapping (`munmap`, process exit).
    pub(crate) const UNMAP: usize = 3;
    /// Changing a mapping's permissions (`mprotect`).
    pub(crate) const PROTECT: usize = 4;
    /// Freezing a VMO's reverse mappings read-only before a shared write.
    pub(crate) const FREEZE: usize = 5;
    /// `mremap` moving or resizing a mapping.
    pub(crate) const REMAP: usize = 6;
    /// Forking: the child's address space gets a flush-all.
    pub(crate) const FORK: usize = 7;
}

pub(crate) const SITE_NAMES: [&str; 8] = [
    "ad",
    "cow_shared",
    "fresh",
    "unmap",
    "protect",
    "freeze",
    "remap",
    "fork",
];

static COUNTS: [AtomicU32; SITE_NAMES.len()] = [
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
];

/// Records that the given site is about to flush.
#[inline]
pub(crate) fn record(site: usize) {
    COUNTS[site].fetch_add(1, Ordering::Relaxed);
}

/// Returns the counter of every site, in [`SITE_NAMES`] order.
pub(crate) fn snapshot() -> [u32; SITE_NAMES.len()] {
    let mut counts = [0; SITE_NAMES.len()];
    for (count, cell) in counts.iter_mut().zip(COUNTS.iter()) {
        *count = cell.load(Ordering::Relaxed);
    }
    counts
}

static NO_REMOTE_FLUSH: Once<String> = Once::new();
define_kv_param!("asterinas.tlb_no_remote_flush", NO_REMOTE_FLUSH);

/// Applies `asterinas.tlb_no_remote_flush` to the TLB flusher.
///
/// This parameter exists to separate "the flush storm resets the board" from
/// "the board resets for another reason"; a kernel running with it is not
/// correct and says so on the console.
pub(crate) fn init_from_cmdline() {
    let Some(value) = NO_REMOTE_FLUSH.get() else {
        return;
    };
    if value != "1" && value != "on" && value != "true" {
        return;
    }
    ostd::mm::tlb::set_no_remote_flush(true);
    ostd::early_println!("ASTERINAS_TLB_NO_REMOTE_FLUSH status=enabled warning=diagnostic-only");
}
