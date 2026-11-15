// SPDX-License-Identifier: MPL-2.0

//! `tracing` events: the ones the tracing filesystem itself writes.

use crate::trace::event::TraceEvent;

/// The subsystem name as it appears under `events/`.
pub const NAME: &str = "tracing";

/// Written by `echo <message> > trace_marker`.
///
/// The kernel never emits this one; the file's write path appends the record,
/// which is how a trace gets a "this is where my test begins" landmark.
pub static TRACE_MARKER: TraceEvent = TraceEvent::new(
    3,
    NAME,
    "trace_marker",
    "field:message char[]",
);
