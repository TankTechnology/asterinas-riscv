// SPDX-License-Identifier: MPL-2.0

//! `ftrace` events: function-level tracing.
//!
//! Unlike every other event here, this one is emitted by the compiler: a
//! kernel built with `-Z instrument-mcount` (see `build_kernel.sh
//! --function-trace`) puts a `call mcount` in every function prologue, and
//! `crate::trace::function::mcount` turns that into a record when this event
//! is on. The record carries the return address; the name is resolved when
//! the buffer is read, off the hot path.

use crate::trace::event::TraceEvent;

/// The subsystem name as it appears under `events/`.
pub const NAME: &str = "ftrace";

/// Emitted on entry to (almost) every function.
pub static FUNCTION: TraceEvent =
    TraceEvent::new(12, NAME, "function", "field:ip hex; field:func char[]; field:offset long");
