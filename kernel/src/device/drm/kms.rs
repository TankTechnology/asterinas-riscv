// SPDX-License-Identifier: MPL-2.0

//! KMS (Kernel Mode Setting) ioctls: framebuffer registration, CRTC
//! control, cursor, and page-flip.
//!
//! These are only available on the primary node (`/dev/dri/card0`), not on
//! the render node. The caller (`mod.rs`) must gate on `is_render_node()`
//! before dispatching here.

use ostd::mm::VmIo;

use super::{
    CONNECTOR_ID, CRTC_ID, DRM_MODE_CONNECTED, DRM_MODE_CONNECTOR_VIRTUAL,
    DRM_MODE_ENCODER_VIRTUAL, DrmModeCrtc, DrmModeFbCmd, DrmModeFbCmd2, DrmModeGetConnector,
    DrmModeGetEncoder, ENCODER_ID, Framebuffer, build_mode,
    cursor::{CursorBuffer, CursorImage, DrmModeCursor2, MODE_CURSOR_BO, validate_cursor},
};
use crate::{
    context::current_userspace,
    prelude::*,
    util::ioctl::{InOutData, Ioctl},
};

const DRM_FORMAT_XRGB8888: u32 = 0x34325258;
const DRM_FORMAT_ARGB8888: u32 = 0x34325241;
const DRM_MODE_FB_MODIFIERS: u32 = 1 << 1;
const DRM_FORMAT_MOD_LINEAR: u64 = 0;

fn framebuffer_extent(
    offset: u32,
    pitch: u32,
    width: u32,
    height: u32,
    bits_per_pixel: u32,
) -> Option<usize> {
    if width == 0 || height == 0 || bits_per_pixel == 0 {
        return None;
    }
    let bytes_per_pixel = bits_per_pixel.checked_add(7)? / 8;
    let row_bytes = (width as usize).checked_mul(bytes_per_pixel as usize)?;
    let pitch = pitch as usize;
    if pitch < row_bytes {
        return None;
    }
    (offset as usize)
        .checked_add(pitch.checked_mul(height as usize - 1)?)?
        .checked_add(row_bytes)
}

/// ADDFB: register a framebuffer backed by a GEM/dumb-buffer handle.
pub(super) fn add_fb(handle: &super::DriHandle, req: &DrmModeFbCmd) -> Result<u32> {
    let tight_pitch = req
        .width
        .checked_mul(4)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "framebuffer pitch overflows"))?;
    if req.bpp != 32 || req.pitch != tight_pitch {
        return_errno_with_message!(
            Errno::EINVAL,
            "only tightly packed 32-bpp framebuffers work"
        );
    }
    let object_id = {
        let inner = handle.inner.lock();
        let Some(&object_id) = inner.handles.get(&req.handle) else {
            ostd::warn!(
                "drm: ADDFB unknown handle={} size={}x{} pitch={} bpp={} depth={}",
                req.handle,
                req.width,
                req.height,
                req.pitch,
                req.bpp,
                req.depth,
            );
            return_errno_with_message!(Errno::EINVAL, "unknown GEM handle");
        };
        let guard = handle.gpu_manager.gem_objects.lock();
        let Some(obj) = guard.get(&object_id) else {
            ostd::warn!("drm: ADDFB stale GEM object={}", object_id);
            return_errno_with_message!(Errno::ENOENT, "stale GEM object");
        };
        let buf = &obj.buffer;
        // The framebuffer may be smaller than the backing buffer (drivers
        // over-allocate, e.g. llvmpipe aligns the height up); the fb only
        // needs to fit within the buffer.
        let needed = framebuffer_extent(0, req.pitch, req.width, req.height, req.bpp)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "invalid framebuffer extent"))?;
        if req.bpp != buf.bpp || needed > buf.size {
            ostd::warn!(
                "drm: ADDFB handle={} object={} needs={} bytes, GEM has {}; size={}x{} pitch={} bpp={}/{} depth={} buffer={:?}",
                req.handle,
                object_id,
                needed,
                buf.size,
                req.width,
                req.height,
                req.pitch,
                req.bpp,
                buf.bpp,
                req.depth,
                buf,
            );
            return_errno_with_message!(Errno::EINVAL, "framebuffer does not fit in the GEM object");
        }
        object_id
    };
    handle.gpu_manager.retain_gem_object(object_id)?;

    let mut inner = handle.inner.lock();
    let fb_id = inner.next_fb_id;
    inner.next_fb_id += 1;
    inner.framebuffers.insert(
        fb_id,
        Framebuffer {
            object_id,
            width: req.width,
            height: req.height,
            offset: 0,
            pitch: req.pitch,
            pixel_format: DRM_FORMAT_XRGB8888,
        },
    );
    Ok(fb_id)
}

/// ADDFB2: register a framebuffer with explicit format and modifier info.
///
/// For the initial implementation, modifiers are accepted but ignored —
/// virtio-gpu 2D path uses linear scanout. We validate that the framebuffer
/// fits within the GEM object (the buffer may be larger than the fb).
pub(super) fn add_fb2(handle: &super::DriHandle, req: &DrmModeFbCmd2) -> Result<u32> {
    let tight_pitch = req
        .width
        .checked_mul(4)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "framebuffer pitch overflows"))?;
    if req.pitches[0] != tight_pitch
        || !matches!(req.pixel_format, DRM_FORMAT_XRGB8888 | DRM_FORMAT_ARGB8888)
    {
        return_errno_with_message!(
            Errno::EINVAL,
            "only tightly packed XRGB8888/ARGB8888 framebuffers work"
        );
    }
    if req.flags & !DRM_MODE_FB_MODIFIERS != 0
        || req.modifier[0] != DRM_FORMAT_MOD_LINEAR
        || req.handles[1..].iter().any(|&value| value != 0)
        || req.pitches[1..].iter().any(|&value| value != 0)
        || req.offsets[1..].iter().any(|&value| value != 0)
        || req.modifier[1..].iter().any(|&value| value != 0)
    {
        return_errno_with_message!(Errno::EINVAL, "unsupported framebuffer layout or modifier");
    }
    let object_id = {
        let inner = handle.inner.lock();
        let Some(&object_id) = inner.handles.get(&req.handles[0]) else {
            ostd::warn!(
                "drm: ADDFB2 unknown handle={} size={}x{} pitch={} offset={} format={:#x}",
                req.handles[0],
                req.width,
                req.height,
                req.pitches[0],
                req.offsets[0],
                req.pixel_format,
            );
            return_errno_with_message!(Errno::EINVAL, "unknown GEM handle");
        };
        let guard = handle.gpu_manager.gem_objects.lock();
        let Some(obj) = guard.get(&object_id) else {
            ostd::warn!("drm: ADDFB2 stale GEM object={}", object_id);
            return_errno_with_message!(Errno::ENOENT, "stale GEM object");
        };
        let buf = &obj.buffer;
        // The framebuffer may be smaller than the backing buffer (drivers
        // over-allocate, e.g. llvmpipe aligns the height to 32); it only
        // needs to fit: last-row start + one row of pixels within the buffer.
        let needed = framebuffer_extent(req.offsets[0], req.pitches[0], req.width, req.height, 32)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "invalid framebuffer extent"))?;
        if needed > buf.size {
            ostd::warn!(
                "drm: ADDFB2 handle={} object={} needs={} bytes, GEM has {}; size={}x{} pitch={} offset={} buffer={:?}",
                req.handles[0],
                object_id,
                needed,
                buf.size,
                req.width,
                req.height,
                req.pitches[0],
                req.offsets[0],
                buf,
            );
            return_errno_with_message!(Errno::EINVAL, "framebuffer does not fit in the GEM object");
        }
        object_id
    };
    handle.gpu_manager.retain_gem_object(object_id)?;

    let mut inner = handle.inner.lock();
    let fb_id = inner.next_fb_id;
    inner.next_fb_id += 1;
    inner.framebuffers.insert(
        fb_id,
        Framebuffer {
            object_id,
            width: req.width,
            height: req.height,
            offset: req.offsets[0],
            pitch: req.pitches[0],
            pixel_format: req.pixel_format,
        },
    );
    Ok(fb_id)
}

/// RMFB: unregister a framebuffer.
pub(super) fn rm_fb(handle: &super::DriHandle, fb_id: u32) -> Result<()> {
    let _kms_operation = handle.kms_operation.lock();
    let (framebuffer, was_active) = {
        let inner = handle.inner.lock();
        let framebuffer = *inner
            .framebuffers
            .get(&fb_id)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown framebuffer id"))?;
        let was_active = inner.current_fb_id == Some(fb_id);
        (framebuffer, was_active)
    };

    if was_active {
        handle
            .gpu_manager
            .gpu
            .disable_scanout()
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu disable failed"))?;
    }

    let mut inner = handle.inner.lock();
    inner.framebuffers.remove(&fb_id);
    if was_active {
        inner.current_fb_id = None;
    }
    drop(inner);
    handle
        .gpu_manager
        .release_gem_object(framebuffer.object_id)?;
    Ok(())
}

/// SETCRTC: set the mode and scanout framebuffer for a CRTC.
pub(super) fn set_crtc(handle: &super::DriHandle, req: &DrmModeCrtc) -> Result<()> {
    if req.crtc_id != CRTC_ID {
        return_errno_with_message!(Errno::EINVAL, "unknown crtc id");
    }
    if req.fb_id == 0 {
        let _kms_operation = handle.kms_operation.lock();
        handle
            .gpu_manager
            .gpu
            .disable_scanout()
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu disable failed"))?;
        handle.inner.lock().current_fb_id = None;
        return Ok(());
    }
    present_fb(handle, req.fb_id)
}

/// Presents a framebuffer on the scanout, copying its pixels to the host.
pub(super) fn present_fb(handle: &super::DriHandle, fb_id: u32) -> Result<()> {
    let _kms_operation = handle.kms_operation.lock();
    let (addr, size, width, height) = {
        let inner = handle.inner.lock();
        let fb = inner
            .framebuffers
            .get(&fb_id)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown framebuffer id"))?;
        let guard = handle.gpu_manager.gem_objects.lock();
        let obj = guard
            .get(&fb.object_id)
            .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;
        let base = handle.gpu_manager.pool_paddr()?;
        debug_assert!(matches!(
            fb.pixel_format,
            DRM_FORMAT_XRGB8888 | DRM_FORMAT_ARGB8888
        ));
        let size = framebuffer_extent(0, fb.pitch, fb.width, fb.height, 32)
            .and_then(|size| u32::try_from(size).ok())
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "framebuffer size overflows"))?;
        let addr = base
            .checked_add(obj.buffer.offset)
            .and_then(|addr| addr.checked_add(fb.offset as usize))
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "framebuffer address overflows"))?;
        (addr, size, fb.width, fb.height)
    };

    handle
        .gpu_manager
        .gpu
        .present_framebuffer(addr as u64, size, width, height)
        .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu present failed"))?;

    let mut inner = handle.inner.lock();
    inner.current_fb_id = Some(fb_id);
    inner.current_width = width;
    inner.current_height = height;
    Ok(())
}

/// GETCRTC: read back the current CRTC state.
pub(super) fn get_crtc(
    handle: &super::DriHandle,
    cmd: Ioctl<b'd', 0xa1, true, InOutData<DrmModeCrtc>>,
) -> Result<i32> {
    let req = cmd.read()?;
    if req.crtc_id != CRTC_ID {
        return_errno_with_message!(Errno::EINVAL, "unknown crtc id");
    }
    let (fb_id, current_width, current_height) = {
        let inner = handle.inner.lock();
        (
            inner.current_fb_id,
            inner.current_width,
            inner.current_height,
        )
    };
    cmd.write(&DrmModeCrtc {
        crtc_id: CRTC_ID,
        fb_id: fb_id.unwrap_or(0),
        mode_valid: u32::from(fb_id.is_some()),
        mode: build_mode(current_width, current_height),
        ..Default::default()
    })?;
    Ok(0)
}

/// GETCONNECTOR: enumerate modes and encoder for a connector.
pub(super) fn get_connector(
    handle: &super::DriHandle,
    cmd: Ioctl<b'd', 0xa7, true, InOutData<DrmModeGetConnector>>,
) -> Result<i32> {
    let mut conn = cmd.read()?;
    if conn.connector_id != CONNECTOR_ID {
        return_errno_with_message!(Errno::EINVAL, "unknown connector id");
    }
    let mode_capacity = conn.count_modes;
    let encoder_capacity = conn.count_encoders;
    conn.count_modes = 1;
    conn.count_props = 0;
    conn.count_encoders = 1;
    conn.encoder_id = ENCODER_ID;
    conn.connector_type = DRM_MODE_CONNECTOR_VIRTUAL;
    conn.connector_type_id = 1;
    conn.connection = DRM_MODE_CONNECTED;
    conn.mm_width = 0;
    conn.mm_height = 0;
    conn.subpixel = 0;
    conn.pad = 0;
    if conn.modes_ptr != 0 && mode_capacity >= 1 {
        let mode = build_mode(
            handle.gpu_manager.gpu.width(),
            handle.gpu_manager.gpu.height(),
        );
        current_userspace!().write_val(conn.modes_ptr as usize, &mode)?;
    }
    if conn.encoders_ptr != 0 && encoder_capacity >= 1 {
        current_userspace!().write_val(conn.encoders_ptr as usize, &ENCODER_ID)?;
    }
    cmd.write(&conn)?;
    Ok(0)
}

/// GETENCODER: return encoder properties.
pub(super) fn get_encoder(
    cmd: Ioctl<b'd', 0xa6, true, InOutData<DrmModeGetEncoder>>,
) -> Result<i32> {
    let mut enc = cmd.read()?;
    if enc.encoder_id != ENCODER_ID {
        return_errno_with_message!(Errno::EINVAL, "unknown encoder id");
    }
    enc.encoder_type = DRM_MODE_ENCODER_VIRTUAL;
    enc.crtc_id = CRTC_ID;
    enc.possible_crtcs = 1;
    enc.possible_clones = 0;
    cmd.write(&enc)?;
    Ok(0)
}

/// CURSOR / CURSOR2: validate and apply one hardware-cursor update.
pub(super) fn set_cursor(handle: &super::DriHandle, request: DrmModeCursor2) -> Result<()> {
    let _cursor_operation = handle.cursor_operation.lock();
    let (update, position, backing) = {
        let inner = handle.inner.lock();
        let buffer = if request.flags & MODE_CURSOR_BO != 0 && request.handle != 0 {
            let object_id = inner.handles.get(&request.handle);
            object_id.and_then(|object_id| {
                let objects = handle.gpu_manager.gem_objects.lock();
                objects.get(object_id).map(|object| {
                    let bytes_per_pixel = object.buffer.bpp.div_ceil(8);
                    CursorBuffer {
                        width: object.buffer.width,
                        height: object.buffer.height,
                        pitch: object.buffer.width.saturating_mul(bytes_per_pixel),
                        bpp: object.buffer.bpp,
                        size: object.buffer.size,
                    }
                })
            })
        } else {
            None
        };
        let update = validate_cursor(request, buffer, CRTC_ID)
            .map_err(|_| Error::with_message(Errno::EINVAL, "invalid cursor request"))?;
        let position = inner.cursor.position_for(update);
        let backing = match update.image {
            Some(CursorImage::Buffer {
                handle: gem_handle, ..
            }) => {
                let object_id = inner.handles.get(&gem_handle).ok_or_else(|| {
                    Error::with_message(Errno::EINVAL, "unknown cursor buffer handle")
                })?;
                let objects = handle.gpu_manager.gem_objects.lock();
                let object = objects
                    .get(object_id)
                    .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale cursor GEM object"))?;
                let base = handle.gpu_manager.pool_paddr()?;
                Some((
                    (base + object.buffer.offset) as u64,
                    u32::try_from(object.buffer.size).map_err(|_| {
                        Error::with_message(Errno::EINVAL, "cursor buffer is too large")
                    })?,
                ))
            }
            _ => None,
        };
        (update, position, backing)
    };

    let resource_id = match update.image {
        Some(CursorImage::Buffer {
            width,
            height,
            hot_x,
            hot_y,
            ..
        }) => {
            let (addr, size) = backing.ok_or_else(|| {
                Error::with_message(Errno::EINVAL, "cursor buffer has no backing")
            })?;
            Some(
                handle
                    .gpu_manager
                    .gpu
                    .update_cursor(
                        addr, size, width, height, hot_x, hot_y, position.x, position.y,
                    )
                    .map_err(|_| {
                        Error::with_message(Errno::EIO, "virtio-gpu cursor update failed")
                    })?,
            )
        }
        Some(CursorImage::Hide) => {
            handle
                .gpu_manager
                .gpu
                .hide_cursor(position.x, position.y)
                .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu cursor hide failed"))?;
            None
        }
        None => {
            handle
                .gpu_manager
                .gpu
                .move_cursor(position.x, position.y)
                .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu cursor move failed"))?;
            None
        }
    };

    handle.inner.lock().cursor.commit(update, resource_id);
    Ok(())
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::framebuffer_extent;

    #[ktest]
    fn framebuffer_extent_rejects_empty_or_overlapping_rows() {
        assert_eq!(framebuffer_extent(0, 256, 64, 0, 32), None);
        assert_eq!(framebuffer_extent(0, 255, 64, 64, 32), None);
    }

    #[ktest]
    fn framebuffer_extent_includes_pitch_and_offset() {
        assert_eq!(framebuffer_extent(128, 512, 64, 2, 32), Some(896));
    }
}
