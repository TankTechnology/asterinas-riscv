// SPDX-License-Identifier: MPL-2.0

//! DRM plane support for atomic modesetting.
//!
//! The virtio-gpu device exposes a single primary plane.
//! This module provides `MODE_GETPLANERESOURCES` and `MODE_GETPLANE` ioctl handlers.

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

fn user_array_slot(base: u64, index: usize, element_size: usize) -> Result<usize> {
    let base = usize::try_from(base)
        .map_err(|_| Error::with_message(Errno::EFAULT, "userspace pointer overflows"))?;
    index
        .checked_mul(element_size)
        .and_then(|offset| base.checked_add(offset))
        .ok_or_else(|| Error::with_message(Errno::EFAULT, "userspace array pointer overflows"))
}

/// `DRM_IOCTL_MODE_GETPLANERESOURCES`: return the list of plane ids.
pub(super) fn get_plane_resources(
    handle: &super::DriHandle,
    cmd: Ioctl<b'd', 0xb5, true, InOutData<DrmModeGetPlaneRes>>,
) -> Result<i32> {
    let mut res = cmd.read()?;
    if !handle
        .universal_planes
        .load(core::sync::atomic::Ordering::Acquire)
    {
        res.count_planes = 0;
        cmd.write(&res)?;
        return Ok(0);
    }
    let capacity = res.count_planes as usize;
    res.count_planes = 1;
    if res.plane_id_ptr != 0 && capacity >= 1 {
        crate::context::current_userspace!()
            .write_val(res.plane_id_ptr as usize, &PRIMARY_PLANE_ID)?;
    }
    cmd.write(&res)?;
    Ok(0)
}

/// `DRM_IOCTL_MODE_GETPLANE`: return info for a specific plane.
pub(super) fn get_plane(
    handle: &super::DriHandle,
    cmd: Ioctl<b'd', 0xb6, true, InOutData<DrmModeGetPlane>>,
) -> Result<i32> {
    let mut plane = cmd.read()?;
    if !handle
        .universal_planes
        .load(core::sync::atomic::Ordering::Acquire)
    {
        return_errno_with_message!(Errno::EINVAL, "universal planes are not enabled");
    }
    if plane.plane_id != PRIMARY_PLANE_ID {
        return_errno_with_message!(Errno::EINVAL, "unknown plane id");
    }

    // `possible_crtcs` is a bitmask over the CRTC array, not an object id.
    let format_capacity = plane.count_format_types as usize;
    plane.possible_crtcs = 1;
    plane.gamma_size = 0;
    plane.count_format_types = DEFAULT_FORMATS.len() as u32;

    if plane.format_type_ptr != 0 && format_capacity >= DEFAULT_FORMATS.len() {
        for (i, fmt) in DEFAULT_FORMATS.iter().enumerate() {
            current_userspace!().write_val(
                user_array_slot(plane.format_type_ptr, i, size_of::<u32>())?,
                fmt,
            )?;
        }
    }

    cmd.write(&plane)?;
    Ok(0)
}
