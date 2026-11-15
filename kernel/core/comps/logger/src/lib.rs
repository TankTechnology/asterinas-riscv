// SPDX-License-Identifier: MPL-2.0

//! The logger implementation for Asterinas.
//!
//! Logs are retained in a bounded record store. Capture and console verbosity
//! are independent; Linux-compatible device and syscall adapters read the store.
//! Different console colors are available with the `log_color` feature.
//!
//! This logger guarantees _atomicity_ under concurrency: messages are always
//! printed in their entirety without being mixed with messages generated
//! concurrently on other cores.
//!
//! IRQs are disabled while printing. So do not print long log messages.
#![no_std]
#![deny(unsafe_code)]

extern crate alloc;

use component::{ComponentInitError, init_component};

// Set this crate's log prefix for `ostd::log`.
macro_rules! __log_prefix {
    () => {
        "logger: "
    };
}

mod aster_logger;
mod console;
pub mod klog;

pub use console::_print;

#[init_component]
fn init() -> Result<(), ComponentInitError> {
    aster_logger::init();
    Ok(())
}
