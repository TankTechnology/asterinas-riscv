// SPDX-License-Identifier: MPL-2.0

//! Virtio-sound PCM misc-device support.
//!
//! This MVP exposes a single playback node, `/dev/snd/pcmC0D0p`, backed by the
//! first discovered [`SoundDevice`]. User space opens the node and `write()`s
//! raw PCM frames; the driver submits them to the virtio-sound TX queue. There
//! is no ALSA stack yet — this is the minimal "direct device node" path.

use aster_virtio::device::sound::{self, device::SoundDevice};
use device_id::{DeviceId, MinorId};

use crate::{
    device::{Device, DeviceType, DevtmpfsInodeMeta, registry::char},
    events::IoEvents,
    fs::{
        file::{PerOpenFileOps, StatusFlags},
        vfs::inode::FileOps,
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
};

const PCM_PLAYBACK_MINOR: u32 = 116;

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
        Ok(Box::new(SoundPcmFile))
    }
}

/// A file handle opened from `/dev/snd/pcmC0D0p`.
struct SoundPcmFile;

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
        let device = match current_device() {
            Some(device) => device,
            None => return_errno_with_message!(Errno::ENODEV, "no virtio-sound device"),
        };

        let mut total: usize = 0;
        while reader.has_remain() {
            let copied = device.play(reader)?;
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
}

fn current_device() -> Option<Arc<SoundDevice>> {
    sound::first_device()
}

pub(super) fn init_in_first_kthread() {
    if sound::first_device().is_some() {
        char::register(SoundPcmDevice::new()).unwrap();
    }
}
