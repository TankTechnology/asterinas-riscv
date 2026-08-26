// SPDX-License-Identifier: MPL-2.0

//! Synopsys DesignWare Ethernet MAC support.

#![no_std]
#![deny(unsafe_code)]

use component::{ComponentInitError, init_component};

pub mod descriptor;
pub mod regs;

#[init_component]
fn init() -> Result<(), ComponentInitError> {
    Ok(())
}
