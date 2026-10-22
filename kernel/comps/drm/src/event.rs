// SPDX-License-Identifier: MPL-2.0

//! DRM file events.
//!
//! Event delivery is the other half of asynchronous page flips: a client sets
//! `DRM_MODE_PAGE_FLIP_EVENT`, polls the DRM file, and reads a
//! `drm_event_vblank`-shaped record. The same record type carries vblank
//! events requested through `DRM_IOCTL_WAIT_VBLANK`.

use aster_core::prelude::*;
use core::time::Duration;

/// `DRM_EVENT_VBLANK`.
pub(crate) const DRM_EVENT_VBLANK: u32 = 0x01;
/// `DRM_EVENT_FLIP_COMPLETE`.
pub(crate) const DRM_EVENT_FLIP_COMPLETE: u32 = 0x02;

/// `struct drm_event` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm.h#L1141-L1144>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(crate) struct DrmEvent {
    type_: u32,
    length: u32,
}

/// `struct drm_event_vblank` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm.h#L1151-L1158>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(crate) struct DrmEventVblank {
    base: DrmEvent,
    user_data: u64,
    tv_sec: u32,
    tv_usec: u32,
    sequence: u32,
    crtc_id: u32,
}

impl DrmEventVblank {
    pub(crate) fn new(
        event_type: u32,
        user_data: u64,
        time: Duration,
        sequence: u32,
        crtc_id: u32,
    ) -> Self {
        Self {
            base: DrmEvent {
                type_: event_type,
                length: core::mem::size_of::<Self>() as u32,
            },
            user_data,
            tv_sec: time.as_secs() as u32,
            tv_usec: time.subsec_micros(),
            sequence,
            crtc_id,
        }
    }
}
