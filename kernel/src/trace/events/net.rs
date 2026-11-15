// SPDX-License-Identifier: MPL-2.0

//! `net` events.
//!
//! A poll of the interface is the granularity the kernel controls: the packet
//! loops live in `aster-bigtcp`, which is a library and cannot see the kernel's
//! trace events. Per-packet `net_rx`/`net_tx` would need a hook injected into
//! that crate, which is a follow-up; `net_poll` already answers "which
//! interface was polled, when, and how often".

use crate::trace::event::TraceEvent;

/// The subsystem name as it appears under `events/`.
pub const NAME: &str = "net";

/// Emitted when the kernel polls an interface.
pub static NET_POLL: TraceEvent = TraceEvent::new(9, NAME, "net_poll", "field:iface char[]");
