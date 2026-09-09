// SPDX-License-Identifier: MPL-2.0

//! Bounded log records, independent sequence lookup, and console policy.
//!
//! This module has no allocation, locking or OSTD dependency. The owning logger
//! serializes mutations and never hands out references into the live ring.

use core::fmt::{self, Write};

/// Maximum retained message bytes, excluding metadata and output prefixes.
pub const MESSAGE_CAPACITY: usize = 1024;
const TRUNCATION: &[u8] = b"[truncated]";
const CONSOLE_MIN_LEVEL: u8 = 1;
const CONSOLE_MAX_LEVEL: u8 = 8;

/// The default upstream capture threshold, independent of console output.
pub(crate) const DEFAULT_CAPTURE_LEVEL_FILTER: u8 = 5;

/// Parses the Linux log-level spellings accepted by `asterinas.klog_capture`.
pub(crate) fn parse_level_filter(value: &str) -> Option<u8> {
    match value {
        "0" | "off" => Some(0),
        "1" | "emerg" => Some(1),
        "2" | "alert" => Some(2),
        "3" | "crit" => Some(3),
        "4" | "error" | "err" => Some(4),
        "5" | "warning" | "warn" => Some(5),
        "6" | "notice" => Some(6),
        "7" | "info" => Some(7),
        "8" | "debug" => Some(8),
        _ => None,
    }
}

/// Keeps the upstream capture filter at least as verbose as the console.
pub(crate) fn effective_level_filter(console: u8, capture: Option<u8>) -> u8 {
    console.max(capture.unwrap_or(DEFAULT_CAPTURE_LEVEL_FILTER))
}

/// A bounded message formatter which explicitly marks truncation.
#[derive(Clone, Copy)]
pub struct Message {
    bytes: [u8; MESSAGE_CAPACITY],
    len: usize,
    truncated: bool,
}

impl Message {
    pub const fn new() -> Self {
        Self {
            bytes: [0; MESSAGE_CAPACITY],
            len: 0,
            truncated: false,
        }
    }

    fn append(&mut self, bytes: &[u8]) -> fmt::Result {
        if self.truncated {
            return Err(fmt::Error);
        }
        let copied = bytes.len().min(MESSAGE_CAPACITY - self.len);
        self.bytes[self.len..self.len + copied].copy_from_slice(&bytes[..copied]);
        self.len += copied;
        if copied < bytes.len() {
            self.truncated = true;
            let start = MESSAGE_CAPACITY - TRUNCATION.len();
            self.bytes[start..].copy_from_slice(TRUNCATION);
            return Err(fmt::Error);
        }
        Ok(())
    }

    fn bytes(&self) -> &[u8] {
        &self.bytes[..self.len]
    }

    fn strip_newline(&mut self) {
        if self.bytes().last() == Some(&b'\n') {
            self.len -= 1;
        }
    }
}

impl Write for Message {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        self.append(s.as_bytes())
    }
}

impl fmt::Display for Message {
    fn fmt(&self, writer: &mut fmt::Formatter<'_>) -> fmt::Result {
        write_text(writer, self.bytes())
    }
}

fn write_text(writer: &mut impl Write, bytes: &[u8]) -> fmt::Result {
    match core::str::from_utf8(bytes) {
        Ok(text) => writer.write_str(text),
        Err(_) => {
            for &byte in bytes {
                if byte.is_ascii() {
                    writer.write_char(byte as char)?;
                } else {
                    write!(writer, "\\x{byte:02x}")?;
                }
            }
            Ok(())
        }
    }
}

/// A complete log record, copied independently of its live storage slot.
#[derive(Clone, Copy)]
pub struct LogRecord {
    sequence: u64,
    timestamp_us: u64,
    priority: u16,
    message: Message,
}

impl LogRecord {
    const EMPTY: Self = Self {
        sequence: 0,
        timestamp_us: 0,
        priority: 0,
        message: Message::new(),
    };

    /// Creates a record from bytes, removing one final newline.
    pub(super) fn new(priority: u16, timestamp_us: u64, bytes: &[u8]) -> Self {
        let mut message = Message::new();
        let _ = message.append(bytes);
        Self::from_message(priority, timestamp_us, message)
    }

    /// Creates a record from an already bounded message.
    pub(super) fn from_message(priority: u16, timestamp_us: u64, mut message: Message) -> Self {
        message.strip_newline();
        Self {
            sequence: 0,
            timestamp_us,
            priority,
            message,
        }
    }

    /// Evaluates arguments once for both retained and console output.
    pub(super) fn formatted(priority: u16, timestamp_us: u64, args: fmt::Arguments<'_>) -> Self {
        let mut message = Message::new();
        let _ = message.write_fmt(args);
        Self::from_message(priority, timestamp_us, message)
    }

    pub(crate) fn message(&self) -> impl fmt::Display + '_ {
        &self.message
    }

    /// Returns the record's position in the log sequence.
    pub fn sequence(&self) -> u64 {
        self.sequence
    }

    /// Formats one Linux `/dev/kmsg` record, including byte escaping.
    ///
    /// Reference: <https://www.kernel.org/doc/Documentation/ABI/testing/dev-kmsg>.
    pub fn format_kmsg(&self, writer: &mut impl Write) -> fmt::Result {
        write!(
            writer,
            "{},{},{},-;",
            self.priority, self.sequence, self.timestamp_us
        )?;
        for &byte in self.message.bytes() {
            if (b' '..=b'~').contains(&byte) && byte != b'\\' {
                writer.write_char(byte as char)?;
            } else {
                write!(writer, "\\x{byte:02x}")?;
            }
        }
        writer.write_char('\n')
    }

    /// Formats the legacy syslog text with a prefix on each message line.
    pub fn format_syslog(&self, writer: &mut impl Write) -> fmt::Result {
        for line in self.message.bytes().split(|byte| *byte == b'\n') {
            write!(
                writer,
                "<{}>[{:>5}.{:06}] ",
                self.priority,
                self.timestamp_us / 1_000_000,
                self.timestamp_us % 1_000_000
            )?;
            // Kernel formatters produce UTF-8. Preserve it, escaping arbitrary
            // bytes accepted from user-written records instead of allocating.
            write_text(writer, line)?;
            writer.write_char('\n')?;
        }
        Ok(())
    }

    /// Writes legacy syslog bytes, preserving non-UTF-8 user messages.
    ///
    /// Unlike text formatting, this is suitable for the syscall byte ABI.
    pub fn write_syslog(&self, mut write_bytes_fn: impl FnMut(&[u8])) {
        let mut prefix = Message::new();
        let result = write!(
            prefix,
            "<{}>[{:>5}.{:06}] ",
            self.priority,
            self.timestamp_us / 1_000_000,
            self.timestamp_us % 1_000_000
        );
        debug_assert!(result.is_ok());
        for line in self.message.bytes().split(|byte| *byte == b'\n') {
            write_bytes_fn(prefix.bytes());
            write_bytes_fn(line);
            write_bytes_fn(b"\n");
        }
    }
}

/// A record lookup which cannot return a live record.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReadError {
    /// No record has been published at this position.
    Empty,
    /// The requested record was overwritten; contains the oldest live sequence.
    Overrun(u64),
}

/// A fixed-slot record ring with a separate non-destructive clear marker.
pub struct RecordRing<const N: usize> {
    records: [LogRecord; N],
    first: u64,
    next: u64,
    cleared: u64,
}

impl<const N: usize> RecordRing<N> {
    pub const fn new() -> Self {
        assert!(N > 0);
        Self {
            records: [LogRecord::EMPTY; N],
            first: 0,
            next: 0,
            cleared: 0,
        }
    }

    /// Publishes a complete record, returning false on sequence exhaustion.
    pub fn push(&mut self, mut record: LogRecord) -> bool {
        let Some(next) = self.next.checked_add(1) else {
            // Never alias a previously issued record identity.
            return false;
        };
        record.sequence = self.next;
        self.records[(self.next % N as u64) as usize] = record;
        self.next = next;
        self.first = self.next.saturating_sub(N as u64);
        true
    }

    /// Returns oldest, next-to-publish and clear-marker sequence numbers.
    pub fn bounds(&self) -> (u64, u64, u64) {
        (self.first, self.next, self.cleared)
    }

    /// Copies a retained record without consuming it for another reader.
    pub fn record(&self, sequence: u64) -> Result<LogRecord, ReadError> {
        if sequence < self.first {
            return Err(ReadError::Overrun(self.first));
        }
        if sequence >= self.next {
            return Err(ReadError::Empty);
        }
        Ok(self.records[(sequence % N as u64) as usize])
    }

    /// Advances only the clear marker, bounded by the published sequence.
    pub fn clear_to(&mut self, sequence: u64) {
        self.cleared = self.cleared.max(sequence.min(self.next));
    }
}

/// Console verbosity and its saved value for syslog off/on operations.
pub struct ConsolePolicy {
    level: u8,
    saved: Option<u8>,
}

impl ConsolePolicy {
    pub const fn new(level: u8) -> Self {
        Self { level, saved: None }
    }

    pub fn level(&self) -> u8 {
        self.level
    }

    pub fn set_level(&mut self, level: u8) {
        self.level = level.min(CONSOLE_MAX_LEVEL);
        self.saved = None;
    }

    pub fn disable(&mut self) {
        self.saved.get_or_insert(self.level);
        self.level = CONSOLE_MIN_LEVEL;
    }

    pub fn enable(&mut self) {
        if let Some(saved) = self.saved.take() {
            self.level = saved;
        }
    }

    pub fn should_print(&self, priority: u8) -> bool {
        priority < self.level
    }
}
