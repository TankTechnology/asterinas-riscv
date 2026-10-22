// SPDX-License-Identifier: MPL-2.0

//! Legacy page flips with completion events.
//!
//! `DRM_IOCTL_MODE_PAGE_FLIP` swaps the framebuffer of an active CRTC. The
//! request is validated here and queued on the CRTC; the ioctl returns without
//! waiting for the scanout. The next completed vblank applies the flip on the
//! device's flip worker and, when `EVENT` was requested, queues a
//! `DRM_EVENT_FLIP_COMPLETE` record on the DRM file, which `poll` and `read`
//! deliver. A second flip while one is pending is `EBUSY`, as on Linux.

use alloc::{sync::Arc, vec::Vec};

use aster_core::prelude::*;
use ostd_pod::Pod;

use crate::{file::DrmFile, flip::PendingFlip, ioctl::ioctl_defs, utils::DrmRect};

/// `DRM_MODE_PAGE_FLIP_EVENT`.
const PAGE_FLIP_EVENT: u32 = 0x01;
/// `DRM_MODE_PAGE_FLIP_ASYNC`.
const PAGE_FLIP_ASYNC: u32 = 0x02;

impl DrmFile {
    pub(super) fn drm_mode_page_flip(&self, cmd: ioctl_defs::ModePageFlip) -> Result<i32> {
        let args: DrmModeCrtcPageFlip = cmd.read()?;

        if args.flags & !(PAGE_FLIP_EVENT | PAGE_FLIP_ASYNC) != 0 || args.reserved != 0 {
            return_errno_with_message!(Errno::EINVAL, "the page-flip flags are invalid");
        }

        let kms_ops = self.device().as_kms_ops().unwrap();
        let object_store = kms_ops.mode_config().object_store().lock();

        let crtc = object_store
            .lookup_crtc_by_id(args.crtc_id)
            .cloned()
            .ok_or(Errno::ENOENT)?;
        let framebuffer = self.lookup_framebuffer(args.fb_id).ok_or(Errno::ENOENT)?;

        // A flip keeps the current mode; the CRTC must already be active.
        let Some(display_mode) = crtc.state().lock().display_mode() else {
            return_errno_with_message!(Errno::EINVAL, "the CRTC has no active mode to flip");
        };

        // Keep the existing connector routing for this CRTC.
        let connector_ids: Vec<u32> = object_store
            .connectors()
            .filter(|connector| {
                connector
                    .state()
                    .lock()
                    .encoder()
                    .upgrade()
                    .and_then(|encoder| encoder.current_crtc().upgrade())
                    .is_some_and(|current| Arc::ptr_eq(&current, &crtc))
            })
            .map(|connector| connector.id())
            .collect();
        if connector_ids.is_empty() {
            return_errno_with_message!(
                Errno::EINVAL,
                "the CRTC has no routed connector to flip onto"
            );
        }

        drop(object_store);

        let source_rect = DrmRect::new(0, 0, display_mode.hdisplay(), display_mode.vdisplay());

        // Queue the flip; the vblank tick wakes the worker that applies it.
        crtc.queue_flip(PendingFlip::new(
            self.device().clone(),
            self.event_queue(),
            args.crtc_id,
            connector_ids,
            display_mode,
            framebuffer,
            source_rect,
            args.user_data,
            args.flags & PAGE_FLIP_EVENT != 0,
        ))?;

        Ok(0)
    }
}

/// `struct drm_mode_crtc_page_flip` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm_mode.h#L1179-L1195>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct DrmModeCrtcPageFlip {
    crtc_id: u32,
    fb_id: u32,
    flags: u32,
    reserved: u32,
    user_data: u64,
}
