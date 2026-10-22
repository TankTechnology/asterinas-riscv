// SPDX-License-Identifier: MPL-2.0

//! The ALSA PCM/control structures `aplay` and libasound use.
//!
//! Only the fields the minimal implementation reads or fills are meaningful;
//! the layouts match `<sound/asound.h>` so libasound can map its own
//! structures onto them.

use ostd_pod::{FromBytes, Immutable, IntoBytes, KnownLayout};

/// `SNDRV_PROTOCOL_VERSION(2, 0, 18)`.
pub(super) const SNDRV_PCM_VERSION: u32 = (2 << 16) | 18;
/// `SNDRV_PROTOCOL_VERSION(2, 0, 9)`.
pub(super) const SNDRV_CTL_VERSION: u32 = (2 << 16) | 9;

// ioctl type bytes.
pub(super) const SNDRV_PCM_MAGIC: u32 = b'A' as u32;
pub(super) const SNDRV_CTL_MAGIC: u32 = b'U' as u32;

// PCM ioctl numbers (`_IOC_NR`).
pub(super) const PCM_PVERSION: u32 = 0x00;
pub(super) const PCM_INFO: u32 = 0x01;
pub(super) const PCM_TSTAMP: u32 = 0x02;
pub(super) const PCM_TTSTAMP: u32 = 0x03;
pub(super) const PCM_USER_PVERSION: u32 = 0x04;
pub(super) const PCM_HW_REFINE: u32 = 0x10;
pub(super) const PCM_HW_PARAMS: u32 = 0x11;
pub(super) const PCM_HW_FREE: u32 = 0x12;
pub(super) const PCM_SW_PARAMS: u32 = 0x13;
pub(super) const PCM_HWSYNC: u32 = 0x22;
pub(super) const PCM_SYNC_PTR: u32 = 0x23;
pub(super) const PCM_STATUS: u32 = 0x20;
pub(super) const PCM_PREPARE: u32 = 0x40;
pub(super) const PCM_RESET: u32 = 0x41;
pub(super) const PCM_START: u32 = 0x42;
pub(super) const PCM_DROP: u32 = 0x43;
pub(super) const PCM_DRAIN: u32 = 0x44;
pub(super) const PCM_WRITEI_FRAMES: u32 = 0x50;
pub(super) const PCM_READI_FRAMES: u32 = 0x51;

// Control ioctl numbers.
pub(super) const CTL_PVERSION: u32 = 0x00;
pub(super) const CTL_CARD_INFO: u32 = 0x01;
pub(super) const CTL_PCM_NEXT_DEVICE: u32 = 0x30;
pub(super) const CTL_PCM_INFO: u32 = 0x31;
pub(super) const CTL_PCM_PREFER_SUBDEVICE: u32 = 0x32;

// Parameter ids.
pub(super) const HW_PARAM_ACCESS: usize = 0;
pub(super) const HW_PARAM_FORMAT: usize = 1;
pub(super) const HW_PARAM_SUBFORMAT: usize = 2;
pub(super) const HW_PARAM_SAMPLE_BITS: usize = 8;
pub(super) const HW_PARAM_FRAME_BITS: usize = 9;
pub(super) const HW_PARAM_CHANNELS: usize = 10;
pub(super) const HW_PARAM_RATE: usize = 11;
pub(super) const HW_PARAM_PERIOD_TIME: usize = 12;
pub(super) const HW_PARAM_PERIOD_SIZE: usize = 13;
pub(super) const HW_PARAM_PERIOD_BYTES: usize = 14;
pub(super) const HW_PARAM_PERIODS: usize = 15;
pub(super) const HW_PARAM_BUFFER_TIME: usize = 16;
pub(super) const HW_PARAM_BUFFER_SIZE: usize = 17;
pub(super) const HW_PARAM_BUFFER_BYTES: usize = 18;
pub(super) const HW_PARAM_FIRST_INTERVAL: usize = 8;
pub(super) const HW_PARAM_LAST_INTERVAL: usize = 19;

/// The only access type: interleaved read/write.
pub(super) const ACCESS_RW_INTERLEAVED: u32 = 3;
/// `SNDRV_PCM_FORMAT_S16_LE`.
pub(super) const FORMAT_S16_LE: u32 = 2;
/// `SNDRV_PCM_SUBFORMAT_STD`.
pub(super) const SUBFORMAT_STD: u32 = 0;

pub(super) const SAMPLE_BITS: u32 = 16;
pub(super) const FRAME_BITS: u32 = 32;
pub(super) const CHANNELS: u32 = 2;
pub(super) const RATE: u32 = 48_000;
/// Frames a single `WRITEI` may carry (one second of audio).
pub(super) const MAX_FRAMES: u64 = 48_000;

// States.
pub(super) const STATE_OPEN: i32 = 0;
pub(super) const STATE_SETUP: i32 = 1;
pub(super) const STATE_PREPARED: i32 = 2;
pub(super) const STATE_RUNNING: i32 = 3;
pub(super) const STATE_XRUN: i32 = 4;

/// `SNDRV_PCM_STREAM_PLAYBACK`.
pub(super) const STREAM_PLAYBACK: i32 = 0;

/// `struct snd_mask`.
#[repr(C)]
#[derive(Clone, Copy, Debug, FromBytes, Immutable, IntoBytes, KnownLayout)]
pub(super) struct SndMask {
    pub bits: [u32; 8],
}

impl SndMask {
    pub(super) fn test_bit(&self, bit: u32) -> bool {
        self.bits[(bit / 32) as usize] & (1 << (bit % 32)) != 0
    }

    pub(super) fn set_only(&mut self, bit: u32) -> bool {
        let changed = self.bits != single_bit(bit).bits;
        *self = single_bit(bit);
        changed
    }
}

pub(super) fn single_bit(bit: u32) -> SndMask {
    let mut mask = SndMask { bits: [0; 8] };
    mask.bits[(bit / 32) as usize] = 1 << (bit % 32);
    mask
}

/// `struct snd_interval`; the four low bits are `openmin/openmax/integer/empty`.
#[repr(C)]
#[derive(Clone, Copy, Debug, FromBytes, Immutable, IntoBytes, KnownLayout)]
pub(super) struct SndInterval {
    pub min: u32,
    pub max: u32,
    pub flags: u32,
}

impl SndInterval {
    pub(super) fn fixed(value: u32) -> Self {
        Self {
            min: value,
            max: value,
            flags: 1 << 2, // integer
        }
    }

    pub(super) fn is_fixed_at(&self, value: u32) -> bool {
        self.min == value && self.max == value
    }
}

/// `struct snd_pcm_hw_params` (528 bytes on 64-bit Linux).
#[repr(C)]
#[derive(Clone, Copy, Debug, FromBytes, Immutable, IntoBytes, KnownLayout)]
pub(super) struct SndPcmHwParams {
    pub flags: u32,
    pub masks: [SndMask; 3],
    pub mres: [SndMask; 5],
    pub intervals: [SndInterval; 12],
    pub ires: [SndInterval; 9],
    pub rmask: u32,
    pub cmask: u32,
    pub info: u32,
    pub msbits: u32,
    pub rate_num: u32,
    pub rate_den: u32,
    pub fifo_size: u64,
    pub sync: [u8; 16],
    pub reserved: [u8; 48],
}

/// `struct snd_pcm_sw_params`.
#[repr(C)]
#[derive(Clone, Copy, Debug, FromBytes, Immutable, IntoBytes, KnownLayout)]
pub(super) struct SndPcmSwParams {
    pub tstamp_mode: i32,
    pub period_step: u32,
    pub sleep_min: u32,
    /// Explicit padding: `avail_min` is 8-byte aligned.
    pub pad0: u32,
    pub avail_min: u64,
    pub xfer_align: u64,
    pub start_threshold: u64,
    pub stop_threshold: u64,
    pub silence_threshold: u64,
    pub silence_size: u64,
    pub boundary: u64,
    pub proto: u32,
    pub tstamp_type: u32,
    pub reserved: [u8; 56],
}

/// `struct snd_pcm_info`.
#[repr(C)]
#[derive(Clone, Copy, Debug, FromBytes, Immutable, IntoBytes, KnownLayout)]
pub(super) struct SndPcmInfo {
    pub device: u32,
    pub subdevice: u32,
    pub stream: i32,
    pub card: i32,
    pub id: [u8; 64],
    pub name: [u8; 80],
    pub subname: [u8; 32],
    pub dev_class: i32,
    pub dev_subclass: i32,
    pub subdevices_count: u32,
    pub subdevices_avail: u32,
    pub pad1: [u8; 16],
    pub reserved: [u8; 64],
}

/// `struct timespec` inside the status structure.
#[repr(C)]
#[derive(Clone, Copy, Debug, FromBytes, Immutable, IntoBytes, KnownLayout)]
pub(super) struct SndTimespec {
    pub tv_sec: i64,
    pub tv_nsec: i64,
}

/// `struct snd_pcm_status`.
#[repr(C)]
#[derive(Clone, Copy, Debug, FromBytes, Immutable, IntoBytes, KnownLayout)]
pub(super) struct SndPcmStatus {
    pub state: i32,
    pub pad1: u32,
    pub trigger_tstamp: SndTimespec,
    pub tstamp: SndTimespec,
    pub appl_ptr: u64,
    pub hw_ptr: u64,
    pub delay: i64,
    pub avail: u64,
    pub avail_max: u64,
    pub overrange: u64,
    pub suspended_state: i32,
    pub audio_tstamp_data: u32,
    pub audio_tstamp: SndTimespec,
    pub driver_tstamp: SndTimespec,
    pub audio_tstamp_accuracy: u32,
    pub reserved: [u8; 20],
}

/// `struct __snd_pcm_sync_ptr` (the 64-bit variant used by libasound).
#[repr(C)]
#[derive(Clone, Copy, Debug, FromBytes, Immutable, IntoBytes, KnownLayout)]
pub(super) struct SndPcmSyncPtr {
    pub flags: u32,
    pub status: [u8; 64],
    pub control: [u8; 64],
}

/// `struct snd_xferi`.
#[repr(C)]
#[derive(Clone, Copy, Debug, FromBytes, Immutable, IntoBytes, KnownLayout)]
pub(super) struct SndXferi {
    pub result: i64,
    pub buf: u64,
    pub frames: u64,
}

/// `struct snd_ctl_card_info`.
#[repr(C)]
#[derive(Clone, Copy, Debug, FromBytes, Immutable, IntoBytes, KnownLayout)]
pub(super) struct SndCtlCardInfo {
    pub card: i32,
    pub pad: i32,
    pub id: [u8; 16],
    pub driver: [u8; 16],
    pub name: [u8; 32],
    pub longname: [u8; 80],
    pub reserved: [u8; 16],
    pub mixername: [u8; 80],
    pub components: [u8; 128],
}

/// Copies `text` into a fixed-size NUL-padded name field.
pub(super) fn fill_name(field: &mut [u8], text: &str) {
    let bytes = text.as_bytes();
    let len = core::cmp::min(bytes.len(), field.len() - 1);
    field[..len].copy_from_slice(&bytes[..len]);
    field[len..].fill(0);
}
