// SPDX-License-Identifier: MPL-2.0

//! Virtio-gpu device configuration space and feature bits.
//!
//! The layout follows the VirtIO spec, section "GPU Device" (device ID 16).
//! Reference: <https://docs.oasis-open.org/virtio/virtio/v1.3/virtio-v1.3.html#x1-41500011>

use core::mem::offset_of;

use aster_util::safe_ptr::SafePtr;
use ostd_pod::FromZeros;

use crate::transport::{ConfigManager, VirtioTransport};

/// The virtio-gpu configuration space (5.7.4.4).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct VirtioGpuConfig {
    /// Signals pending events to the driver.
    pub events_read: u32,
    /// Clears pending events in the device.
    pub events_clear: u32,
    /// Maximum number of scanouts supported by the device.
    pub num_scanouts: u32,
    /// Number of virtio-gpu capability sets.
    pub num_capsets: u32,
}

impl VirtioGpuConfig {
    pub(super) fn new_manager(transport: &dyn VirtioTransport) -> ConfigManager<Self> {
        let safe_ptr = transport
            .device_config_mem()
            .map(|mem| SafePtr::new(mem, 0));
        let bar_space = transport.device_config_bar();
        ConfigManager::new(safe_ptr, bar_space)
    }
}

impl ConfigManager<VirtioGpuConfig> {
    pub(super) fn read_config(&self) -> VirtioGpuConfig {
        let mut config = VirtioGpuConfig::new_zeroed();
        config.events_read = self
            .read_once::<u32>(offset_of!(VirtioGpuConfig, events_read))
            .unwrap();
        config.events_clear = self
            .read_once::<u32>(offset_of!(VirtioGpuConfig, events_clear))
            .unwrap();
        config.num_scanouts = self
            .read_once::<u32>(offset_of!(VirtioGpuConfig, num_scanouts))
            .unwrap();
        config.num_capsets = self
            .read_once::<u32>(offset_of!(VirtioGpuConfig, num_capsets))
            .unwrap();
        config
    }
}
