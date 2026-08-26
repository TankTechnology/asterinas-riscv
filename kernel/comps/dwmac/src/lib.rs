// SPDX-License-Identifier: MPL-2.0

//! Synopsys DesignWare Ethernet MAC support.

#![no_std]
#![deny(unsafe_code)]

#[macro_use]
extern crate ostd_pod;

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
#[cfg(target_arch = "riscv64")]
mod device;

pub mod descriptor;
pub mod phy;
pub mod queue;
pub mod regs;
pub mod select;

#[init_component]
fn init() -> Result<(), ComponentInitError> {
    #[cfg(target_arch = "riscv64")]
    {
        let Some(platform) = arch::prepare().map_err(|error| {
            ostd::error!("EIC7700 platform initialization failed: {:?}", error);
            ComponentInitError::Unknown
        })?
        else {
            return Ok(());
        };
        match device::register(platform) {
            Ok(()) => {}
            Err(device::DeviceError::Platform(arch::PlatformError::Select(
                select::SelectError::NoLink,
            ))) => {
                ostd::warn!("no linked Megrez GMAC detected; Ethernet remains unavailable");
            }
            Err(error) => {
                ostd::error!("EIC7700 network registration failed: {:?}", error);
                return Err(ComponentInitError::Unknown);
            }
        }
    }
    #[cfg(not(target_arch = "riscv64"))]
    arch::initialize();
    Ok(())
}
