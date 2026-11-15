// SPDX-License-Identifier: MPL-2.0

//! Default-off syscall diagnostics.
//!
//! The per-thread diagnostic state is not part of the current `PosixThread`
//! layout, so the hooks are no-ops. The syscall path keeps calling them so the
//! integration points stay visible.

use crate::prelude::*;

/// Records the entry of a syscall.
pub(super) fn enter(_ctx: &Context, _syscall_number: u64, _args: [u64; 6]) {}

/// Records the return of a syscall.
pub(super) fn leave(_ctx: &Context, _return_value: isize) {}
