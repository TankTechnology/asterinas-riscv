// SPDX-License-Identifier: MPL-2.0

//! Validation for the legacy DRM hardware-cursor ioctls.

use crate::prelude::*;

pub(super) const MODE_CURSOR_BO: u32 = 0x01;
pub(super) const MODE_CURSOR_MOVE: u32 = 0x02;

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeCursor {
    pub flags: u32,
    pub crtc_id: u32,
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    pub handle: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct DrmModeCursor2 {
    pub flags: u32,
    pub crtc_id: u32,
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    pub handle: u32,
    pub hot_x: i32,
    pub hot_y: i32,
}

impl From<DrmModeCursor> for DrmModeCursor2 {
    fn from(cursor: DrmModeCursor) -> Self {
        Self {
            flags: cursor.flags,
            crtc_id: cursor.crtc_id,
            x: cursor.x,
            y: cursor.y,
            width: cursor.width,
            height: cursor.height,
            handle: cursor.handle,
            hot_x: 0,
            hot_y: 0,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct CursorBuffer {
    pub width: u32,
    pub height: u32,
    pub pitch: u32,
    pub bpp: u32,
    pub size: usize,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(super) struct CursorPosition {
    pub x: i32,
    pub y: i32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum CursorImage {
    Hide,
    Buffer {
        handle: u32,
        width: u32,
        height: u32,
        hot_x: u32,
        hot_y: u32,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct CursorUpdate {
    pub position: Option<CursorPosition>,
    pub image: Option<CursorImage>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(super) struct CursorState {
    pub active_handle: Option<u32>,
    pub resource_id: Option<u32>,
    pub position: CursorPosition,
}

impl CursorState {
    pub fn position_for(&self, update: CursorUpdate) -> CursorPosition {
        update.position.unwrap_or(self.position)
    }

    /// Commits an update after its device operation has succeeded.
    pub fn commit(&mut self, update: CursorUpdate, resource_id: Option<u32>) {
        if let Some(position) = update.position {
            self.position = position;
        }
        match update.image {
            Some(CursorImage::Buffer { handle, .. }) => {
                debug_assert!(resource_id.is_some());
                self.active_handle = Some(handle);
                self.resource_id = resource_id;
            }
            Some(CursorImage::Hide) => {
                debug_assert!(resource_id.is_none());
                self.active_handle = None;
                self.resource_id = None;
            }
            None => debug_assert!(resource_id.is_none()),
        }
    }

    pub fn uses_handle(&self, handle: u32) -> bool {
        self.active_handle == Some(handle)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum CursorValidationError {
    InvalidCrtc,
    InvalidFlags,
    InvalidDimensions,
    InvalidHotspot,
    MissingBuffer,
    InvalidBuffer,
}

/// Validates one legacy cursor request after the caller has resolved its
/// per-file dumb-buffer handle.
pub(super) fn validate_cursor(
    request: DrmModeCursor2,
    buffer: Option<CursorBuffer>,
    expected_crtc: u32,
) -> core::result::Result<CursorUpdate, CursorValidationError> {
    if request.crtc_id != expected_crtc {
        return Err(CursorValidationError::InvalidCrtc);
    }

    let supported_flags = MODE_CURSOR_BO | MODE_CURSOR_MOVE;
    if request.flags == 0 || request.flags & !supported_flags != 0 {
        return Err(CursorValidationError::InvalidFlags);
    }

    let position = (request.flags & MODE_CURSOR_MOVE != 0).then_some(CursorPosition {
        x: request.x,
        y: request.y,
    });
    if request.flags & MODE_CURSOR_BO == 0 {
        return Ok(CursorUpdate {
            position,
            image: None,
        });
    }

    if request.handle == 0 {
        return Ok(CursorUpdate {
            position,
            image: Some(CursorImage::Hide),
        });
    }

    if request.width == 0 || request.height == 0 || request.width > 64 || request.height > 64 {
        return Err(CursorValidationError::InvalidDimensions);
    }
    if request.hot_x < 0
        || request.hot_y < 0
        || request.hot_x as u32 >= request.width
        || request.hot_y as u32 >= request.height
    {
        return Err(CursorValidationError::InvalidHotspot);
    }

    let buffer = buffer.ok_or(CursorValidationError::MissingBuffer)?;
    let min_pitch = request
        .width
        .checked_mul(4)
        .ok_or(CursorValidationError::InvalidBuffer)?;
    let required_size = (buffer.pitch as usize)
        .checked_mul(request.height as usize)
        .ok_or(CursorValidationError::InvalidBuffer)?;
    if buffer.bpp != 32
        || buffer.width < request.width
        || buffer.height < request.height
        || buffer.pitch < min_pitch
        || buffer.size < required_size
    {
        return Err(CursorValidationError::InvalidBuffer);
    }

    Ok(CursorUpdate {
        position,
        image: Some(CursorImage::Buffer {
            handle: request.handle,
            width: request.width,
            height: request.height,
            hot_x: request.hot_x as u32,
            hot_y: request.hot_y as u32,
        }),
    })
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    const CRTC: u32 = 7;

    fn request(flags: u32) -> DrmModeCursor2 {
        DrmModeCursor2 {
            flags,
            crtc_id: CRTC,
            x: -9,
            y: 17,
            width: 64,
            height: 64,
            handle: 11,
            hot_x: 3,
            hot_y: 5,
        }
    }

    fn buffer() -> CursorBuffer {
        CursorBuffer {
            width: 64,
            height: 64,
            pitch: 64 * 4,
            bpp: 32,
            size: 64 * 64 * 4,
        }
    }

    #[ktest]
    fn cursor_uapi_layout_matches_linux() {
        assert_eq!(size_of::<DrmModeCursor>(), 28);
        assert_eq!(size_of::<DrmModeCursor2>(), 36);
    }

    #[ktest]
    fn legacy_cursor_uses_the_origin_hotspot() {
        let legacy = DrmModeCursor {
            flags: MODE_CURSOR_BO,
            crtc_id: CRTC,
            width: 64,
            height: 64,
            handle: 11,
            ..Default::default()
        };
        let request: DrmModeCursor2 = legacy.into();
        assert_eq!(request.hot_x, 0);
        assert_eq!(request.hot_y, 0);
        assert!(validate_cursor(request, Some(buffer()), CRTC).is_ok());
    }

    #[ktest]
    fn update_and_move_are_preserved() {
        let update = validate_cursor(
            request(MODE_CURSOR_BO | MODE_CURSOR_MOVE),
            Some(buffer()),
            CRTC,
        )
        .unwrap();
        assert_eq!(
            update,
            CursorUpdate {
                position: Some(CursorPosition { x: -9, y: 17 }),
                image: Some(CursorImage::Buffer {
                    handle: 11,
                    width: 64,
                    height: 64,
                    hot_x: 3,
                    hot_y: 5,
                }),
            }
        );
    }

    #[ktest]
    fn handle_zero_hides_without_buffer() {
        let mut req = request(MODE_CURSOR_BO);
        req.handle = 0;
        req.width = 0;
        req.height = 0;
        req.hot_x = -1;
        req.hot_y = -1;
        assert_eq!(
            validate_cursor(req, None, CRTC).unwrap(),
            CursorUpdate {
                position: None,
                image: Some(CursorImage::Hide),
            }
        );
    }

    #[ktest]
    fn move_only_does_not_resolve_a_buffer() {
        let req = request(MODE_CURSOR_MOVE);
        assert_eq!(
            validate_cursor(req, None, CRTC).unwrap(),
            CursorUpdate {
                position: Some(CursorPosition { x: -9, y: 17 }),
                image: None,
            }
        );
    }

    #[ktest]
    fn invalid_flags_and_crtc_are_rejected() {
        let mut req = request(0);
        assert_eq!(
            validate_cursor(req, Some(buffer()), CRTC),
            Err(CursorValidationError::InvalidFlags)
        );
        req.flags = MODE_CURSOR_BO | 0x80;
        assert_eq!(
            validate_cursor(req, Some(buffer()), CRTC),
            Err(CursorValidationError::InvalidFlags)
        );
        req.flags = MODE_CURSOR_BO;
        req.crtc_id += 1;
        assert_eq!(
            validate_cursor(req, Some(buffer()), CRTC),
            Err(CursorValidationError::InvalidCrtc)
        );
    }

    #[ktest]
    fn dimensions_are_nonzero_and_bounded() {
        for (width, height) in [(0, 64), (64, 0), (65, 64), (64, 65)] {
            let mut req = request(MODE_CURSOR_BO);
            req.width = width;
            req.height = height;
            assert_eq!(
                validate_cursor(req, Some(buffer()), CRTC),
                Err(CursorValidationError::InvalidDimensions)
            );
        }
    }

    #[ktest]
    fn hotspot_must_lie_inside_the_cursor() {
        for (hot_x, hot_y) in [(-1, 0), (0, -1), (64, 0), (0, 64)] {
            let mut req = request(MODE_CURSOR_BO);
            req.hot_x = hot_x;
            req.hot_y = hot_y;
            assert_eq!(
                validate_cursor(req, Some(buffer()), CRTC),
                Err(CursorValidationError::InvalidHotspot)
            );
        }
    }

    #[ktest]
    fn backing_must_cover_a_32bpp_cursor() {
        let req = request(MODE_CURSOR_BO);
        assert_eq!(
            validate_cursor(req, None, CRTC),
            Err(CursorValidationError::MissingBuffer)
        );

        let mut bad = buffer();
        bad.bpp = 24;
        assert_eq!(
            validate_cursor(req, Some(bad), CRTC),
            Err(CursorValidationError::InvalidBuffer)
        );
        bad = buffer();
        bad.width = 63;
        assert_eq!(
            validate_cursor(req, Some(bad), CRTC),
            Err(CursorValidationError::InvalidBuffer)
        );
        bad = buffer();
        bad.pitch -= 1;
        assert_eq!(
            validate_cursor(req, Some(bad), CRTC),
            Err(CursorValidationError::InvalidBuffer)
        );
        bad = buffer();
        bad.size -= 1;
        assert_eq!(
            validate_cursor(req, Some(bad), CRTC),
            Err(CursorValidationError::InvalidBuffer)
        );
    }

    #[ktest]
    fn committed_state_tracks_set_move_and_hide() {
        let mut state = CursorState::default();
        let set = validate_cursor(
            request(MODE_CURSOR_BO | MODE_CURSOR_MOVE),
            Some(buffer()),
            CRTC,
        )
        .unwrap();
        assert_eq!(state.position_for(set), CursorPosition { x: -9, y: 17 });
        state.commit(set, Some(41));
        assert_eq!(state.active_handle, Some(11));
        assert_eq!(state.resource_id, Some(41));
        assert!(state.uses_handle(11));

        let mut move_req = request(MODE_CURSOR_MOVE);
        move_req.x = 23;
        move_req.y = -4;
        let move_only = validate_cursor(move_req, None, CRTC).unwrap();
        state.commit(move_only, None);
        assert_eq!(state.position, CursorPosition { x: 23, y: -4 });
        assert_eq!(state.active_handle, Some(11));
        assert_eq!(state.resource_id, Some(41));

        let mut hide_req = request(MODE_CURSOR_BO);
        hide_req.handle = 0;
        let hide = validate_cursor(hide_req, None, CRTC).unwrap();
        assert_eq!(state.position_for(hide), CursorPosition { x: 23, y: -4 });
        state.commit(hide, None);
        assert_eq!(state.active_handle, None);
        assert_eq!(state.resource_id, None);
        assert!(!state.uses_handle(11));
    }
}
