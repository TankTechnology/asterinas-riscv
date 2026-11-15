// SPDX-License-Identifier: MPL-2.0

//! `mm` events: memory management.

use crate::trace::event::TraceEvent;

/// The subsystem name as it appears under `events/`.
pub const NAME: &str = "mm";

/// Emitted when a user page fault enters the VMAR fault handler.
pub static PAGE_FAULT_USER: TraceEvent = TraceEvent::new(
    8,
    NAME,
    "page_fault_user",
    "field:address hex; field:write bool",
);
