// SPDX-License-Identifier: MPL-2.0

//! Retained kernel log records shared by `/dev/kmsg` and `syslog(2)`.
//!
//! Formatting, user copies, and reader wakeups must never run under the store
//! lock. In particular, emitting a log must not call back into the scheduler:
//! scheduler code can itself log while holding scheduler locks. Device and
//! syscall readers drain the pending flag to notify waiters out of band.

use core::{
    sync::atomic::{AtomicBool, AtomicU8, Ordering},
    time::Duration,
};

use ostd::{
    log::Record,
    sync::{LocalIrqDisabled, SpinLock},
    timer::Jiffies,
};

pub use self::store::{LogRecord, MESSAGE_CAPACITY, ReadError};

mod store;

/// Maximum retained records; storage is static and no allocation is lazy.
pub const RECORD_CAPACITY: usize = 512;

const CONSOLE_MAX_LEVEL: u8 = 8;
const CONSOLE_MIN_LEVEL: u8 = 1;

static KLOG: KernelLog = KernelLog {
    records: SpinLock::new(store::RecordRing::new()),
    pending: AtomicBool::new(false),
    restricted: AtomicBool::new(true),
    console_level: AtomicU8::new(CONSOLE_MAX_LEVEL),
    console_saved: AtomicU8::new(0),
};

/// Returns the shared kernel log.
pub fn klog() -> &'static KernelLog {
    &KLOG
}

/// A bounded log store accessible to device and syscall adapters.
pub struct KernelLog {
    records: SpinLock<store::RecordRing<RECORD_CAPACITY>, LocalIrqDisabled>,
    pending: AtomicBool,
    restricted: AtomicBool,
    console_level: AtomicU8,
    console_saved: AtomicU8,
}

impl KernelLog {
    /// Returns oldest, next-to-publish and clear-marker sequence numbers.
    pub fn bounds(&self) -> (u64, u64, u64) {
        self.records.lock().bounds()
    }

    /// Copies one record without holding the store lock during user access.
    pub fn record(&self, sequence: u64) -> Result<LogRecord, ReadError> {
        self.records.lock().record(sequence)
    }

    /// Appends a user-written message with an already validated priority.
    pub fn push(&self, priority: u16, message: &[u8]) {
        let timestamp = Jiffies::elapsed().as_duration();
        let prepared = LogRecord::new(priority, timestamp_us(&timestamp), message);
        self.publish(prepared);
    }

    /// Appends a logger record for later retrieval through `/dev/kmsg`.
    pub fn append(&self, record: &Record, timestamp: &Duration) {
        let prepared = LogRecord::formatted(
            record.level() as u16,
            timestamp_us(timestamp),
            format_args!("{}{}", record.prefix(), record.args()),
        );
        self.publish(prepared);
    }

    fn publish(&self, record: LogRecord) {
        let published = self.records.lock().push(record);
        if published {
            self.pending.store(true, Ordering::Release);
        }
    }

    /// Advances the non-destructive clear marker, without deleting records.
    pub fn clear_to(&self, sequence: u64) {
        self.records.lock().clear_to(sequence);
    }

    /// Consumes the coalesced reader-notification request.
    pub fn take_pending(&self) -> bool {
        self.pending.swap(false, Ordering::AcqRel)
    }

    /// Returns the configured message storage capacity in bytes.
    pub fn capacity(&self) -> usize {
        RECORD_CAPACITY * MESSAGE_CAPACITY
    }

    /// Returns whether reading kernel logs requires elevated capabilities.
    pub fn dmesg_restrict(&self) -> bool {
        self.restricted.load(Ordering::Relaxed)
    }

    /// Sets the privileged-read policy.
    pub fn set_dmesg_restrict(&self, restricted: bool) {
        self.restricted.store(restricted, Ordering::Relaxed);
    }

    /// Returns whether a record at `level` should be printed to the console.
    pub fn should_print(&self, level: ostd::log::Level) -> bool {
        (level as u8) < self.console_level.load(Ordering::Relaxed)
    }

    /// Changes console verbosity without changing capture verbosity.
    pub fn set_console_level(&self, level: u8) {
        self.console_level.store(
            level.min(CONSOLE_MAX_LEVEL).max(CONSOLE_MIN_LEVEL),
            Ordering::Relaxed,
        );
        self.console_saved.store(0, Ordering::Relaxed);
    }

    /// Saves console verbosity and enables only emergency console messages.
    pub fn disable_console(&self) {
        let current = self.console_level.load(Ordering::Relaxed);
        if self.console_saved.load(Ordering::Relaxed) == 0 {
            self.console_saved.store(current, Ordering::Relaxed);
        }
        self.console_level
            .store(CONSOLE_MIN_LEVEL, Ordering::Relaxed);
    }

    /// Restores the previously saved console verbosity, if any.
    pub fn enable_console(&self) {
        let saved = self.console_saved.swap(0, Ordering::Relaxed);
        if saved != 0 {
            self.console_level.store(saved, Ordering::Relaxed);
        }
    }
}

fn timestamp_us(timestamp: &Duration) -> u64 {
    // The ABI unit is microseconds; current OSTD jiffies have millisecond
    // resolution. Do not misrepresent this as a high-resolution tracing clock.
    timestamp.as_micros().min(u64::MAX as u128) as u64
}
