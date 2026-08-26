// SPDX-License-Identifier: MPL-2.0

//! Universal Serial Bus (USB) host support.

#![no_std]
#![deny(unsafe_code)]

extern crate alloc;

use aster_logger as _;
use component::{ComponentInitError, init_component};

macro_rules! __log_prefix {
    () => {
        "usb: "
    };
}

#[cfg_attr(target_arch = "riscv64", path = "arch/riscv/mod.rs")]
#[cfg_attr(not(target_arch = "riscv64"), path = "arch/other.rs")]
mod arch;
mod keyboard;
mod mouse;

#[init_component]
fn init() -> Result<(), ComponentInitError> {
    arch::init();
    Ok(())
}

/// Runs the architecture-specific USB host polling worker.
pub fn run_polling() {
    arch::run_polling();
}
