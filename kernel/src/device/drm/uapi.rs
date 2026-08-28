// SPDX-License-Identifier: MPL-2.0

//! Linux DRM core and KMS userspace ABI layouts.
//!
//! This module contains only the byte-level structures shared by the typed
//! ioctl dispatcher and DRM state handlers. Driver-specific virtio-gpu and
//! syncobj layouts remain beside their owning implementations.

use super::vblank;
use crate::prelude::*;

/// `struct drm_version`; `size_t` is 8 bytes on RISC-V.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h#L634>.
#[padding_struct]
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmVersion {
    pub(super) version_major: i32,
    pub(super) version_minor: i32,
    pub(super) version_patchlevel: i32,
    pub(super) name_len: usize,
    pub(super) name: usize,
    pub(super) date_len: usize,
    pub(super) date: usize,
    pub(super) desc_len: usize,
    pub(super) desc: usize,
}

/// `struct drm_get_cap` (also used for `drm_set_client_cap`, same layout).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmGetCap {
    pub(super) capability: u64,
    pub(super) value: u64,
}

/// `struct drm_auth` used by `DRM_IOCTL_GET_MAGIC` and `DRM_IOCTL_AUTH_MAGIC`.
///
/// Reference: <https://github.com/torvalds/linux/blob/master/include/uapi/drm/drm.h>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmAuth {
    pub(super) magic: u32,
}

/// `struct drm_set_client_cap`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmSetClientCap {
    pub(super) capability: u64,
    pub(super) value: u64,
}

/// `struct drm_gem_close`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmGemClose {
    pub(super) handle: u32,
    pub(super) pad: u32,
}

/// `struct drm_gem_flink`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmGemFlink {
    pub(super) handle: u32,
    pub(super) name: u32,
}

/// `struct drm_gem_open`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmGemOpen {
    pub(super) name: u32,
    pub(super) handle: u32,
    pub(super) size: u64,
}

/// `struct drm_prime_handle` — argument for `DRM_IOCTL_PRIME_{HANDLE_TO_FD,FD_TO_HANDLE}`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmPrimeHandle {
    pub(super) handle: u32,
    /// Only meaningful for HANDLE_TO_FD: `DRM_CLOEXEC`.
    pub(super) flags: u32,
    /// Returned dmabuf fd (HANDLE_TO_FD) or input fd (FD_TO_HANDLE).
    pub(super) fd: i32,
}

/// `struct drm_mode_card_res`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeCardRes {
    pub(super) fb_id_ptr: u64,
    pub(super) crtc_id_ptr: u64,
    pub(super) connector_id_ptr: u64,
    pub(super) encoder_id_ptr: u64,
    pub(super) count_fbs: u32,
    pub(super) count_crtcs: u32,
    pub(super) count_connectors: u32,
    pub(super) count_encoders: u32,
    pub(super) min_width: u32,
    pub(super) max_width: u32,
    pub(super) min_height: u32,
    pub(super) max_height: u32,
}

/// `struct drm_mode_modeinfo`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeModeInfo {
    pub(super) clock: u32,
    pub(super) hdisplay: u16,
    pub(super) hsync_start: u16,
    pub(super) hsync_end: u16,
    pub(super) htotal: u16,
    pub(super) hskew: u16,
    pub(super) vdisplay: u16,
    pub(super) vsync_start: u16,
    pub(super) vsync_end: u16,
    pub(super) vtotal: u16,
    pub(super) vscan: u16,
    pub(super) vrefresh: u32,
    pub(super) flags: u32,
    pub(super) type_: u32,
    pub(super) name: [u8; 32],
}

/// `struct drm_mode_crtc`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeCrtc {
    pub(super) set_connectors_ptr: u64,
    pub(super) count_connectors: u32,
    pub(super) crtc_id: u32,
    pub(super) fb_id: u32,
    pub(super) x: u32,
    pub(super) y: u32,
    pub(super) gamma_size: u32,
    pub(super) mode_valid: u32,
    pub(super) mode: DrmModeModeInfo,
}

/// `struct drm_mode_get_encoder`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeGetEncoder {
    pub(super) encoder_id: u32,
    pub(super) encoder_type: u32,
    pub(super) crtc_id: u32,
    pub(super) possible_crtcs: u32,
    pub(super) possible_clones: u32,
}

/// `struct drm_mode_get_connector`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeGetConnector {
    pub(super) encoders_ptr: u64,
    pub(super) modes_ptr: u64,
    pub(super) props_ptr: u64,
    pub(super) prop_values_ptr: u64,
    pub(super) count_modes: u32,
    pub(super) count_props: u32,
    pub(super) count_encoders: u32,
    pub(super) encoder_id: u32,
    pub(super) connector_id: u32,
    pub(super) connector_type: u32,
    pub(super) connector_type_id: u32,
    pub(super) connection: u32,
    pub(super) mm_width: u32,
    pub(super) mm_height: u32,
    pub(super) subpixel: u32,
    pub(super) pad: u32,
}

/// `struct drm_mode_fb_cmd`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeFbCmd {
    pub(super) fb_id: u32,
    pub(super) width: u32,
    pub(super) height: u32,
    pub(super) pitch: u32,
    pub(super) bpp: u32,
    pub(super) depth: u32,
    pub(super) handle: u32,
}

/// `struct drm_mode_create_dumb`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeCreateDumb {
    pub(super) height: u32,
    pub(super) width: u32,
    pub(super) bpp: u32,
    pub(super) flags: u32,
    pub(super) handle: u32,
    pub(super) pitch: u32,
    pub(super) size: u64,
}

/// `struct drm_mode_map_dumb`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeMapDumb {
    pub(super) handle: u32,
    pub(super) pad: u32,
    pub(super) offset: u64,
}

/// `struct drm_mode_destroy_dumb`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeDestroyDumb {
    pub(super) handle: u32,
}

/// `struct drm_mode_obj_get_properties`.
///
/// The three `__u32` fields after two `__u64`s leave 4 bytes of implicit
/// trailing padding (the C `sizeof` is 32, not 28); model that explicitly so the
/// struct stays `Pod`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1069>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeObjGetProperties {
    pub(super) props_ptr: u64,
    pub(super) prop_values_ptr: u64,
    pub(super) count_props: u32,
    pub(super) obj_id: u32,
    pub(super) obj_type: u32,
    pub(super) pad: u32,
}

/// `struct drm_mode_get_property`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L963>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeGetProperty {
    pub(super) values_ptr: u64,
    pub(super) enum_blob_ptr: u64,
    pub(super) prop_id: u32,
    pub(super) flags: u32,
    pub(super) name: [u8; 32],
    pub(super) count_values: u32,
    pub(super) count_enum_blobs: u32,
}

/// `struct drm_mode_property_enum`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L952>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModePropertyEnum {
    pub(super) value: u64,
    pub(super) name: [u8; 32],
}

/// `struct drm_mode_get_blob`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1084>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeGetBlob {
    pub(super) blob_id: u32,
    pub(super) length: u32,
    pub(super) data: u64,
}

/// 64-bit layout of `union drm_wait_vblank`.
///
/// The request's `signal` and the reply's `tval_sec` share the third field.
/// All Asterinas DRM architectures use the Linux 64-bit `long` layout.
///
/// Reference: <https://github.com/torvalds/linux/blob/master/include/uapi/drm/drm.h>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmWaitVblank {
    pub(super) type_: u32,
    pub(super) sequence: u32,
    pub(super) signal_or_tval_sec: u64,
    pub(super) tval_usec: i64,
}

impl DrmWaitVblank {
    pub(super) fn signal(self) -> u64 {
        self.signal_or_tval_sec
    }

    pub(super) fn set_reply(&mut self, snapshot: vblank::VblankSnapshot) {
        self.sequence = snapshot.sequence as u32;
        self.signal_or_tval_sec = snapshot.timestamp.as_secs();
        self.tval_usec = i64::from(snapshot.timestamp.subsec_micros());
    }
}

/// `struct drm_crtc_get_sequence`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmCrtcGetSequence {
    pub(super) crtc_id: u32,
    pub(super) active: u32,
    pub(super) sequence: u64,
    pub(super) sequence_ns: i64,
}

/// `struct drm_crtc_queue_sequence`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmCrtcQueueSequence {
    pub(super) crtc_id: u32,
    pub(super) flags: u32,
    pub(super) sequence: u64,
    pub(super) user_data: u64,
}

/// `struct drm_event_vblank` — the payload delivered by `read()` for vblank
/// waits and page-flip completion events.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm.h#L937>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmEventVblank {
    pub(super) type_: u32,
    pub(super) length: u32,
    pub(super) user_data: u64,
    pub(super) tv_sec: u32,
    pub(super) tv_usec: u32,
    pub(super) sequence: u32,
    pub(super) crtc_id: u32,
}

/// `struct drm_event_crtc_sequence`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmEventCrtcSequence {
    pub(super) type_: u32,
    pub(super) length: u32,
    pub(super) user_data: u64,
    pub(super) time_ns: i64,
    pub(super) sequence: u64,
}

/// `struct drm_mode_crtc_page_flip`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1424>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeCrtcPageFlip {
    pub(super) crtc_id: u32,
    pub(super) fb_id: u32,
    pub(super) flags: u32,
    pub(super) reserved: u32,
    pub(super) user_data: u64,
}

/// `struct drm_mode_fb_dirty_cmd`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1439>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeFbDirtyCmd {
    pub(super) fb_id: u32,
    pub(super) flags: u32,
    pub(super) color: u32,
    pub(super) num_clips: u32,
    pub(super) clips_ptr: u64,
}

/// `struct drm_mode_atomic`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1430>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeAtomic {
    pub(super) flags: u32,
    pub(super) count_objs: u32,
    pub(super) objs_ptr: u64,
    pub(super) count_props_ptr: u64,
    pub(super) props_ptr: u64,
    pub(super) prop_values_ptr: u64,
    pub(super) reserved: u64,
    pub(super) user_data: u64,
}

/// `struct drm_mode_create_blob`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1400>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeCreatePropertyBlob {
    pub(super) data_ptr: u64,
    pub(super) length: u32,
    pub(super) blob_id: u32,
}

/// `struct drm_mode_destroy_blob`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1407>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeDestroyPropertyBlob {
    pub(super) blob_id: u32,
    pub(super) pad: u32,
}

/// `struct drm_mode_get_plane_res`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1120>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeGetPlaneRes {
    pub(super) plane_id_ptr: u64,
    pub(super) count_planes: u32,
    pub(super) pad: u32,
}

/// `struct drm_mode_get_plane`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L1130>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeGetPlane {
    pub(super) plane_id: u32,
    pub(super) crtc_id: u32,
    pub(super) fb_id: u32,
    pub(super) possible_crtcs: u32,
    pub(super) gamma_size: u32,
    pub(super) count_format_types: u32,
    pub(super) format_type_ptr: u64,
}

/// `struct drm_mode_fb_cmd2`.
///
/// The `__u32` fields before `__u64 modifier[4]` leave 4 bytes of implicit
/// padding on 64-bit architectures (the C `sizeof` is 104, not 100); model
/// that explicitly so the struct stays `Pod`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/drm/drm_mode.h#L699>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeFbCmd2 {
    pub(super) fb_id: u32,
    pub(super) width: u32,
    pub(super) height: u32,
    pub(super) pixel_format: u32,
    pub(super) flags: u32,
    pub(super) handles: [u32; 4],
    pub(super) pitches: [u32; 4],
    pub(super) offsets: [u32; 4],
    pub(super) pad: u32,
    pub(super) modifier: [u64; 4],
}
