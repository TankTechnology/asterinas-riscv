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
// Only the RISC-V USB host stack drives a boot keyboard for now.
#[cfg(target_arch = "riscv64")]
mod keyboard;
#[cfg(target_arch = "riscv64")]
mod mouse;

#[init_component]
fn init() -> Result<(), ComponentInitError> {
    arch::init();
    Ok(())
}

/// Returns how many USB host workers [`run_host`] can start.
///
/// A platform with one controller returns one; the Megrez board's two DWC3
/// controllers return two, so the kernel spawns one worker per controller.
pub fn host_count() -> usize {
    arch::host_count()
}

/// Runs the USB host worker for controller `index`.
pub fn run_host(index: usize) {
    arch::run_host(index)
}

/// Runs the first USB host worker.
pub fn run_polling() {
    arch::run_polling();
}
