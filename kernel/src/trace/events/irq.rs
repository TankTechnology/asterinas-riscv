// SPDX-License-Identifier: MPL-2.0

//! `irq` events: interrupt handler entry and exit.

use crate::trace::event::TraceEvent;

/// The subsystem name as it appears under `events/`.
pub const NAME: &str = "irq";

/// Emitted before the handlers of an interrupt line run.
pub static IRQ_HANDLER_ENTRY: TraceEvent =
    TraceEvent::new(6, NAME, "irq_handler_entry", "field:irq int");

/// Emitted after the handlers of an interrupt line return.
pub static IRQ_HANDLER_EXIT: TraceEvent =
    TraceEvent::new(7, NAME, "irq_handler_exit", "field:irq int");
