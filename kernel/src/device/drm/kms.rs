// SPDX-License-Identifier: MPL-2.0

//! KMS (Kernel Mode Setting) ioctls: framebuffer registration, CRTC
//! control, cursor, and page-flip.
//!
//! These are only available on the primary node (`/dev/dri/card0`), not on
//! the render node. The caller (`mod.rs`) must gate on `is_render_node()`
//! before dispatching here.

use ostd::mm::VmIo;

use super::{
    CONNECTOR_ID, CRTC_ID, DRM_MODE_CONNECTED, DRM_MODE_CONNECTOR_VIRTUAL, DRM_MODE_CURSOR_BO,
    DRM_MODE_CURSOR_MOVE, DRM_MODE_ENCODER_VIRTUAL, DrmModeCrtc, DrmModeFbCmd, DrmModeFbCmd2,
    DrmModeGetConnector, DrmModeGetEncoder, ENCODER_ID, Framebuffer, build_mode,
};
use crate::{
    context::current_userspace,
    prelude::*,
    util::ioctl::{InOutData, Ioctl},
};

/// ADDFB: register a framebuffer backed by a GEM/dumb-buffer handle.
pub(super) fn add_fb(handle: &super::DriHandle, req: &DrmModeFbCmd) -> Result<u32> {
    let object_id = {
        let inner = handle.inner.lock();
        let object_id = *inner
            .handles
            .get(&req.handle)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?;
        let guard = handle.gpu_manager.gem_objects.lock();
        let obj = guard
            .get(&object_id)
            .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;
        let buf = &obj.buffer;
        // The framebuffer may be smaller than the backing buffer (drivers
        // over-allocate, e.g. llvmpipe aligns the height up); the fb only
        // needs to fit within the buffer.
        let needed = buf.pitch * (req.height - 1) + req.width * (req.bpp + 7) / 8;
        if req.bpp != buf.bpp || needed as usize > buf.size {
            return_errno_with_message!(Errno::EINVAL, "framebuffer does not fit in the GEM object");
        }
        object_id
    };

    let mut inner = handle.inner.lock();
    let fb_id = inner.next_fb_id;
    inner.next_fb_id += 1;
    inner.framebuffers.insert(
        fb_id,
        Framebuffer {
            object_id,
            width: req.width,
            height: req.height,
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
    let object_id = {
        let inner = handle.inner.lock();
        let object_id = *inner
            .handles
            .get(&req.handles[0])
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?;
        let guard = handle.gpu_manager.gem_objects.lock();
        let obj = guard
            .get(&object_id)
            .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;
        let buf = &obj.buffer;
        // The framebuffer may be smaller than the backing buffer (drivers
        // over-allocate, e.g. llvmpipe aligns the height to 32); it only
        // needs to fit: last-row start + one row of pixels within the buffer.
        let pitch = req.pitches[0] as usize;
        let bytes_per_pixel = 4usize;
        let needed = pitch * (req.height as usize - 1) + req.width as usize * bytes_per_pixel;
        if needed > buf.size {
            return_errno_with_message!(Errno::EINVAL, "framebuffer does not fit in the GEM object");
        }
        // Accept any pitch — ADDFB2 allows explicit pitches
        object_id
    };

    let mut inner = handle.inner.lock();
    let fb_id = inner.next_fb_id;
    inner.next_fb_id += 1;
    inner.framebuffers.insert(
        fb_id,
        Framebuffer {
            object_id,
            width: req.width,
            height: req.height,
        },
    );
    Ok(fb_id)
}

/// RMFB: unregister a framebuffer.
pub(super) fn rm_fb(handle: &super::DriHandle, fb_id: u32) -> Result<()> {
    let mut inner = handle.inner.lock();
    if inner.framebuffers.remove(&fb_id).is_none() {
        return_errno_with_message!(Errno::EINVAL, "unknown framebuffer id");
    }
    if inner.current_fb_id == Some(fb_id) {
        inner.current_fb_id = None;
    }
    Ok(())
}

/// SETCRTC: set the mode and scanout framebuffer for a CRTC.
pub(super) fn set_crtc(handle: &super::DriHandle, req: &DrmModeCrtc) -> Result<()> {
    if req.crtc_id != CRTC_ID {
        return_errno_with_message!(Errno::EINVAL, "unknown crtc id");
    }
    if req.fb_id == 0 {
        return Ok(());
    }
    present_fb(handle, req.fb_id)
}

/// Presents a framebuffer on the scanout, copying its pixels to the host.
pub(super) fn present_fb(handle: &super::DriHandle, fb_id: u32) -> Result<()> {
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
        (
            base + obj.buffer.offset,
            obj.buffer.size,
            fb.width,
            fb.height,
        )
    };

    handle
        .gpu_manager
        .gpu
        .present_framebuffer(addr as u64, size as u32, width, height)
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
    let inner = handle.inner.lock();
    cmd.write(&DrmModeCrtc {
        crtc_id: CRTC_ID,
        fb_id: inner.current_fb_id.unwrap_or(0),
        mode_valid: 1,
        mode: build_mode(inner.current_width, inner.current_height),
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
    let capacity = conn.count_modes;
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
    if conn.modes_ptr != 0 && capacity >= 1 {
        let mode = build_mode(
            handle.gpu_manager.gpu.width(),
            handle.gpu_manager.gpu.height(),
        );
        current_userspace!().write_val(conn.modes_ptr as usize, &mode)?;
    }
    if conn.encoders_ptr != 0 {
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

/// CURSOR / CURSOR2: set or move the hardware cursor.
#[expect(clippy::too_many_arguments)]
pub(super) fn set_cursor(
    handle: &super::DriHandle,
    flags: u32,
    crtc_id: u32,
    x: i32,
    y: i32,
    gem_handle: u32,
    hot_x: i32,
    hot_y: i32,
) -> Result<()> {
    if crtc_id != CRTC_ID {
        return_errno_with_message!(Errno::EINVAL, "unknown crtc id");
    }

    if flags & DRM_MODE_CURSOR_BO != 0 {
        if gem_handle == 0 {
            handle
                .gpu_manager
                .gpu
                .hide_cursor()
                .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu cursor hide failed"))?;
        } else {
            let (addr, size, width, height) = {
                let inner = handle.inner.lock();
                let object_id = inner
                    .handles
                    .get(&gem_handle)
                    .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?;
                let guard = handle.gpu_manager.gem_objects.lock();
                let obj = guard
                    .get(object_id)
                    .ok_or_else(|| Error::with_message(Errno::ENOENT, "stale GEM object"))?;
                let base = handle.gpu_manager.pool_paddr()?;
                (
                    base + obj.buffer.offset,
                    obj.buffer.size,
                    obj.buffer.width,
                    obj.buffer.height,
                )
            };
            handle
                .gpu_manager
                .gpu
                .present_cursor(
                    addr as u64,
                    size as u32,
                    width,
                    height,
                    hot_x as u32,
                    hot_y as u32,
                )
                .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu cursor present failed"))?;
        }
    }

    if flags & DRM_MODE_CURSOR_MOVE != 0 {
        handle
            .gpu_manager
            .gpu
            .move_cursor(x as u32, y as u32)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu cursor move failed"))?;
    }
    Ok(())
}
