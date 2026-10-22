// SPDX-License-Identifier: MPL-2.0

//! ALSA PCM and control character devices.
//!
//! This is the smallest surface `aplay` (libasound) needs to open
//! `/dev/snd/pcmC0D0p`, negotiate 48 kHz/16-bit/stereo, write interleaved
//! frames and drain. Playback is synchronous: a `WRITEI` returns once the
//! samples have been consumed by the controller, which keeps the driver free
//! of a software ring and satisfies ALSA's blocking-write contract.

use alloc::{boxed::Box, sync::Arc, vec};

use aster_core::{
    context::{read_user_val, write_user_val},
    device::{Device, DeviceType, registry::char},
    events::IoEvents,
    fs::{
        devtmpfs::DevtmpfsNodeMeta,
        file::{PerOpenFileOps, StatusFlags},
        vfs::{inode::FileOps, path::Path},
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
    util::ioctl::RawIoctl,
};
use device_id::{DeviceId, MajorId, MinorId};
use ostd::{
    mm::{VmReader, VmWriter},
    sync::Mutex,
    warn,
};
use ostd_pod::{FromZeros, IntoBytes};
use spin::Once;

use super::{
    HdaController,
    uapi::*,
};

/// Linux's `SOUND_MAJOR`.
const SOUND_MAJOR: u16 = 116;
/// The control device of card 0.
const CONTROL_MINOR: u32 = 0;
/// The playback PCM device of card 0.
const PCM_MINOR: u32 = 3;

/// The controller the PCM node drives; set once at probe time.
static CONTROLLER: Once<Arc<HdaController>> = Once::new();

/// Registers `/dev/snd/controlC0` and `/dev/snd/pcmC0D0p`.
pub(super) fn register(controller: &Arc<HdaController>) {
    CONTROLLER.call_once(|| controller.clone());
    let _ = char::register(Arc::new(SndControl));
    let _ = char::register(Arc::new(SndPcmPlayback));
}

#[derive(Debug)]
struct SndControl;

impl Device for SndControl {
    fn type_(&self) -> DeviceType {
        DeviceType::Char
    }

    fn id(&self) -> DeviceId {
        DeviceId::new(MajorId::new(SOUND_MAJOR), MinorId::new(CONTROL_MINOR))
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsNodeMeta> {
        Some(DevtmpfsNodeMeta::new("snd/controlC0").unwrap())
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        Ok(Box::new(SndControlFile))
    }
}

#[derive(Debug)]
struct SndPcmPlayback;

impl Device for SndPcmPlayback {
    fn type_(&self) -> DeviceType {
        DeviceType::Char
    }

    fn id(&self) -> DeviceId {
        DeviceId::new(MajorId::new(SOUND_MAJOR), MinorId::new(PCM_MINOR))
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsNodeMeta> {
        Some(DevtmpfsNodeMeta::new("snd/pcmC0D0p").unwrap())
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        Ok(Box::new(SndPcmFile::default()))
    }
}

// --- PCM device -----------------------------------------------------------

#[derive(Debug)]
struct SndPcmFile {
    state: Mutex<PcmState>,
}

impl Default for SndPcmFile {
    fn default() -> Self {
        Self {
            state: Mutex::new(PcmState::default()),
        }
    }
}

#[derive(Debug, Default)]
struct PcmState {
    hw: Option<SndPcmHwParams>,
    prepared: bool,
    running: bool,
    hw_ptr: u64,
    appl_ptr: u64,
}

impl SndPcmFile {
    fn ioctl(&self, raw: RawIoctl) -> Result<i32> {
        let magic = (raw.cmd() >> 8) & 0xFF;
        if magic != SNDRV_PCM_MAGIC {
            return_errno_with_message!(Errno::ENOTTY, "not an ALSA PCM ioctl");
        }

        match raw.cmd() & 0xFF {
            PCM_PVERSION => {
                write_user_val(raw.arg(), &SNDRV_PCM_VERSION)?;
                Ok(0)
            }
            PCM_INFO => {
                let mut info = SndPcmInfo::new_zeroed();
                fill_pcm_info(&mut info);
                write_user_val(raw.arg(), &info)?;
                Ok(0)
            }
            PCM_TSTAMP | PCM_TTSTAMP | PCM_USER_PVERSION => {
                // Timestamp/version negotiation from libasound; accepted and
                // ignored, the driver reports its own protocol version.
                let _: i32 = read_user_val(raw.arg())?;
                Ok(0)
            }
            PCM_HW_REFINE => self.hw_refine(raw.arg()),
            PCM_HW_PARAMS => self.hw_params(raw.arg()),
            PCM_HW_FREE => {
                let mut state = self.state.lock();
                state.hw = None;
                state.prepared = false;
                Ok(0)
            }
            PCM_SW_PARAMS => {
                let _: SndPcmSwParams = read_user_val(raw.arg())?;
                Ok(0)
            }
            PCM_PREPARE => {
                let mut state = self.state.lock();
                if state.hw.is_none() {
                    return_errno_with_message!(Errno::EINVAL, "no hardware parameters set");
                }
                state.prepared = true;
                state.running = false;
                state.hw_ptr = 0;
                state.appl_ptr = 0;
                Ok(0)
            }
            PCM_START => {
                let mut state = self.state.lock();
                if !state.prepared {
                    return_errno_with_message!(Errno::EPIPE, "the stream is not prepared");
                }
                state.running = true;
                Ok(0)
            }
            PCM_DRAIN | PCM_HWSYNC | PCM_RESET | PCM_DROP => {
                let mut state = self.state.lock();
                state.running = false;
                Ok(0)
            }
            PCM_STATUS => {
                let status = self.status();
                write_user_val(raw.arg(), &status)?;
                Ok(0)
            }
            PCM_SYNC_PTR => self.sync_ptr(raw.arg()),
            PCM_WRITEI_FRAMES => self.writei_frames(raw.arg()),
            _ => {
                warn!("unsupported PCM ioctl magic={:#x} nr={:#x}", magic, raw.cmd() & 0xFF);
                return_errno_with_message!(Errno::ENOTTY, "unsupported ALSA PCM ioctl");
            }
        }
    }

    fn hw_refine(&self, arg: usize) -> Result<i32> {
        let mut params: SndPcmHwParams = read_user_val(arg)?;
        let mut cmask = 0u32;

        let masks = [
            (HW_PARAM_ACCESS, ACCESS_RW_INTERLEAVED),
            (HW_PARAM_FORMAT, FORMAT_S16_LE),
            (HW_PARAM_SUBFORMAT, SUBFORMAT_STD),
        ];
        for (index, value) in masks {
            if !params.masks[index].test_bit(value) {
                warn!(
                    "hw_refine: mask {} lacks {} (bits {:08x}...)",
                    index, value, params.masks[index].bits[0]
                );
                return_errno_with_message!(Errno::EINVAL, "the requested access/format is unsupported");
            }
            if params.masks[index].set_only(value) {
                cmask |= 1 << index;
            }
        }

        let fixed = [
            (HW_PARAM_SAMPLE_BITS, SAMPLE_BITS),
            (HW_PARAM_FRAME_BITS, FRAME_BITS),
            (HW_PARAM_CHANNELS, CHANNELS),
            (HW_PARAM_RATE, RATE),
        ];
        for (index, value) in fixed {
            let interval = params.intervals[index - HW_PARAM_FIRST_INTERVAL];
            if value < interval.min || value > interval.max {
                warn!(
                    "hw_refine: interval {} wants {}..{} but we support {}",
                    index, interval.min, interval.max, value
                );
                return_errno_with_message!(Errno::EINVAL, "the requested format is unsupported");
            }
            if !interval.is_fixed_at(value) {
                params.intervals[index - HW_PARAM_FIRST_INTERVAL] = SndInterval::fixed(value);
                cmask |= 1 << index;
            }
        }

        // Buffer geometry: libasound picks the minimum of every interval, so
        // an unconstrained period would come out as zero frames and its
        // default software parameters would then be rejected.
        let geometry = [
            (HW_PARAM_PERIOD_TIME, 20_000u32, 1_000_000u32),
            (HW_PARAM_PERIOD_SIZE, 480, 65_536),
            (HW_PARAM_PERIOD_BYTES, 1_920, 262_144),
            (HW_PARAM_PERIODS, 2, 1024),
            (HW_PARAM_BUFFER_TIME, 40_000, 1_000_000),
            (HW_PARAM_BUFFER_SIZE, 1_920, MAX_FRAMES as u32),
            (HW_PARAM_BUFFER_BYTES, 7_680, (MAX_FRAMES * 4) as u32),
        ];
        for (index, low, high) in geometry {
            let interval = params.intervals[index - HW_PARAM_FIRST_INTERVAL];
            let min = interval.min.max(low);
            let max = interval.max.min(high);
            if min > max {
                warn!(
                    "hw_refine: geometry {} wants {}..{} but we support {}..{}",
                    index, interval.min, interval.max, low, high
                );
                return_errno_with_message!(Errno::EINVAL, "the buffer geometry is unsupported");
            }
            if min != interval.min || max != interval.max {
                params.intervals[index - HW_PARAM_FIRST_INTERVAL] =
                    SndInterval { min, max, flags: 1 << 2 };
                cmask |= 1 << index;
            }
        }

        params.cmask = cmask;
        write_user_val(arg, &params)?;
        Ok(0)
    }

    fn hw_params(&self, arg: usize) -> Result<i32> {
        let params: SndPcmHwParams = read_user_val(arg)?;

        if !params.masks[HW_PARAM_ACCESS].test_bit(ACCESS_RW_INTERLEAVED)
            || !params.masks[HW_PARAM_FORMAT].test_bit(FORMAT_S16_LE)
            || !params.masks[HW_PARAM_SUBFORMAT].test_bit(SUBFORMAT_STD)
        {
            warn!(
                "hw_params: masks access={:08x} format={:08x} subformat={:08x}",
                params.masks[HW_PARAM_ACCESS].bits[0],
                params.masks[HW_PARAM_FORMAT].bits[0],
                params.masks[HW_PARAM_SUBFORMAT].bits[0]
            );
            return_errno_with_message!(Errno::EINVAL, "the access/format is unsupported");
        }
        let intervals = [
            (SAMPLE_BITS, params.intervals[HW_PARAM_SAMPLE_BITS - HW_PARAM_FIRST_INTERVAL]),
            (FRAME_BITS, params.intervals[HW_PARAM_FRAME_BITS - HW_PARAM_FIRST_INTERVAL]),
            (CHANNELS, params.intervals[HW_PARAM_CHANNELS - HW_PARAM_FIRST_INTERVAL]),
            (RATE, params.intervals[HW_PARAM_RATE - HW_PARAM_FIRST_INTERVAL]),
        ];
        for (value, interval) in intervals {
            if value < interval.min || value > interval.max {
                warn!(
                    "hw_params: interval for {} is {}..{} (ours {})",
                    value, interval.min, interval.max, value
                );
                return_errno_with_message!(Errno::EINVAL, "the sample format is unsupported");
            }
        }

        self.state.lock().hw = Some(params);
        Ok(0)
    }

    fn writei_frames(&self, arg: usize) -> Result<i32> {
        let mut xfer: SndXferi = read_user_val(arg)?;
        if xfer.frames == 0 {
            return Ok(0);
        }
        if xfer.frames > MAX_FRAMES {
            return_errno_with_message!(Errno::EINVAL, "the write is larger than one second");
        }

        let frames = xfer.frames;
        let bytes = (frames * 4) as usize;
        let user_ptr = usize::try_from(xfer.buf)
            .map_err(|_| Error::with_message(Errno::EFAULT, "invalid user buffer"))?;
        let mut data = vec![0u8; bytes];
        aster_core::context::read_user_slice(user_ptr, &mut data)?;

        {
            let state = self.state.lock();
            if state.hw.is_none() || !state.prepared {
                return_errno_with_message!(Errno::EPIPE, "the stream is not prepared");
            }
        }

        let controller = CONTROLLER
            .get()
            .ok_or_else(|| Error::with_message(Errno::ENODEV, "no HDA controller"))?;
        if let Err(error) = controller.play(&data) {
            warn!("writei failed: {:?}", error);
            return Err(error);
        }

        {
            let mut state = self.state.lock();
            state.hw_ptr += frames;
            state.appl_ptr += frames;
        }

        xfer.result = frames as i64;
        write_user_val(arg, &xfer)?;
        Ok(0)
    }

    fn status(&self) -> SndPcmStatus {
        let state = self.state.lock();
        let mut status = SndPcmStatus::new_zeroed();
        status.state = if state.running {
            STATE_RUNNING
        } else if state.prepared {
            STATE_PREPARED
        } else if state.hw.is_some() {
            STATE_SETUP
        } else {
            STATE_OPEN
        };
        let now = aster_core::monotonic_now();
        status.trigger_tstamp.tv_sec = now.as_secs() as i64;
        status.trigger_tstamp.tv_nsec = now.subsec_nanos() as i64;
        status.tstamp = status.trigger_tstamp;
        status.appl_ptr = state.appl_ptr;
        status.hw_ptr = state.hw_ptr;
        status
    }

    fn sync_ptr(&self, arg: usize) -> Result<i32> {
        let mut sync: SndPcmSyncPtr = read_user_val(arg)?;
        let status = self.status();
        let status_bytes = status.as_bytes();
        let len = core::cmp::min(status_bytes.len(), sync.status.len());
        sync.status[..len].copy_from_slice(&status_bytes[..len]);

        let state = self.state.lock();
        sync.control[..8].copy_from_slice(&state.appl_ptr.to_ne_bytes());
        // avail_min stays zero: the driver never blocks on buffer space.
        write_user_val(arg, &sync)?;
        Ok(0)
    }
}

fn fill_pcm_info(info: &mut SndPcmInfo) {
    info.card = 0;
    info.stream = STREAM_PLAYBACK;
    info.dev_class = 0;
    info.subdevices_count = 1;
    info.subdevices_avail = 1;
    fill_name(&mut info.id, "HDA");
    fill_name(&mut info.name, "Asterinas HDA");
    fill_name(&mut info.subname, "playback");
}

impl FileOps for SndPcmFile {
    fn read_at(
        &self,
        _offset: usize,
        _writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EINVAL, "the playback node is write-only")
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        // libasound falls back to `write(2)` only when `WRITEI` is missing;
        // report that the node takes interleaved writes through the ioctl.
        return_errno_with_message!(Errno::EINVAL, "use SNDRV_PCM_IOCTL_WRITEI_FRAMES")
    }
}

impl PerOpenFileOps for SndPcmFile {
    fn check_seekable(&self) -> Result<()> {
        return_errno_with_message!(Errno::ESPIPE, "a PCM device is not seekable")
    }

    fn is_offset_aware(&self) -> bool {
        false
    }

    fn ioctl(&self, _path: &Path, raw_ioctl: RawIoctl) -> Result<i32> {
        self.ioctl(raw_ioctl)
    }
}

impl Pollable for SndPcmFile {
    fn poll(&self, _mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        IoEvents::empty()
    }
}

// --- Control device -------------------------------------------------------

#[derive(Debug)]
struct SndControlFile;

impl SndControlFile {
    fn ioctl(&self, raw: RawIoctl) -> Result<i32> {
        let magic = (raw.cmd() >> 8) & 0xFF;
        if magic != SNDRV_CTL_MAGIC {
            return_errno_with_message!(Errno::ENOTTY, "not an ALSA control ioctl");
        }

        match raw.cmd() & 0xFF {
            CTL_PVERSION => {
                write_user_val(raw.arg(), &SNDRV_CTL_VERSION)?;
                Ok(0)
            }
            CTL_CARD_INFO => {
                let mut info = SndCtlCardInfo::new_zeroed();
                info.card = 0;
                fill_name(&mut info.id, "HDA");
                fill_name(&mut info.driver, "AsterinasHDA");
                fill_name(&mut info.name, "Asterinas HDA");
                fill_name(&mut info.longname, "Asterinas HDA (virtio/PCI, 48 kHz)");
                fill_name(&mut info.mixername, "");
                fill_name(&mut info.components, "HDA:1af40012");
                write_user_val(raw.arg(), &info)?;
                Ok(0)
            }
            CTL_PCM_INFO => {
                let mut info: SndPcmInfo = read_user_val(raw.arg())?;
                let is_playback = info.stream == STREAM_PLAYBACK;
                let is_card_pcm = info.device == 0 && info.subdevice == 0;
                fill_pcm_info(&mut info);
                if !is_playback || !is_card_pcm {
                    return_errno_with_message!(Errno::ENOENT, "no such PCM device");
                }
                write_user_val(raw.arg(), &info)?;
                Ok(0)
            }
            CTL_PCM_NEXT_DEVICE => {
                let _: i32 = read_user_val(raw.arg())?;
                write_user_val(raw.arg(), &-1i32)?;
                Ok(0)
            }
            CTL_PCM_PREFER_SUBDEVICE => {
                let _: i32 = read_user_val(raw.arg())?;
                Ok(0)
            }
            _ => {
                warn!("unsupported control ioctl magic={:#x} nr={:#x}", magic, raw.cmd() & 0xFF);
                return_errno_with_message!(Errno::ENOTTY, "unsupported ALSA control ioctl");
            }
        }
    }
}

impl FileOps for SndControlFile {
    fn read_at(
        &self,
        _offset: usize,
        _writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EINVAL, "the control node is ioctl-only")
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EINVAL, "the control node is ioctl-only")
    }
}

impl PerOpenFileOps for SndControlFile {
    fn check_seekable(&self) -> Result<()> {
        return_errno_with_message!(Errno::ESPIPE, "a control device is not seekable")
    }

    fn is_offset_aware(&self) -> bool {
        false
    }

    fn ioctl(&self, _path: &Path, raw_ioctl: RawIoctl) -> Result<i32> {
        self.ioctl(raw_ioctl)
    }
}

impl Pollable for SndControlFile {
    fn poll(&self, _mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        IoEvents::empty()
    }
}
