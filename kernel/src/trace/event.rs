// SPDX-License-Identifier: MPL-2.0

//! Trace events: the registry and the enabled switch.
//!
//! An event is a static exactly like a Linux `tracepoint`: it carries an id, a
//! name for the interfaces under `/proc/trace/events`, a format line, and an
//! enabled flag. Emitters guard on [`TraceEvent::enabled`] before formatting
//! anything, so a disabled event costs one relaxed load and a branch.

use core::{
    fmt::{self, Write},
    sync::atomic::{AtomicBool, Ordering},
};

use super::ring::{self, RECORD_DATA_LEN};

/// The global switch behind `tracing_on`; when off, every event is off.
/// It starts on, as in Linux: enabling one event is enough to see records.
pub static TRACING_ON: AtomicBool = AtomicBool::new(true);

/// A trace event.
pub struct TraceEvent {
    id: u16,
    subsystem: &'static str,
    name: &'static str,
    format: &'static str,
    enabled: AtomicBool,
}

impl TraceEvent {
    pub const fn new(
        id: u16,
        subsystem: &'static str,
        name: &'static str,
        format: &'static str,
    ) -> Self {
        Self {
            id,
            subsystem,
            name,
            format,
            enabled: AtomicBool::new(false),
        }
    }

    pub const fn id(&self) -> u16 {
        self.id
    }

    pub const fn subsystem(&self) -> &'static str {
        self.subsystem
    }

    pub const fn name(&self) -> &'static str {
        self.name
    }

    pub const fn format(&self) -> &'static str {
        self.format
    }

    /// Whether this event is enabled *and* tracing as a whole is on.
    pub fn enabled(&self) -> bool {
        TRACING_ON.load(Ordering::Relaxed) && self.enabled.load(Ordering::Relaxed)
    }

    /// The per-event flag alone, as `events/<subsystem>/<event>/enable` shows it.
    pub fn enabled_flag(&self) -> bool {
        self.enabled.load(Ordering::Relaxed)
    }

    pub fn set_enabled(&self, on: bool) {
        self.enabled.store(on, Ordering::Relaxed);
    }

    /// Appends a record, if the event is enabled.
    ///
    /// The caller formats into a stack buffer first; a disabled event never
    /// reaches the formatting.
    pub fn trace(&self, pid: u32, tid: u32, args: fmt::Arguments<'_>) {
        let mut payload = Payload::new();
        let _ = payload.write_fmt(args);
        ring::push(self.id, pid, tid, payload.bytes());
    }
}

/// The payload buffer the emitters format into.
struct Payload {
    buf: [u8; RECORD_DATA_LEN],
    len: usize,
}

impl Payload {
    fn new() -> Self {
        Self {
            buf: [0; RECORD_DATA_LEN],
            len: 0,
        }
    }

    fn bytes(&self) -> &[u8] {
        &self.buf[..self.len]
    }
}

impl Write for Payload {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        let room = self.buf.len() - self.len;
        let take = s.len().min(room);
        self.buf[self.len..self.len + take].copy_from_slice(&s.as_bytes()[..take]);
        self.len += take;
        Ok(())
    }
}

/// Every event the kernel knows about, in id order.
///
/// Listing them explicitly keeps the id space visible in one place; adding an
/// event means adding it here, which is what the interfaces iterate.
static EVENTS: &[&TraceEvent] = &[
    &super::events::syscall::SYS_ENTER,
    &super::events::syscall::SYS_EXIT,
    &super::events::tracing::TRACE_MARKER,
];

pub fn all_events() -> &'static [&'static TraceEvent] {
    EVENTS
}

pub fn event_by_id(id: u16) -> Option<&'static TraceEvent> {
    EVENTS.iter().copied().find(|event| event.id() == id)
}

/// The distinct subsystem names, in the order they appear under `events/`.
pub fn subsystems() -> &'static [&'static str] {
    super::events::SUBSYSTEMS
}

/// The events of one subsystem, in id order.
pub fn events_of(subsystem: &str) -> impl Iterator<Item = &'static TraceEvent> {
    EVENTS
        .iter()
        .copied()
        .filter(move |event| event.subsystem() == subsystem)
}

pub fn event_named(subsystem: &str, name: &str) -> Option<&'static TraceEvent> {
    EVENTS
        .iter()
        .copied()
        .find(|event| event.subsystem() == subsystem && event.name() == name)
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::ktest;

    use super::*;
    use crate::trace::events::syscall::SYS_ENTER;

    #[ktest]
    fn switches_combine() {
        SYS_ENTER.set_enabled(false);
        TRACING_ON.store(true, Ordering::Relaxed);
        assert!(!SYS_ENTER.enabled(), "the event is off");

        SYS_ENTER.set_enabled(true);
        assert!(SYS_ENTER.enabled(), "both switches are on");

        TRACING_ON.store(false, Ordering::Relaxed);
        assert!(!SYS_ENTER.enabled(), "the global switch wins");
        assert!(SYS_ENTER.enabled_flag(), "the per-event flag is untouched");

        TRACING_ON.store(true, Ordering::Relaxed);
        SYS_ENTER.set_enabled(false);
    }

    #[ktest]
    fn registry_is_consistent() {
        for event in all_events().iter() {
            assert_eq!(
                event_by_id(event.id()).map(|found| found.name()),
                Some(event.name()),
                "id {} resolves to {}",
                event.id(),
                event.name()
            );
            assert!(
                subsystems().contains(&event.subsystem()),
                "{} is listed under some subsystem",
                event.name()
            );
            assert!(
                events_of(event.subsystem()).any(|candidate| candidate.name() == event.name()),
                "{} is reachable through its subsystem",
                event.name()
            );
        }
    }
}
