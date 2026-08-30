// SPDX-License-Identifier: MPL-2.0

//! Secure Digital and MultiMediaCard host support.

#![no_std]
#![deny(unsafe_code)]

extern crate alloc;

use core::sync::atomic::AtomicBool;

use aster_block::MajorIdOwner;
use aster_logger as _;
use component::{ComponentInitError, init_component};
use spin::Once;

macro_rules! __log_prefix {
    () => {
        "mmc: "
    };
}

#[cfg_attr(target_arch = "riscv64", path = "arch/riscv.rs")]
#[cfg_attr(not(target_arch = "riscv64"), path = "arch/other.rs")]
mod arch;
#[cfg(target_arch = "riscv64")]
mod block;
pub mod card;
pub mod sdhci;

static MMC_BLOCK_MAJOR_ID: Once<MajorIdOwner> = Once::new();
static MMC_WRITE_PARTITION2: AtomicBool = AtomicBool::new(false);
pub(crate) static MMC_BOUNDED_PIO: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("asterinas.mmc_write_partition2", MMC_WRITE_PARTITION2);
aster_cmdline::define_flag_param!("asterinas.mmc_bounded_pio", MMC_BOUNDED_PIO);

#[init_component]
fn init() -> Result<(), ComponentInitError> {
    #[cfg(target_arch = "riscv64")]
    {
        let Some((host, card)) = arch::probe().map_err(|error| {
            ostd::error!("[mmc] probe failed at {}: {:?}", error.stage(), error);
            ComponentInitError::Unknown
        })?
        else {
            return Ok(());
        };
        MMC_BLOCK_MAJOR_ID.call_once(|| aster_block::allocate_major().unwrap());
        block::register(host, card).map_err(|error| {
            ostd::error!("[mmc] block registration failed: {:?}", error);
            ComponentInitError::Unknown
        })?;
    }
    Ok(())
}
