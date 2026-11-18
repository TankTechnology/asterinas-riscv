// SPDX-License-Identifier: MPL-2.0

use device_id::DeviceId;

use crate::{
    device::{Device, DeviceType},
    prelude::*,
};

mod block;
pub mod char;

pub(super) fn init_in_first_kthread() {
    block::init_in_first_kthread();
}

pub(super) fn init_in_first_process() -> Result<()> {
    // Char devices register their devtmpfs nodes here for everything that was
    // registered before devtmpfs existed -- the virtual terminals, the console,
    // the framebuffer and the DRM node among them. Without this call those
    // devices exist in the registry but never appear under `/dev`.
    char::init_in_first_process()?;
    block::init_in_first_process()?;

    Ok(())
}

pub(crate) fn lookup(device_type: DeviceType, device_id: DeviceId) -> Option<Arc<dyn Device>> {
    match device_type {
        DeviceType::Char => char::lookup(device_id),
        DeviceType::Block => block::lookup(device_id),
    }
}
