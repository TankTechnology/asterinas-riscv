// SPDX-License-Identifier: MPL-2.0

//! The events the kernel declares.
//!
//! Each subsystem is a module here; its events are statics that emitters refer
//! to directly, which is why the guard branch in `trace_event!` folds away to
//! nothing when the event is off.

pub mod block;
pub mod ftrace;
pub mod irq;
pub mod mm;
pub mod net;
pub mod profile;
pub mod sched;
pub mod syscall;
pub mod tracing;

/// Subsystems in the order they appear under `events/`.
pub const SUBSYSTEMS: &[&str] = &[
    block::NAME,
    ftrace::NAME,
    irq::NAME,
    mm::NAME,
    net::NAME,
    profile::NAME,
    sched::NAME,
    syscall::NAME,
    tracing::NAME,
];
