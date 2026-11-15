// SPDX-License-Identifier: MPL-2.0

//! Kernel tracing support.
//!
//! The first piece is the symbol table (`kallsyms`), which every later tracing
//! facility needs: an event or a sample carries an address, and turning that
//! into "which function" is a binary search over the table this module owns.

pub mod kallsyms;
