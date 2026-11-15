// SPDX-License-Identifier: MPL-2.0

//! `syscall` events: one record per system call entry and exit.

use crate::trace::event::TraceEvent;

/// The subsystem name as it appears under `events/`.
pub const NAME: &str = "syscall";

/// Emitted when a system call enters the kernel.
pub static SYS_ENTER: TraceEvent = TraceEvent::new(
    1,
    NAME,
    "sys_enter",
    "field:nr int; field:pid int; field:tid int",
);

/// Emitted when a system call returns, carrying the value the caller sees.
pub static SYS_EXIT: TraceEvent = TraceEvent::new(
    2,
    NAME,
    "sys_exit",
    "field:nr int; field:ret long; field:pid int; field:tid int",
);
