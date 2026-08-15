// SPDX-License-Identifier: MPL-2.0

//! Manages virtio-sound devices.
//!
//! This module owns the global registry of discovered [`SoundDevice`] instances
//! and the wire types shared between the driver and the device. Virtio transport
//! initialization creates devices in [`device`], then registers them here.

use alloc::{collections::btree_map::BTreeMap, string::String, sync::Arc};

use ostd::sync::SpinLock;
use spin::Once;

use crate::device::sound::device::SoundDevice;

mod config;
pub mod device;

pub const DEVICE_NAME: &str = "Virtio-Sound";

/// Registers a [`SoundDevice`] under `name`.
fn register_device(name: String, device: Arc<SoundDevice>) {
    let mut sound_devs = SOUND_DEVICE_TABLE.get().unwrap().lock();
    sound_devs.insert(name, device);
}

/// Returns the first registered [`SoundDevice`].
pub fn first_device() -> Option<Arc<SoundDevice>> {
    let sound_devs = SOUND_DEVICE_TABLE.get().unwrap().lock();
    sound_devs.values().next().cloned()
}

/// Initializes the sound device registry.
pub(crate) fn init() {
    SOUND_DEVICE_TABLE.call_once(|| SpinLock::new(BTreeMap::new()));
}

static SOUND_DEVICE_TABLE: Once<SpinLock<BTreeMap<String, Arc<SoundDevice>>>> = Once::new();

/// Virtqueue indices (5.14.4).
pub const VQ_CONTROL: u16 = 0;
pub const VQ_EVENT: u16 = 1;
pub const VQ_TX: u16 = 2;
pub const VQ_RX: u16 = 3;

/// Common status codes (5.14.2).
pub const S_OK: u32 = 0x8000;
pub const S_BAD_MSG: u32 = 0x8001;
pub const S_NOT_SUPP: u32 = 0x8002;

/// Dataflow directions (5.14.2).
pub const D_OUTPUT: u8 = 0;
pub const D_INPUT: u8 = 1;

/// Control request codes (5.14.6).
pub const R_PCM_INFO: u32 = 0x0100;
pub const R_PCM_SET_PARAMS: u32 = 0x0101;
pub const R_PCM_PREPARE: u32 = 0x0102;
pub const R_PCM_RELEASE: u32 = 0x0103;
pub const R_PCM_START: u32 = 0x0104;
pub const R_PCM_STOP: u32 = 0x0105;

/// Sample format used for the initial MVP (S16LE).
pub const FMT_S16: u8 = 5;
/// Frame rate used for the initial MVP (48 kHz).
pub const RATE_48000: u8 = 7;

/// Common header (5.14.2).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioSndHdr {
    pub code: u32,
}

/// Common control request to query item information (5.14.6.1).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioSndQueryInfo {
    pub hdr: VirtioSndHdr,
    pub start_id: u32,
    pub count: u32,
    pub size: u32,
}

/// Common item information header (5.14.6.1).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioSndInfo {
    pub hda_fn_nid: u32,
}

/// PCM stream information (5.14.6.6.2.1).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioSndPcmInfo {
    pub hdr: VirtioSndInfo,
    pub features: u32,
    pub formats: u64,
    pub rates: u64,
    pub direction: u8,
    pub channels_min: u8,
    pub channels_max: u8,
    pub padding: [u8; 5],
}

/// PCM control header carrying a stream id (5.14.6.6).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioSndPcmHdr {
    pub hdr: VirtioSndHdr,
    pub stream_id: u32,
}

/// Set PCM stream format (5.14.6.6.2.2).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioSndPcmSetParams {
    pub hdr: VirtioSndPcmHdr,
    pub buffer_bytes: u32,
    pub period_bytes: u32,
    pub features: u32,
    pub channels: u8,
    pub format: u8,
    pub rate: u8,
    pub padding: u8,
}

/// I/O request header (5.14.6.7).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioSndPcmXfer {
    pub stream_id: u32,
}

/// I/O request status (5.14.6.7).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct VirtioSndPcmStatus {
    pub status: u32,
    pub latency_bytes: u32,
}
