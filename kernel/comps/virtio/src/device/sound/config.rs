// SPDX-License-Identifier: MPL-2.0

//! Virtio-sound device configuration space and feature bits.
//!
//! The layout follows the VirtIO spec, section "Sound Device" (device ID 25).
//! Reference: <https://docs.oasis-open.org/virtio/virtio/v1.3/virtio-v1.3.html#x1-41600011>

use core::mem::offset_of;

use aster_util::safe_ptr::SafePtr;
use bitflags::bitflags;
use ostd_pod::FromZeros;

use crate::transport::{ConfigManager, VirtioTransport};

bitflags! {
    /// Device feature bits for virtio-sound.
    pub(super) struct SoundFeatures: u64 {
        /// The device supports control elements.
        const VIRTIO_SND_F_CTLS = 1 << 0;
    }
}

/// The virtio-sound configuration space (5.14.4).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct VirtioSoundConfig {
    /// Number of available physical jacks.
    pub jacks: u32,
    /// Number of available PCM streams.
    pub streams: u32,
    /// Number of available channel maps.
    pub chmaps: u32,
}

impl VirtioSoundConfig {
    pub(super) fn new_manager(transport: &dyn VirtioTransport) -> ConfigManager<Self> {
        let safe_ptr = transport
            .device_config_mem()
            .map(|mem| SafePtr::new(mem, 0));
        let bar_space = transport.device_config_bar();
        ConfigManager::new(safe_ptr, bar_space)
    }
}

impl ConfigManager<VirtioSoundConfig> {
    pub(super) fn read_config(&self) -> VirtioSoundConfig {
        let mut config = VirtioSoundConfig::new_zeroed();
        config.jacks = self
            .read_once::<u32>(offset_of!(VirtioSoundConfig, jacks))
            .unwrap();
        config.streams = self
            .read_once::<u32>(offset_of!(VirtioSoundConfig, streams))
            .unwrap();
        config.chmaps = self
            .read_once::<u32>(offset_of!(VirtioSoundConfig, chmaps))
            .unwrap();
        config
    }
}
