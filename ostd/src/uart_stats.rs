// SPDX-License-Identifier: MPL-2.0

//! How long console writes take.
//!
//! A console write holds the device lock with local IRQs disabled for its
//! whole duration. When a write stalls, every CPU waiting for a page-table
//! flush on that hart stalls with it, so the cost of one write is a
//! first-class piece of state. Reporting it through the heartbeat (which is
//! already printed) avoids adding a print from inside the console path,
//! where the report itself would be another long write.

use core::sync::atomic::{AtomicU8, AtomicU32, AtomicU64, Ordering};

/// Longest single console write since the last heartbeat, in microseconds.
static MAX_US: AtomicU32 = AtomicU32::new(0);
/// Number of console writes in the last heartbeat period.
static COUNT: AtomicU32 = AtomicU32::new(0);
/// Total microseconds spent inside console writes in this period. Divided by
/// the period, this is the fraction of the CPU's time spent with local IRQs
/// disabled on the console.
static SUM_US: AtomicU64 = AtomicU64::new(0);
/// Outcome of the slowest write: 0 ok, 1 busy, 2 timeout, 3 mmio.
static MAX_OUTCOME: AtomicU8 = AtomicU8::new(0);

/// Records one console write.
pub fn note_write(elapsed_us: u32, outcome: u8) {
    COUNT.fetch_add(1, Ordering::Relaxed);
    SUM_US.fetch_add(u64::from(elapsed_us), Ordering::Relaxed);
    let previous = MAX_US.fetch_max(elapsed_us, Ordering::Relaxed);
    if elapsed_us > previous {
        MAX_OUTCOME.store(outcome, Ordering::Relaxed);
    }
}

/// Returns `(max_us, count, max_outcome, sum_us)` and resets the period.
pub fn take_write_stats() -> (u32, u32, u8, u64) {
    (
        MAX_US.swap(0, Ordering::Relaxed),
        COUNT.swap(0, Ordering::Relaxed),
        MAX_OUTCOME.swap(0, Ordering::Relaxed),
        SUM_US.swap(0, Ordering::Relaxed),
    )
}

// ---------------------------------------------------------------------------
// The firmware console path (`early_println`): one machine-mode call per byte.
// It never touches the kernel's UART driver, so it never appears in the
// statistics above. Measured separately.

static FW_MAX_US: AtomicU32 = AtomicU32::new(0);
static FW_SUM_US: AtomicU64 = AtomicU64::new(0);
static FW_COUNT: AtomicU32 = AtomicU32::new(0);

/// Records one firmware-console write.
pub fn note_fw_write(elapsed_us: u32) {
    FW_COUNT.fetch_add(1, Ordering::Relaxed);
    FW_SUM_US.fetch_add(u64::from(elapsed_us), Ordering::Relaxed);
    FW_MAX_US.fetch_max(elapsed_us, Ordering::Relaxed);
}

/// Returns `(max_us, count, sum_us)` for the firmware console and resets it.
pub fn take_fw_write_stats() -> (u32, u32, u64) {
    (
        FW_MAX_US.swap(0, Ordering::Relaxed),
        FW_COUNT.swap(0, Ordering::Relaxed),
        FW_SUM_US.swap(0, Ordering::Relaxed),
    )
}
