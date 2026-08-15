// SPDX-License-Identifier: MPL-2.0

//! Minimal DRM character device support.
//!
//! Exposes a single `/dev/dri/card0` node backed by the first discovered
//! virtio-gpu device. The ioctl surface is deliberately minimal for the M1
//! bring-up: `DRM_IOCTL_VERSION` identifies the device; mode-setting and
//! buffer-sharing ioctls are left for later milestones.

use device_id::{DeviceId, MajorId, MinorId};
use ostd::mm::VmIo;

use crate::{
    context::current_userspace,
    device::{Device, DeviceType, DevtmpfsInodeMeta, registry::char},
    events::IoEvents,
    fs::{
        file::{PerOpenFileOps, StatusFlags},
        vfs::{inode::FileOps, path::Path},
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
    util::ioctl::{RawIoctl, dispatch_ioctl},
};

/// Linux DRM character-device major number.
const DRM_MAJOR: u16 = 226;

const DRIVER_NAME: &str = "virtio-gpu";
const DRIVER_DATE: &str = "20260815";
const DRIVER_DESC: &str = "Asterinas virtio-gpu 2D driver";

#[derive(Debug)]
struct Dri;

#[derive(Debug)]
struct DriHandle;

/// `struct drm_version`; `size_t` is 8 bytes on RISC-V.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h#L634>.
#[padding_struct]
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVersion {
    version_major: i32,
    version_minor: i32,
    version_patchlevel: i32,
    name_len: usize,
    name: usize,
    date_len: usize,
    date: usize,
    desc_len: usize,
    desc: usize,
}

mod ioctl_defs {
    use super::DrmVersion;
    use crate::util::ioctl::{InOutData, ioc};

    // Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h#L1124>
    // #define DRM_IOCTL_VERSION _IOWR(DRM_IOCTL_BASE, DRM_IOCTL_NR(0), struct drm_version)
    pub(super) type GetVersion = ioc!(DRM_IOCTL_VERSION, b'd', 0x00, InOutData<DrmVersion>);
}

impl Device for Dri {
    fn type_(&self) -> DeviceType {
        DeviceType::Char
    }

    fn id(&self) -> DeviceId {
        // Linux: major 226 (DRM), minor 0 (the first card).
        DeviceId::new(MajorId::new(DRM_MAJOR), MinorId::new(0))
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsInodeMeta<'_>> {
        Some(DevtmpfsInodeMeta::new("dri/card0"))
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        if aster_virtio::device::gpu::first_device().is_none() {
            return_errno_with_message!(Errno::ENODEV, "no virtio-gpu device");
        }
        Ok(Box::new(DriHandle))
    }
}

impl Pollable for DriHandle {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        mask & IoEvents::OUT
    }
}

impl FileOps for DriHandle {
    fn read_at(
        &self,
        _offset: usize,
        _writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        Ok(0)
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        Ok(0)
    }
}

impl PerOpenFileOps for DriHandle {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        false
    }

    fn ioctl(&self, _path: &Path, raw_ioctl: RawIoctl) -> Result<i32> {
        use ioctl_defs::*;

        dispatch_ioctl!(match raw_ioctl {
            cmd @ GetVersion => {
                let mut version = cmd.read()?;
                version.version_major = 0;
                version.version_minor = 1;
                version.version_patchlevel = 0;
                copy_field(version.name, &mut version.name_len, DRIVER_NAME)?;
                copy_field(version.date, &mut version.date_len, DRIVER_DATE)?;
                copy_field(version.desc, &mut version.desc_len, DRIVER_DESC)?;
                cmd.write(&version)?;
                Ok(0)
            }
            _ => {
                ostd::debug!(
                    "the ioctl command {:#x} is unknown for DRM devices",
                    raw_ioctl.cmd()
                );
                return_errno_with_message!(Errno::ENOTTY, "the ioctl command is unknown");
            }
        })
    }
}

/// Copies a driver string into a userspace buffer and updates the length field.
///
/// If the buffer is null (or has zero length), only the required length is
/// reported. Otherwise the string is null-terminated and truncated as needed.
fn copy_field(dst: usize, len: &mut usize, src: &str) -> Result<()> {
    let src_bytes = src.as_bytes();
    if dst != 0 && *len > 0 {
        let copy = src_bytes.len().min(*len - 1);
        current_userspace!().write_bytes(dst, &src_bytes[..copy])?;
        current_userspace!().write_val(dst + copy, &0u8)?;
    }
    *len = src_bytes.len();
    Ok(())
}

pub(super) fn init_in_first_kthread() {
    if aster_virtio::device::gpu::first_device().is_none() {
        return;
    }

    char::register(Arc::new(Dri)).expect("failed to register DRM char device");
}
