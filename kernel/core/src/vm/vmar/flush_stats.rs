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
use core::sync::atomic::{AtomicBool, AtomicU32, Ordering};

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
    /// Copy-on-write of one page (`duplicate_frame`).
    ///
    /// This site has no explicit `dispatch_tlb_flush` in the kernel: the
    /// per-page `cursor.unmap(PAGE_SIZE)` it calls dispatches internally, in
    /// `ostd`'s `VmSpace::Cursor::unmap`. It counts faults, and each one costs
    /// one remote flush per CPU that maps the old frame.
    pub(crate) const COW_COPY: usize = 8;
}

pub(crate) const SITE_NAMES: [&str; 9] = [
    "ad",
    "cow_shared",
    "fresh",
    "unmap",
    "protect",
    "freeze",
    "remap",
    "fork",
    "cow_copy",
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

/// The shape of one whole-range operation (`munmap`, `mprotect`).
///
/// A range operation walks the mappings it intersects and, today, flushes once
/// per mapping. Batching those flushes into one only pays when an operation
/// covers several mappings, so the decision needs the distribution: this is
/// how many mappings an operation touched, and how long it held the page-table
/// subtree lock while doing so.
#[derive(Clone, Copy, Default)]
pub(crate) struct RangeOpShape {
    pub calls: u32,
    pub mappings: u32,
    pub max_mappings: u32,
    /// Longest single operation, in microseconds. A long hold is what makes a
    /// whole-range batch unacceptable: it would keep the page-table subtree
    /// locked across the entire loop.
    pub max_micros: u32,
}

static MUNMAP_SHAPE: [AtomicU32; 4] = [
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
];
static MPROTECT_SHAPE: [AtomicU32; 4] = [
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
];

fn record_shape(cells: &[AtomicU32; 4], mappings: u32, micros: u32) {
    cells[0].fetch_add(1, Ordering::Relaxed);
    cells[1].fetch_add(mappings, Ordering::Relaxed);
    cells[2].fetch_max(mappings, Ordering::Relaxed);
    cells[3].fetch_max(micros, Ordering::Relaxed);
}

fn load_shape(cells: &[AtomicU32; 4]) -> RangeOpShape {
    RangeOpShape {
        calls: cells[0].load(Ordering::Relaxed),
        mappings: cells[1].load(Ordering::Relaxed),
        max_mappings: cells[2].load(Ordering::Relaxed),
        max_micros: cells[3].load(Ordering::Relaxed),
    }
}

/// Records one `munmap`-style range operation.
pub(crate) fn record_munmap_shape(mappings: u32, micros: u32) {
    record_shape(&MUNMAP_SHAPE, mappings, micros);
}

/// Records one `mprotect`-style range operation.
pub(crate) fn record_mprotect_shape(mappings: u32, micros: u32) {
    record_shape(&MPROTECT_SHAPE, mappings, micros);
}

/// Returns `(munmap, mprotect)` range-operation shapes.
pub(crate) fn range_op_shapes() -> (RangeOpShape, RangeOpShape) {
    (load_shape(&MUNMAP_SHAPE), load_shape(&MPROTECT_SHAPE))
}

/// Converts a TSC delta to microseconds for [`RangeOpShape::max_micros`].
pub(crate) fn tsc_micros(ticks: u64) -> u32 {
    let frequency = ostd::arch::tsc_freq().max(1);
    (ticks.saturating_mul(1_000_000) / frequency).min(u32::MAX as u64) as u32
}

static NO_REMOTE_FLUSH: Once<String> = Once::new();
define_kv_param!("asterinas.tlb_no_remote_flush", NO_REMOTE_FLUSH);

/// `asterinas.sbi_ipi_serialize`: one hart at a time in the firmware send call.
///
/// Diagnostic mitigation while the board's failure under concurrent senders
/// is being pinned down; it costs one atomic per interrupt for a CPU-time
/// overhead well under one percent.
static SERIALIZE_IPI: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.sbi_ipi_serialize", SERIALIZE_IPI);

/// Applies `asterinas.tlb_no_remote_flush` to the TLB flusher.
///
/// This parameter exists to separate "the flush storm resets the board" from
/// "the board resets for another reason"; a kernel running with it is not
/// correct and says so on the console.
pub(crate) fn init_from_cmdline() {
    if SERIALIZE_IPI.load(Ordering::Relaxed) {
        ostd::smp::set_sbi_ipi_serialized(true);
        ostd::early_println!("ASTERINAS_SBI_IPI_SERIALIZED status=enabled");
    }
    let Some(value) = NO_REMOTE_FLUSH.get() else {
        return;
    };
    if value != "1" && value != "on" && value != "true" {
        return;
    }
    ostd::mm::tlb::set_no_remote_flush(true);
    ostd::early_println!("ASTERINAS_TLB_NO_REMOTE_FLUSH status=enabled warning=diagnostic-only");
}
