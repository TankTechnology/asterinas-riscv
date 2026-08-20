// SPDX-License-Identifier: MPL-2.0

//! Virtio-sound PCM misc-device support.
//!
//! The device exposes a playback node, `/dev/snd/pcmC0D0p`, backed by the first
//! discovered [`SoundDevice`]. It speaks the ALSA PCM ioctl ABI (see
//! [`crate::device::snd`]) so unmodified ALSA clients such as `aplay` run
//! against it, and additionally accepts raw PCM `write()`s (the original
//! direct-node path kept for the AUDIO-M1 smoke test).

use aster_virtio::device::sound::{self, device::SoundDevice};
use device_id::{DeviceId, MinorId};
use ostd::mm::VmIo;

use crate::{
    context::current_userspace,
    device::{
        Device, DeviceType, DevtmpfsInodeMeta,
        registry::char,
        snd::{
            control,
            pcm::{
                DEV_BUFFER_BYTES, DEV_CHANNELS, DEV_PERIOD_BYTES, PcmStream, SNDRV_PCM_VERSION,
                SndPcmChannelInfo, SndXferi, ioctl_defs,
            },
        },
    },
    events::IoEvents,
    fs::{
        file::{PerOpenFileOps, StatusFlags},
        vfs::{inode::FileOps, path::Path},
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
    util::ioctl::{RawIoctl, dispatch_ioctl},
};

const PCM_PLAYBACK_MINOR: u32 = 116;
const CONTROL_MINOR: u32 = 0;

/// The `/dev/snd/pcmC0D0p` playback device.
#[derive(Debug)]
struct SoundPcmDevice {
    id: DeviceId,
}

impl SoundPcmDevice {
    fn new() -> Arc<Self> {
        let major = super::MISC_MAJOR.get().unwrap().get();
        let minor = MinorId::new(PCM_PLAYBACK_MINOR);
        let id = DeviceId::new(major, minor);
        Arc::new(Self { id })
    }
}

impl Device for SoundPcmDevice {
    fn type_(&self) -> DeviceType {
        DeviceType::Char
    }

    fn id(&self) -> DeviceId {
        self.id
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsInodeMeta<'_>> {
        Some(DevtmpfsInodeMeta::new("snd/pcmC0D0p"))
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        let device = match current_device() {
            Some(device) => device,
            None => return_errno_with_message!(Errno::ENODEV, "no virtio-sound device"),
        };
        Ok(Box::new(SoundPcmFile {
            device,
            pcm: Mutex::new(PcmStream::new()),
        }))
    }
}

/// A file handle opened from `/dev/snd/pcmC0D0p`.
struct SoundPcmFile {
    device: Arc<SoundDevice>,
    pcm: Mutex<PcmStream>,
}

impl SoundPcmFile {
    /// Runs the `PREPARE` handshake: parameterize + prepare the stream.
    fn prepare_device(&self) -> Result<()> {
        self.device.set_params(
            DEV_CHANNELS as u8,
            sound::FMT_S16,
            sound::RATE_48000,
            DEV_BUFFER_BYTES,
            DEV_PERIOD_BYTES,
        )?;
        self.device.prepare()?;
        self.pcm.lock().on_prepare();
        Ok(())
    }

    /// Runs the `START` handshake (idempotent).
    fn start_device(&self) -> Result<()> {
        let mut pcm = self.pcm.lock();
        if pcm.is_started() {
            return Ok(());
        }
        self.device.start()?;
        pcm.on_start();
        Ok(())
    }

    /// Runs the `DROP`/`DRAIN` handshake (idempotent).
    fn stop_device(&self) -> Result<()> {
        let mut pcm = self.pcm.lock();
        if !pcm.is_started() {
            pcm.on_stop();
            return Ok(());
        }
        self.device.stop()?;
        pcm.on_stop();
        Ok(())
    }

    /// Handles `SNDRV_PCM_IOCTL_WRITEI_FRAMES`: copies interleaved frames from
    /// userspace into the virtio-sound TX queue and writes the frame count back
    /// into the user's `snd_xferi.result`.
    fn writei(&self, mut xferi: SndXferi, arg: usize) -> Result<()> {
        // Auto-start a prepared stream before the first write, mirroring the
        // kernel's `snd_pcm_lib_write1`. `aplay` calls `snd_pcm_prepare` then
        // `snd_pcm_writei` without an explicit START, so without this the TX
        // frames would go to a prepared-but-not-started stream.
        if self.pcm.lock().params().is_some() {
            self.start_device()?;
        }
        let frame_bytes = {
            let pcm = self.pcm.lock();
            pcm.params()
                .map(|p| (p.channels * 2) as usize) // S16 = 2 bytes/sample
                .unwrap_or((DEV_CHANNELS * 2) as usize)
        };
        let len = (xferi.frames as usize)
            .checked_mul(frame_bytes)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "writei frame size overflows"))?;
        if len == 0 {
            xferi.result = 0;
            current_userspace!().write_val(arg, &xferi.result)?;
            return Ok(());
        }

        // Copy the whole PCM buffer out of user space in one shot; the
        // `current_userspace!()` temporary cannot outlive a single statement.
        let mut data = vec![0u8; len];
        current_userspace!().read_bytes(xferi.buf as usize, &mut data)?;

        let mut total_bytes = 0usize;
        while total_bytes < len {
            let copied = self.device.write_bytes(&data[total_bytes..])?;
            if copied == 0 {
                break;
            }
            total_bytes += copied;
        }

        let frames_written = total_bytes / frame_bytes;
        self.pcm.lock().advance(frames_written as u64);
        xferi.result = frames_written as i64;
        current_userspace!().write_val(arg, &xferi.result)?;
        Ok(())
    }
}

impl Pollable for SoundPcmFile {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        // Playback is writable; report readiness for writes.
        mask & IoEvents::OUT
    }
}

impl FileOps for SoundPcmFile {
    fn read_at(
        &self,
        _offset: usize,
        _writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        // Capture is not implemented yet.
        return_errno_with_message!(Errno::EOPNOTSUPP, "PCM capture is not implemented");
    }

    fn write_at(
        &self,
        _offset: usize,
        reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        // Raw-PCM path (no ALSA): auto-negotiate with the driver defaults.
        let mut total: usize = 0;
        while reader.has_remain() {
            let copied = self.device.play(reader)?;
            if copied == 0 {
                break;
            }
            total += copied;
        }
        Ok(total)
    }
}

impl PerOpenFileOps for SoundPcmFile {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        false
    }

    fn ioctl(&self, _path: &Path, raw_ioctl: RawIoctl) -> Result<i32> {
        use ioctl_defs::*;

        ostd::warn!("PCM ioctl {:#x}", raw_ioctl.cmd());
        dispatch_ioctl!(match raw_ioctl {
            cmd @ Pversion => {
                cmd.write(&SNDRV_PCM_VERSION)?;
                Ok(0)
            }
            cmd @ Info => {
                let info = self.pcm.lock().build_info();
                cmd.write(&info)?;
                Ok(0)
            }
            Tstamp | Ttstamp | UserPversion => {
                // Timestamp / protocol-version handshakes are accepted and
                // ignored (writei-only playback does not use them).
                Ok(0)
            }
            cmd @ HwRefine => {
                let input = cmd.read()?;
                let out = self.pcm.lock().apply_hw_params(&input);
                cmd.write(&out)?;
                Ok(0)
            }
            cmd @ HwParams => {
                let input = cmd.read()?;
                let out = self.pcm.lock().apply_hw_params(&input);
                cmd.write(&out)?;
                Ok(0)
            }
            _cmd @ HwFree => {
                self.pcm.lock().hw_free();
                Ok(0)
            }
            cmd @ SwParams => {
                let sw = cmd.read()?;
                cmd.write(&sw)?;
                Ok(0)
            }
            cmd @ Status => {
                let status = self.pcm.lock().build_status();
                cmd.write(&status)?;
                Ok(0)
            }
            cmd @ Delay => {
                cmd.write(&0i64)?;
                Ok(0)
            }
            cmd @ ChannelInfo => {
                let ci = SndPcmChannelInfo {
                    channel: 0,
                    offset: 0,
                    first: 0,
                    step: 32,
                    ..Default::default()
                };
                cmd.write(&ci)?;
                Ok(0)
            }
            _cmd @ Prepare => {
                self.prepare_device()?;
                Ok(0)
            }
            _cmd @ Start => {
                self.start_device()?;
                Ok(0)
            }
            Drop | Drain => {
                self.stop_device()?;
                Ok(0)
            }
            cmd @ WriteiFrames => {
                let xferi = cmd.read()?;
                self.writei(xferi, raw_ioctl.arg())?;
                Ok(0)
            }
            cmd @ SyncPtr => {
                // libasound pushes its `appl_ptr` and pulls back `state`/`hw_ptr`.
                // Playback is synchronous (hw_ptr advances in `writei`), so the
                // app's `appl_ptr` is informational; we just report our tracked
                // position.
                let mut sync_ptr = cmd.read()?;
                sync_ptr.status = self.pcm.lock().build_mmap_status();
                cmd.write(&sync_ptr)?;
                Ok(0)
            }
            _ => {
                ostd::warn!("unknown ALSA PCM ioctl command {:#x}", raw_ioctl.cmd());
                return_errno_with_message!(Errno::ENOTTY, "unknown ALSA PCM ioctl");
            }
        })
    }
}

/// The `/dev/snd/controlC0` control device. It only answers
/// `SNDRV_CTL_IOCTL_CARD_INFO`, which is all `libasound` needs to resolve the
/// card (`snd_card_load2`), and returns `ENOTTY` for everything else.
#[derive(Debug)]
struct SoundControlDevice {
    id: DeviceId,
}

impl SoundControlDevice {
    fn new() -> Arc<Self> {
        let major = super::MISC_MAJOR.get().unwrap().get();
        let minor = MinorId::new(CONTROL_MINOR);
        let id = DeviceId::new(major, minor);
        Arc::new(Self { id })
    }
}

impl Device for SoundControlDevice {
    fn type_(&self) -> DeviceType {
        DeviceType::Char
    }

    fn id(&self) -> DeviceId {
        self.id
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsInodeMeta<'_>> {
        Some(DevtmpfsInodeMeta::new("snd/controlC0"))
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        Ok(Box::new(SoundControlFile))
    }
}

/// A file handle opened from `/dev/snd/controlC0`.
struct SoundControlFile;

impl Pollable for SoundControlFile {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        mask & IoEvents::IN
    }
}

impl FileOps for SoundControlFile {
    fn read_at(
        &self,
        _offset: usize,
        _writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "control read is not supported");
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "control write is not supported");
    }
}

impl PerOpenFileOps for SoundControlFile {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        false
    }

    fn ioctl(&self, _path: &Path, raw_ioctl: RawIoctl) -> Result<i32> {
        use control::ioctl_defs::*;

        ostd::warn!("CTL ioctl {:#x}", raw_ioctl.cmd());
        dispatch_ioctl!(match raw_ioctl {
            cmd @ Pversion => {
                cmd.write(&control::SNDRV_CTL_VERSION)?;
                Ok(0)
            }
            cmd @ CardInfo => {
                cmd.write(&control::build_card_info())?;
                Ok(0)
            }
            _cmd @ SubscribeEvents => {
                // We generate no events; accept the subscription as a no-op.
                Ok(0)
            }
            _ => return_errno_with_message!(Errno::ENOTTY, "unknown ALSA control ioctl"),
        })
    }
}

fn current_device() -> Option<Arc<SoundDevice>> {
    sound::first_device()
}

pub(super) fn init_in_first_kthread() {
    if sound::first_device().is_some() {
        char::register(SoundPcmDevice::new()).unwrap();
        char::register(SoundControlDevice::new()).unwrap();
    }
}
