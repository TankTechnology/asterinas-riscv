// SPDX-License-Identifier: MPL-2.0

//! Synopsys DesignWare Ethernet MAC support.

#![no_std]
#![deny(unsafe_code)]

macro_rules! __log_prefix {
    () => {
        "dwmac: "
    };
}

use aster_logger as _;
use component::{ComponentInitError, init_component};

#[cfg_attr(target_arch = "riscv64", path = "arch/riscv.rs")]
#[cfg_attr(not(target_arch = "riscv64"), path = "arch/other.rs")]
mod arch;

pub mod descriptor;
pub mod phy;
pub mod regs;
pub mod select;

#[init_component]
fn init() -> Result<(), ComponentInitError> {
    #[cfg(target_arch = "riscv64")]
    arch::initialize().map_err(|error| {
        ostd::error!("EIC7700 platform initialization failed: {:?}", error);
        ComponentInitError::Unknown
    })?;
    #[cfg(not(target_arch = "riscv64"))]
    arch::initialize();
    Ok(())
}
