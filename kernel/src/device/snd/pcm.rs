// SPDX-License-Identifier: MPL-2.0

//! ALSA PCM ioctl ABI (playback), transcribed from `sound/asound.h`.
//!
//! The structs below are `#[repr(C)]` `Pod` types whose layout matches the
//! LP64 (riscv64/x86_64) layout of the corresponding `asound.h` structures
//! byte-for-byte. The ioctl commands are defined with [`ioc`] using the modern
//! `_IOR`/`_IOW`/`_IOWR`/`_IO` encoding (magic `'A'`), so [`dispatch_ioctl`]
//! maps them directly onto the ABI userspace expects.
//!
//! Reference: `include/uapi/sound/asound.h` (SNDRV_PCM_VERSION 2.0.17).

use crate::prelude::*;

/// The ALSA PCM protocol version we report (`SNDRV_PROTOCOL_VERSION(2, 0, 17)`).
pub const SNDRV_PCM_VERSION: i32 = 0x20011;

// Stream directions (`SNDRV_PCM_STREAM_*`).
pub const SNDRV_PCM_STREAM_PLAYBACK: i32 = 0;
#[expect(dead_code)]
pub const SNDRV_PCM_STREAM_CAPTURE: i32 = 1;

// Device classes (`SNDRV_PCM_CLASS_*` / `SNDRV_PCM_SUBCLASS_*`).
pub const SNDRV_PCM_CLASS_GENERIC: i32 = 0;
pub const SNDRV_PCM_SUBCLASS_GENERIC_MIX: i32 = 0;

// Access types (`SNDRV_PCM_ACCESS_*`).
pub const SNDRV_PCM_ACCESS_RW_INTERLEAVED: u32 = 3;

// Sample formats (`SNDRV_PCM_FORMAT_*`).
pub const SNDRV_PCM_FORMAT_S16_LE: u32 = 2;

// Subformat (`SNDRV_PCM_SUBFORMAT_STD`).
pub const SNDRV_PCM_SUBFORMAT_STD: u32 = 0;

// Hardware info flags (`SNDRV_PCM_INFO_*`). We deliberately do not advertise
// `MMAP` so libasound uses the `writei` (ioctl) data path.
pub const SNDRV_PCM_INFO_INTERLEAVED: u32 = 0x100;

// Stream states (`SNDRV_PCM_STATE_*`).
pub const SNDRV_PCM_STATE_OPEN: i32 = 0;
pub const SNDRV_PCM_STATE_SETUP: i32 = 1;
pub const SNDRV_PCM_STATE_PREPARED: i32 = 2;
pub const SNDRV_PCM_STATE_RUNNING: i32 = 3;
#[expect(dead_code)]
pub const SNDRV_PCM_STATE_XRUN: i32 = 4;
#[expect(dead_code)]
pub const SNDRV_PCM_STATE_DRAINING: i32 = 5;

// `snd_pcm_hw_param_t` indices for masks (also the `masks` array index).
pub const HW_PARAM_ACCESS: usize = 0;
pub const HW_PARAM_FORMAT: usize = 1;
pub const HW_PARAM_SUBFORMAT: usize = 2;
// Array indices into `snd_pcm_hw_params.intervals`. The `intervals` array is
// indexed by `hw_param - SNDRV_PCM_HW_PARAM_FIRST_INTERVAL`, so these are the
// `snd_pcm_hw_param_t` values shifted down by `SAMPLE_BITS` (8).
pub const HW_PARAM_SAMPLE_BITS: usize = 0;
pub const HW_PARAM_FRAME_BITS: usize = 1;
pub const HW_PARAM_CHANNELS: usize = 2;
pub const HW_PARAM_RATE: usize = 3;
pub const HW_PARAM_PERIOD_SIZE: usize = 5;
pub const HW_PARAM_PERIOD_BYTES: usize = 6;
pub const HW_PARAM_PERIODS: usize = 7;
pub const HW_PARAM_BUFFER_SIZE: usize = 9;
pub const HW_PARAM_BUFFER_BYTES: usize = 10;

/// The single hardware configuration we support (mirrors the virtio-sound
/// driver's hardcoded `SET_PARAMS`).
pub const DEV_CHANNELS: u32 = 2;
pub const DEV_RATE: u32 = 48_000;
pub const DEV_FORMAT: u32 = SNDRV_PCM_FORMAT_S16_LE;
pub const DEV_BUFFER_BYTES: u32 = 8192;
pub const DEV_PERIOD_BYTES: u32 = 2048;
/// Bytes per frame (2 channels × 2 bytes/sample for S16).
pub const DEV_FRAME_BYTES: u32 = 4;
pub const DEV_BUFFER_FRAMES: u32 = DEV_BUFFER_BYTES / DEV_FRAME_BYTES;
pub const DEV_PERIOD_FRAMES: u32 = DEV_PERIOD_BYTES / DEV_FRAME_BYTES;

/// `struct timespec` (LP64: two `long`s).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct SndTimespec {
    pub tv_sec: i64,
    pub tv_nsec: i64,
}

/// `struct snd_interval` — `{ unsigned int min, max; unsigned int openmin:1,
/// openmax:1, integer:1, empty:1; }` (12 bytes).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct SndInterval {
    pub min: u32,
    pub max: u32,
    /// Packed `openmin`/`openmax`/`integer`/`empty` bitfield (bit 2 = integer).
    pub flags: u32,
}

impl SndInterval {
    /// A single closed integer value `v`.
    pub fn single(v: u32) -> Self {
        Self {
            min: v,
            max: v,
            flags: 0x4, // integer = 1
        }
    }
}

/// `struct snd_mask` — `__u32 bits[8]` (32 bytes).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct SndMask(pub [u32; 8]);

impl SndMask {
    pub fn single(bit: u32) -> Self {
        let mut m = Self([0; 8]);
        m.0[(bit / 32) as usize] |= 1 << (bit % 32);
        m
    }
}

/// `struct snd_pcm_info` (288 bytes on LP64).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct SndPcmInfo {
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
    pub sync: [u8; 16],
    pub reserved: [u8; 64],
}

/// `struct snd_pcm_hw_params` (608 bytes on LP64).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct SndPcmHwParams {
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
    pub reserved: [u8; 64],
}

/// `struct snd_pcm_sw_params` (136 bytes on LP64).
#[padding_struct]
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct SndPcmSwParams {
    pub tstamp_mode: i32,
    pub period_step: u32,
    pub sleep_min: u32,
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

/// `struct snd_pcm_status` (152 bytes on LP64).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct SndPcmStatus {
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

/// `struct snd_pcm_channel_info` (24 bytes on LP64).
#[padding_struct]
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct SndPcmChannelInfo {
    pub channel: u32,
    pub offset: u64,
    pub first: u32,
    pub step: u32,
}

/// `struct snd_xferi` — the `SNDRV_PCM_IOCTL_WRITEI_FRAMES` argument (24 bytes).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct SndXferi {
    pub result: i64,
    pub buf: u64,
    pub frames: u64,
}

/// `struct snd_pcm_mmap_status64` (56 bytes on LP64) — the status half of the
/// `SNDRV_PCM_IOCTL_SYNC_PTR` argument (also the mmap'd status page).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct SndPcmMmapStatus {
    pub state: i32,
    pub pad1: u32,
    pub hw_ptr: u64,
    pub tstamp: SndTimespec,
    pub suspended_state: i32,
    pub pad3: u32,
    pub audio_tstamp: SndTimespec,
}

/// `struct snd_pcm_mmap_control64` (16 bytes on LP64) — the control half of the
/// `SNDRV_PCM_IOCTL_SYNC_PTR` argument.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct SndPcmMmapControl {
    pub appl_ptr: u64,
    pub avail_min: u64,
}

/// `struct snd_pcm_sync_ptr64` (136 bytes on LP64) — the
/// `SNDRV_PCM_IOCTL_SYNC_PTR` argument: `flags` plus a 64-byte status union and
/// a 64-byte control union. `libasound` issues this even on the writei path to
/// push its `appl_ptr` and pull back `state`/`hw_ptr`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct SndPcmSyncPtr {
    pub flags: u32,
    pub pad1: u32,
    pub status: SndPcmMmapStatus,
    pub status_pad: [u8; 8],
    pub control: SndPcmMmapControl,
    pub control_pad: [u8; 48],
}

pub(crate) mod ioctl_defs {
    use super::{
        SndPcmChannelInfo, SndPcmHwParams, SndPcmInfo, SndPcmStatus, SndPcmSwParams, SndPcmSyncPtr,
        SndXferi,
    };
    use crate::util::ioctl::{InData, InOutData, NoData, OutData, ioc};

    // Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/sound/asound.h#L666-L695>
    pub(crate) type Pversion = ioc!(SNDRV_PCM_IOCTL_PVERSION, b'A', 0x00, OutData<i32>);
    pub(crate) type Info = ioc!(SNDRV_PCM_IOCTL_INFO, b'A', 0x01, OutData<SndPcmInfo>);
    pub(crate) type Tstamp = ioc!(SNDRV_PCM_IOCTL_TSTAMP, b'A', 0x02, InData<i32>);
    pub(crate) type Ttstamp = ioc!(SNDRV_PCM_IOCTL_TTSTAMP, b'A', 0x03, InData<i32>);
    pub(crate) type UserPversion = ioc!(SNDRV_PCM_IOCTL_USER_PVERSION, b'A', 0x04, InData<i32>);
    pub(crate) type HwRefine = ioc!(SNDRV_PCM_IOCTL_HW_REFINE, b'A', 0x10, InOutData<SndPcmHwParams>);
    pub(crate) type HwParams = ioc!(SNDRV_PCM_IOCTL_HW_PARAMS, b'A', 0x11, InOutData<SndPcmHwParams>);
    pub(crate) type HwFree = ioc!(SNDRV_PCM_IOCTL_HW_FREE, b'A', 0x12, NoData);
    pub(crate) type SwParams = ioc!(SNDRV_PCM_IOCTL_SW_PARAMS, b'A', 0x13, InOutData<SndPcmSwParams>);
    pub(crate) type Status = ioc!(SNDRV_PCM_IOCTL_STATUS, b'A', 0x20, OutData<SndPcmStatus>);
    pub(crate) type Delay = ioc!(SNDRV_PCM_IOCTL_DELAY, b'A', 0x21, OutData<i64>);
    pub(crate) type ChannelInfo = ioc!(SNDRV_PCM_IOCTL_CHANNEL_INFO, b'A', 0x32, OutData<SndPcmChannelInfo>);
    pub(crate) type Prepare = ioc!(SNDRV_PCM_IOCTL_PREPARE, b'A', 0x40, NoData);
    pub(crate) type Start = ioc!(SNDRV_PCM_IOCTL_START, b'A', 0x42, NoData);
    pub(crate) type Drop = ioc!(SNDRV_PCM_IOCTL_DROP, b'A', 0x43, NoData);
    pub(crate) type Drain = ioc!(SNDRV_PCM_IOCTL_DRAIN, b'A', 0x44, NoData);
    pub(crate) type WriteiFrames = ioc!(SNDRV_PCM_IOCTL_WRITEI_FRAMES, b'A', 0x50, InData<SndXferi>);
    pub(crate) type SyncPtr = ioc!(SNDRV_PCM_IOCTL_SYNC_PTR, b'A', 0x23, InOutData<SndPcmSyncPtr>);
}

/// Negotiated hardware parameters for the playback stream.
#[derive(Clone, Copy, Debug)]
pub struct PcmParams {
    pub channels: u32,
    #[expect(dead_code)]
    pub rate: u32,
    #[expect(dead_code)]
    pub format: u32,
    pub buffer_frames: u32,
    #[expect(dead_code)]
    pub period_frames: u32,
}

/// Per-open ALSA PCM stream state (mirrors the `SNDRV_PCM_STATE_*` machine).
#[derive(Debug)]
pub struct PcmStream {
    state: i32,
    params: Option<PcmParams>,
    hw_ptr: u64,
    started: bool,
}

impl PcmStream {
    pub fn new() -> Self {
        Self {
            state: SNDRV_PCM_STATE_OPEN,
            params: None,
            hw_ptr: 0,
            started: false,
        }
    }

    #[expect(dead_code)]
    pub fn state(&self) -> i32 {
        self.state
    }

    pub fn params(&self) -> Option<PcmParams> {
        self.params
    }

    /// Builds the `SNDRV_PCM_IOCTL_INFO` reply.
    pub fn build_info(&self) -> SndPcmInfo {
        let mut info = SndPcmInfo::new_zeroed();
        info.device = 0;
        info.subdevice = 0;
        info.stream = SNDRV_PCM_STREAM_PLAYBACK;
        info.card = 0;
        info.dev_class = SNDRV_PCM_CLASS_GENERIC;
        info.dev_subclass = SNDRV_PCM_SUBCLASS_GENERIC_MIX;
        info.subdevices_count = 1;
        info.subdevices_avail = 1;
        let id = b"virtio-sound";
        info.id[..id.len()].copy_from_slice(id);
        let name = b"Asterinas virtio-sound playback";
        info.name[..name.len()].copy_from_slice(name);
        let subname = b"pcmC0D0p";
        info.subname[..subname.len()].copy_from_slice(subname);
        info
    }

    /// Applies (and constrains) a `HW_REFINE`/`HW_PARAMS` request, returning the
    /// constrained copy to hand back to userspace.
    ///
    /// We support exactly one configuration (S16LE / 48 kHz / stereo); any
    /// request is clamped to it, so a single-valued or range request alike
    /// converges to the same point.
    pub fn apply_hw_params(&mut self, p: &SndPcmHwParams) -> SndPcmHwParams {
        let mut out = *p;
        out.masks[HW_PARAM_ACCESS] = SndMask::single(SNDRV_PCM_ACCESS_RW_INTERLEAVED);
        out.masks[HW_PARAM_FORMAT] = SndMask::single(DEV_FORMAT);
        out.masks[HW_PARAM_SUBFORMAT] = SndMask::single(SNDRV_PCM_SUBFORMAT_STD);
        out.intervals[HW_PARAM_SAMPLE_BITS] = SndInterval::single(16);
        out.intervals[HW_PARAM_FRAME_BITS] = SndInterval::single(16 * DEV_CHANNELS);
        out.intervals[HW_PARAM_CHANNELS] = SndInterval::single(DEV_CHANNELS);
        out.intervals[HW_PARAM_RATE] = SndInterval::single(DEV_RATE);
        out.intervals[HW_PARAM_PERIOD_SIZE] = SndInterval::single(DEV_PERIOD_FRAMES);
        out.intervals[HW_PARAM_PERIOD_BYTES] = SndInterval::single(DEV_PERIOD_BYTES);
        out.intervals[HW_PARAM_PERIODS] = SndInterval::single(DEV_BUFFER_BYTES / DEV_PERIOD_BYTES);
        out.intervals[HW_PARAM_BUFFER_SIZE] = SndInterval::single(DEV_BUFFER_FRAMES);
        out.intervals[HW_PARAM_BUFFER_BYTES] = SndInterval::single(DEV_BUFFER_BYTES);
        out.info = SNDRV_PCM_INFO_INTERLEAVED;
        out.msbits = 16;
        out.rate_num = DEV_RATE;
        out.rate_den = 1;
        out.fifo_size = 0;
        // Claim we changed everything so libasound re-reads the constrained set.
        out.cmask = u32::MAX;
        out.rmask = 0;

        self.params = Some(PcmParams {
            channels: DEV_CHANNELS,
            rate: DEV_RATE,
            format: DEV_FORMAT,
            buffer_frames: DEV_BUFFER_FRAMES,
            period_frames: DEV_PERIOD_FRAMES,
        });
        self.state = SNDRV_PCM_STATE_SETUP;
        out
    }

    /// Handles `HW_FREE`: drops the negotiated params and returns to `OPEN`.
    pub fn hw_free(&mut self) {
        self.params = None;
        self.state = SNDRV_PCM_STATE_OPEN;
    }

    /// Handles `PREPARE` state transition.
    pub fn on_prepare(&mut self) {
        self.state = SNDRV_PCM_STATE_PREPARED;
        self.started = false;
    }

    /// Handles `START` state transition.
    pub fn on_start(&mut self) {
        self.state = SNDRV_PCM_STATE_RUNNING;
        self.started = true;
    }

    /// Handles `DROP`/`DRAIN` state transition.
    pub fn on_stop(&mut self) {
        self.state = SNDRV_PCM_STATE_SETUP;
        self.started = false;
    }

    /// Whether the stream has been started.
    pub fn is_started(&self) -> bool {
        self.started
    }

    /// Advances the hardware pointer after `frames` frames were written.
    pub fn advance(&mut self, frames: u64) {
        self.hw_ptr = self.hw_ptr.wrapping_add(frames);
    }

    /// Builds the `snd_pcm_mmap_status` reply for `SNDRV_PCM_IOCTL_SYNC_PTR`.
    pub fn build_mmap_status(&self) -> SndPcmMmapStatus {
        SndPcmMmapStatus {
            state: self.state,
            pad1: 0,
            hw_ptr: self.hw_ptr,
            tstamp: SndTimespec::default(),
            suspended_state: SNDRV_PCM_STATE_OPEN,
            pad3: 0,
            audio_tstamp: SndTimespec::default(),
        }
    }

    /// Builds the `SNDRV_PCM_IOCTL_STATUS` reply.
    pub fn build_status(&self) -> SndPcmStatus {
        let params = self.params.unwrap_or(PcmParams {
            channels: DEV_CHANNELS,
            rate: DEV_RATE,
            format: DEV_FORMAT,
            buffer_frames: DEV_BUFFER_FRAMES,
            period_frames: DEV_PERIOD_FRAMES,
        });
        SndPcmStatus {
            state: self.state,
            pad1: 0,
            trigger_tstamp: SndTimespec::default(),
            tstamp: SndTimespec::default(),
            appl_ptr: self.hw_ptr,
            hw_ptr: self.hw_ptr,
            delay: 0,
            avail: params.buffer_frames as u64,
            avail_max: params.buffer_frames as u64,
            overrange: 0,
            suspended_state: SNDRV_PCM_STATE_OPEN,
            audio_tstamp_data: 0,
            audio_tstamp: SndTimespec::default(),
            driver_tstamp: SndTimespec::default(),
            audio_tstamp_accuracy: 0,
            reserved: [0; 20],
        }
    }
}

impl Default for PcmStream {
    fn default() -> Self {
        Self::new()
    }
}
