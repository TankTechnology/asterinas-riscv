// SPDX-License-Identifier: MPL-2.0

//! `profile` events: samples taken by the timer interrupt.

use crate::trace::event::TraceEvent;

/// The subsystem name as it appears under `events/`.
pub const NAME: &str = "profile";

/// A sampled program counter.
///
/// The payload is a raw address (8 bytes, little endian) followed by one byte
/// that says whether the interrupted context was user mode; the name is
/// resolved when the buffer is read, off the interrupt path. Turns into a
/// sampling profiler by enabling it: one sample per timer tick per CPU.
pub static SAMPLE: TraceEvent = TraceEvent::new(
    13,
    NAME,
    "sample",
    "field:pc hex; field:user bool",
);
