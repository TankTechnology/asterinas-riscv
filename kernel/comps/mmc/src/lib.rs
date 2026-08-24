// SPDX-License-Identifier: MPL-2.0

//! Secure Digital and MultiMediaCard host support.

#![no_std]
#![deny(unsafe_code)]

extern crate alloc;

use aster_logger as _;
use component::{ComponentInitError, init_component};

pub mod sdhci;

#[init_component]
fn init() -> Result<(), ComponentInitError> {
    Ok(())
}
