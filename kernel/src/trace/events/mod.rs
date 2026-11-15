// SPDX-License-Identifier: MPL-2.0

//! The events the kernel declares.
//!
//! Each subsystem is a module here; its events are statics that emitters refer
//! to directly, which is why the guard branch in `trace_event!` folds away to
//! nothing when the event is off.

pub mod syscall;
pub mod tracing;

/// Subsystems in the order they appear under `events/`.
pub const SUBSYSTEMS: &[&str] = &[syscall::NAME, tracing::NAME];
