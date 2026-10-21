// SPDX-License-Identifier: MPL-2.0

//! Legacy page flips with completion events.
//!
//! `DRM_IOCTL_MODE_PAGE_FLIP` swaps the framebuffer of an active CRTC. The
//! driver has no hardware flip queue, so the transfer/flush runs inside the
//! ioctl; what the `EVENT` flag adds is the asynchronous half clients actually
//! depend on — a `DRM_EVENT_FLIP_COMPLETE` record on the DRM file, delivered
//! through `poll` and `read`.

use alloc::{sync::Arc, vec::Vec};

use aster_core::prelude::*;
use ostd_pod::Pod;

use super::kms::ScanoutSource;
use crate::{
    event::{DRM_EVENT_FLIP_COMPLETE, DrmEventVblank},
    file::DrmFile,
    ioctl::ioctl_defs,
    utils::DrmRect,
};

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
        self.commit_modeset(
            args.crtc_id,
            &connector_ids,
            Some(display_mode),
            ScanoutSource::Framebuffer(framebuffer),
            source_rect,
            false,
        )?;

        if args.flags & PAGE_FLIP_EVENT != 0 {
            self.enqueue_event(DrmEventVblank::new(
                DRM_EVENT_FLIP_COMPLETE,
                args.user_data,
                aster_core::monotonic_now(),
                crtc.vblank_sequence(),
                args.crtc_id,
            ));
        }

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
