// SPDX-License-Identifier: MPL-2.0

//! DRM plane support for atomic modesetting.
//!
//! The virtio-gpu device exposes a single primary plane. This module provides
//! `MODE_GETPLANERESOURCES` and `MODE_GETPLANE` ioctl handlers.

use ostd::mm::VmIo;

use super::{DrmModeGetPlane, DrmModeGetPlaneRes, PRIMARY_PLANE_ID};
use crate::{
    context::current_userspace,
    prelude::*,
    util::ioctl::{InOutData, Ioctl},
};

/// Format codes supported by the primary plane.
///
/// `DRM_FORMAT_XRGB8888` and `DRM_FORMAT_ARGB8888` are the standard
/// 32-bpp formats used by virtio-gpu.
const DRM_FORMAT_XRGB8888: u32 = 0x34325258; // XR24 (little-endian)
const DRM_FORMAT_ARGB8888: u32 = 0x34325241; // AR24 (little-endian)

const DEFAULT_FORMATS: [u32; 2] = [DRM_FORMAT_XRGB8888, DRM_FORMAT_ARGB8888];

/// `DRM_IOCTL_MODE_GETPLANERESOURCES`: return the list of plane ids.
pub(super) fn get_plane_resources(
    cmd: Ioctl<b'd', 0xb5, true, InOutData<DrmModeGetPlaneRes>>,
) -> Result<i32> {
    let mut res = cmd.read()?;
    res.count_planes = 1;
    if res.plane_id_ptr != 0 {
        crate::context::current_userspace!()
            .write_val(res.plane_id_ptr as usize, &PRIMARY_PLANE_ID)?;
    }
    cmd.write(&res)?;
    Ok(0)
}

/// `DRM_IOCTL_MODE_GETPLANE`: return info for a specific plane.
pub(super) fn get_plane(cmd: Ioctl<b'd', 0xb6, true, InOutData<DrmModeGetPlane>>) -> Result<i32> {
    let mut plane = cmd.read()?;
    if plane.plane_id != PRIMARY_PLANE_ID {
        return_errno_with_message!(Errno::EINVAL, "unknown plane id");
    }

    plane.possible_crtcs = 1; // bit 0 = CRTC_ID=1
    plane.gamma_size = 0;
    plane.count_format_types = DEFAULT_FORMATS.len() as u32;

    if plane.format_type_ptr != 0 {
        let format_ptr = plane.format_type_ptr as usize;
        for (i, fmt) in DEFAULT_FORMATS.iter().enumerate() {
            current_userspace!().write_val(format_ptr + i * size_of::<u32>(), fmt)?;
        }
    }

    cmd.write(&plane)?;
    Ok(0)
}
