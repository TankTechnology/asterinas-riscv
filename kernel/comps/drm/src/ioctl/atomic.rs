// SPDX-License-Identifier: MPL-2.0

//! Atomic KMS commits and property blobs.
//!
//! The supported commit shape is the one a single-CRTC compositor uses:
//! `ACTIVE`/`MODE_ID` on the CRTC, `CRTC_ID` on the connectors that route to
//! it, and `FB_ID`/`SRC_*` on its primary plane. Validation and application go
//! through the same [`DrmFile::commit_modeset`] path as legacy `SETCRTC`, so
//! both ioctls publish identical core state and drive the same driver hook.
//!
//! Deliberately not implemented yet (they return `EINVAL`): plane `CRTC_*`
//! destination rects other than the full mode, page-flip events, non-blocking
//! commits, cursor/overlay planes, and multi-CRTC commits.

use alloc::{boxed::Box, sync::Arc, vec::Vec};
use core::time::Duration;
use core::mem::size_of;

use aster_core::{
    fs::file::{close_file_fd, create_anon_file, with_anon_file_as},
    prelude::*,
};
use ostd::mm::VmIo;
use ostd_pod::Pod;

use super::{copy_array_from_user, kms::ScanoutSource};
use crate::{
    event::{DRM_EVENT_FLIP_COMPLETE, DrmEventVblank},
    fence::DrmFence,
    file::{DrmClientCaps, DrmFile},
    ioctl::ioctl_defs,
    kms::{
        display_mode::{DrmDisplayMode, DrmModeInfo},
        objects::{crtc::DrmCrtc, property::{DrmPropertyAttachments, DrmPropertyFlags}},
    },
    sync_file::{DrmSyncFile, SYNC_FILE_NAME},
    utils::DrmRect,
};

/// `DRM_MODE_ATOMIC_PAGE_FLIP_EVENT`.
const ATOMIC_PAGE_FLIP_EVENT: u32 = 0x0001;
/// `DRM_MODE_ATOMIC_PAGE_FLIP_ASAP`.
const ATOMIC_PAGE_FLIP_ASAP: u32 = 0x0002;
/// `DRM_MODE_ATOMIC_TEST_ONLY`.
const ATOMIC_TEST_ONLY: u32 = 0x0100;
/// `DRM_MODE_ATOMIC_NONBLOCK`.
const ATOMIC_NONBLOCK: u32 = 0x0200;
/// `DRM_MODE_ATOMIC_ALLOW_MODESET`.
const ATOMIC_ALLOW_MODESET: u32 = 0x0400;
/// The flags this implementation understands at all.
const ATOMIC_FLAGS_MASK: u32 = ATOMIC_PAGE_FLIP_EVENT
    | ATOMIC_PAGE_FLIP_ASAP
    | ATOMIC_TEST_ONLY
    | ATOMIC_NONBLOCK
    | ATOMIC_ALLOW_MODESET;

/// The largest blob a client may create.
const MAX_USER_BLOB_LEN: usize = 1024 * 1024;

impl DrmFile {
    pub(super) fn drm_mode_atomic(&self, cmd: ioctl_defs::ModeAtomic) -> Result<i32> {
        let args: DrmModeAtomic = cmd.read()?;

        if args.flags & !ATOMIC_FLAGS_MASK != 0 || args.reserved != 0 {
            return_errno_with_message!(Errno::EINVAL, "the atomic flags or reserved field are invalid");
        }
        if args.flags & ATOMIC_PAGE_FLIP_ASAP != 0 {
            return_errno_with_message!(
                Errno::EINVAL,
                "asynchronous (ASAP) atomic commits are not supported"
            );
        }
        if !self.has_client_caps(DrmClientCaps::ATOMIC) {
            return_errno_with_message!(
                Errno::EINVAL,
                "the client has not enabled DRM_CLIENT_CAP_ATOMIC"
            );
        }
        if args.count_objs == 0 {
            return_errno_with_message!(Errno::EINVAL, "the atomic commit has no objects");
        }
        let test_only = args.flags & ATOMIC_TEST_ONLY != 0;

        let object_ids = cmd.with_data_ptr(|args_ptr| {
            copy_array_from_user::<u32>(args_ptr.vm(), args.objs_ptr, args.count_objs)
        })?;
        let prop_counts = cmd.with_data_ptr(|args_ptr| {
            copy_array_from_user::<u32>(args_ptr.vm(), args.count_props_ptr, args.count_objs)
        })?;
        let total_props = prop_counts.iter().try_fold(0usize, |total, count| {
            total.checked_add(*count as usize)
        });
        let Some(total_props) = total_props else {
            return_errno_with_message!(Errno::EINVAL, "the atomic property count overflows");
        };
        let prop_ids = cmd.with_data_ptr(|args_ptr| {
            copy_array_from_user::<u32>(args_ptr.vm(), args.props_ptr, total_props as u32)
        })?;
        let prop_values = cmd.with_data_ptr(|args_ptr| {
            copy_array_from_user::<u64>(args_ptr.vm(), args.prop_values_ptr, total_props as u32)
        })?;

        let kms_ops = self.device().as_kms_ops().unwrap();
        let object_store = kms_ops.mode_config().object_store().lock();

        let mut crtc_id: Option<u32> = None;
        let mut crtc_active: Option<bool> = None;
        let mut crtc_mode_blob: Option<u32> = None;
        let mut plane_id: Option<u32> = None;
        let mut plane_fb_id: Option<u32> = None;
        let mut plane_crtc_id: Option<u32> = None;
        let mut source: [Option<u32>; 4] = [None; 4];
        let mut connector_crtcs: Vec<(u32, u32)> = Vec::new();
        let mut plane_in_fence_fd: Option<i64> = None;
        let mut crtc_out_fence_ptr: Option<u64> = None;

        let mut offset = 0usize;
        for (index, object_id) in object_ids.iter().enumerate() {
            let count = prop_counts[index] as usize;

            #[derive(Clone, Copy)]
            enum ObjectKind {
                Crtc,
                Connector,
                Plane,
            }
            let (kind, attachments): (ObjectKind, &DrmPropertyAttachments) =
                if let Some(crtc) = object_store.lookup_crtc_by_id(*object_id) {
                    (ObjectKind::Crtc, crtc.properties())
                } else if let Some(connector) = object_store.lookup_connector_by_id(*object_id) {
                    (ObjectKind::Connector, connector.properties())
                } else if let Some(plane) = object_store.lookup_plane_by_id(*object_id) {
                    (ObjectKind::Plane, plane.properties())
                } else {
                    return_errno_with_message!(Errno::ENOENT, "an atomic object does not exist");
                };

            match kind {
                ObjectKind::Crtc => {
                    if crtc_id.replace(*object_id).is_some() {
                        return_errno_with_message!(
                            Errno::EINVAL,
                            "an atomic commit may update only one CRTC"
                        );
                    }
                }
                ObjectKind::Connector => {}
                ObjectKind::Plane => {
                    if plane_id.replace(*object_id).is_some() {
                        return_errno_with_message!(
                            Errno::EINVAL,
                            "an atomic commit may update only one plane"
                        );
                    }
                }
            }

            for entry in 0..count {
                let prop_id = prop_ids[offset + entry];
                let value = prop_values[offset + entry];
                let attachment = attachments
                    .attachments()
                    .iter()
                    .find(|attachment| attachment.property().id() == prop_id)
                    .ok_or_else(|| {
                        Error::with_message(
                            Errno::EINVAL,
                            "the atomic property is not attached to the object",
                        )
                    })?;
                if attachment
                    .property()
                    .flags()
                    .contains(DrmPropertyFlags::IMMUTABLE)
                {
                    return_errno_with_message!(Errno::EINVAL, "the atomic property is immutable");
                }
                let name = attachment.property().name().as_str().unwrap_or("");

                match (kind, name) {
                    (ObjectKind::Crtc, "ACTIVE") => crtc_active = Some(value != 0),
                    (ObjectKind::Crtc, "MODE_ID") => crtc_mode_blob = Some(value as u32),
                    (ObjectKind::Crtc, "OUT_FENCE_PTR") => crtc_out_fence_ptr = Some(value),
                    (ObjectKind::Connector, "CRTC_ID") => {
                        connector_crtcs.push((*object_id, value as u32))
                    }
                    (ObjectKind::Plane, "FB_ID") => plane_fb_id = Some(value as u32),
                    (ObjectKind::Plane, "CRTC_ID") => plane_crtc_id = Some(value as u32),
                    (ObjectKind::Plane, "SRC_X") => source[0] = Some(value as u32),
                    (ObjectKind::Plane, "SRC_Y") => source[1] = Some(value as u32),
                    (ObjectKind::Plane, "SRC_W") => source[2] = Some(value as u32),
                    (ObjectKind::Plane, "SRC_H") => source[3] = Some(value as u32),
                    (ObjectKind::Plane, "IN_FENCE_FD") => plane_in_fence_fd = Some(value as i64),
                    _ => {
                        return_errno_with_message!(
                            Errno::EINVAL,
                            "the atomic property is not supported by this commit path"
                        )
                    }
                }
            }
            offset += count;
        }

        let Some(crtc_id) = crtc_id else {
            return_errno_with_message!(Errno::EINVAL, "the atomic commit has no CRTC");
        };
        let Some(active) = crtc_active else {
            return_errno_with_message!(Errno::EINVAL, "the atomic commit has no CRTC ACTIVE");
        };
        if let Some(plane_crtc_id) = plane_crtc_id
            && plane_crtc_id != 0
            && plane_crtc_id != crtc_id
        {
            return_errno_with_message!(
                Errno::EINVAL,
                "the plane is assigned to a different CRTC"
            );
        }

        let connector_ids: Vec<u32> = connector_crtcs
            .iter()
            .filter(|(_, assigned)| *assigned == crtc_id)
            .map(|(connector_id, _)| *connector_id)
            .collect();

        // An in-fence holds the commit until its producer is done. The
        // descriptor is consumed, as in Linux.
        if !test_only
            && let Some(fd) = plane_in_fence_fd
            && fd >= 0
        {
            let (fence, point, timeline) = with_anon_file_as::<DrmSyncFile, _>(fd as i32, |file| {
                (file.fence(), file.point(), file.timeline())
            })?;
            // Never wait on a producer fence without a bound: an
            // unimplemented/never-signaled fence would hang the ioctl.
            let waited = if timeline {
                fence.wait(point, Some(Duration::from_secs(1)))
            } else {
                fence.wait_signaled(Some(Duration::from_secs(1)))
            };
            if let Err(err) = waited {
                ostd::warn!("drm: atomic in-fence wait timed out (err={err:?})");
                return Err(err);
            }
            let _ = close_file_fd(fd as i32);
        }

        if !active {
            if crtc_mode_blob.unwrap_or(0) != 0 {
                return_errno_with_message!(
                    Errno::EINVAL,
                    "an inactive CRTC cannot have a mode"
                );
            }
            let crtc = object_store
                .lookup_crtc_by_id(crtc_id)
                .cloned()
                .ok_or(Errno::ENOENT)?;
            drop(object_store);
            self.commit_modeset(
                crtc_id,
                &[],
                None,
                ScanoutSource::None,
                DrmRect::default(),
                test_only,
            )?;
            self.emit_flip_complete(&crtc, crtc_id, &args, test_only);
            self.emit_out_fence(&cmd, crtc_out_fence_ptr, test_only)?;
            return Ok(0);
        }

        // Enable: a mode blob and a framebuffer are mandatory.
        let Some(plane_id) = plane_id else {
            return_errno_with_message!(Errno::EINVAL, "the atomic commit has no plane");
        };
        let blob_id = crtc_mode_blob.unwrap_or(0);
        if blob_id == 0 {
            return_errno_with_message!(Errno::EINVAL, "an active CRTC needs a MODE_ID blob");
        }
        let Some(blob) = object_store.lookup_property_blob(blob_id) else {
            return_errno_with_message!(Errno::ENOENT, "the MODE_ID blob does not exist");
        };
        if blob.len() < size_of::<DrmModeInfo>() {
            return_errno_with_message!(Errno::EINVAL, "the MODE_ID blob is too small");
        }
        let display_mode = DrmDisplayMode::try_from(DrmModeInfo::from_first_bytes(blob.data()))?;

        let Some(framebuffer_id) = plane_fb_id else {
            return_errno_with_message!(Errno::EINVAL, "the primary plane has no FB_ID");
        };
        let framebuffer = self.lookup_framebuffer(framebuffer_id).ok_or(Errno::ENOENT)?;
        let framebuffer_size = framebuffer.size();

        let source_rect = match source {
            [Some(x), Some(y), Some(width), Some(height)] => DrmRect::new(
                x >> 16,
                y >> 16,
                width >> 16,
                height >> 16,
            ),
            [None, None, None, None] => {
                DrmRect::new(0, 0, framebuffer_size.width(), framebuffer_size.height())
            }
            _ => {
                return_errno_with_message!(
                    Errno::EINVAL,
                    "the atomic source rectangle must be fully specified"
                )
            }
        };
        let Some(plane) = object_store.lookup_plane_by_id(plane_id) else {
            return_errno_with_message!(Errno::ENOENT, "the primary plane does not exist");
        };
        // The commit path drives the CRTC's primary plane; another plane would
        // need a compositing path this driver does not have.
        let Some(crtc) = object_store.lookup_crtc_by_id(crtc_id).cloned() else {
            return_errno_with_message!(Errno::ENOENT, "the CRTC does not exist");
        };
        let Some(primary_plane) = crtc.primary_plane().upgrade() else {
            return_errno_with_message!(Errno::ENOENT, "the CRTC has no primary plane");
        };
        if !Arc::ptr_eq(plane, &primary_plane) {
            return_errno_with_message!(
                Errno::EINVAL,
                "atomic commits may only drive the primary plane"
            );
        }

        drop(object_store);

        self.commit_modeset(
            crtc_id,
            &connector_ids,
            Some(display_mode),
            ScanoutSource::Framebuffer(framebuffer),
            source_rect,
            test_only,
        )?;
        self.emit_flip_complete(&crtc, crtc_id, &args, test_only);
        self.emit_out_fence(&cmd, crtc_out_fence_ptr, test_only)?;

        Ok(0)
    }

    /// Writes an out-fence descriptor to the user pointer named by
    /// `OUT_FENCE_PTR`.
    ///
    /// The commit has completed synchronously, so the fence is already
    /// signaled when userspace receives it.
    fn emit_out_fence(
        &self,
        cmd: &ioctl_defs::ModeAtomic,
        out_fence_ptr: Option<u64>,
        test_only: bool,
    ) -> Result<()> {
        let Some(ptr) = out_fence_ptr else {
            return Ok(());
        };
        if test_only || ptr == 0 {
            return Ok(());
        }

        let fence = DrmFence::new_signaled();
        let fd = create_anon_file(
            Box::new(DrmSyncFile::new(fence, 0, false)),
            true,
            SYNC_FILE_NAME,
        )?;
        cmd.with_data_ptr(|args_ptr| {
            args_ptr
                .vm()
                .write_val::<u64>(ptr as usize, &(fd as u64))
                .map_err(|_| Error::with_message(Errno::EFAULT, "the out-fence pointer is invalid"))
        })?;

        Ok(())
    }

    /// Queues `DRM_EVENT_FLIP_COMPLETE` for a committed atomic request.
    fn emit_flip_complete(
        &self,
        crtc: &DrmCrtc,
        crtc_id: u32,
        args: &DrmModeAtomic,
        test_only: bool,
    ) {
        if test_only || args.flags & ATOMIC_PAGE_FLIP_EVENT == 0 {
            return;
        }
        self.enqueue_event(DrmEventVblank::new(
            DRM_EVENT_FLIP_COMPLETE,
            args.user_data,
            aster_core::monotonic_now(),
            crtc.vblank_sequence(),
            crtc_id,
        ));
    }

    pub(super) fn drm_mode_create_blob(
        &self,
        cmd: ioctl_defs::ModeCreatePropBlob,
    ) -> Result<i32> {
        let mut args: DrmModeCreateBlob = cmd.read()?;
        if args.length == 0 || args.length as usize > MAX_USER_BLOB_LEN {
            return_errno_with_message!(Errno::EINVAL, "the property blob length is invalid");
        }
        let data = cmd.with_data_ptr(|args_ptr| {
            copy_array_from_user::<u8>(args_ptr.vm(), args.data, args.length)
        })?;

        let kms_ops = self.device().as_kms_ops().unwrap();
        let blob = kms_ops
            .mode_config()
            .object_store()
            .lock()
            .create_property_blob(data.into_boxed_slice())?;
        self.add_user_blob(blob.id());

        args.blob_id = blob.id();
        cmd.write(&args)?;

        Ok(0)
    }

    pub(super) fn drm_mode_destroy_blob(
        &self,
        cmd: ioctl_defs::ModeDestroyPropBlob,
    ) -> Result<i32> {
        let args: DrmModeDestroyBlob = cmd.read()?;
        self.remove_user_blob(args.blob_id)?;

        let kms_ops = self.device().as_kms_ops().unwrap();
        kms_ops
            .mode_config()
            .object_store()
            .lock()
            .destroy_property_blob(args.blob_id)?;

        Ok(0)
    }
}

/// `struct drm_mode_atomic` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm_mode.h#L1332-L1341>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct DrmModeAtomic {
    flags: u32,
    count_objs: u32,
    objs_ptr: u64,
    count_props_ptr: u64,
    props_ptr: u64,
    prop_values_ptr: u64,
    reserved: u64,
    user_data: u64,
}

/// `struct drm_mode_create_blob` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm_mode.h#L1399-L1406>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct DrmModeCreateBlob {
    data: u64,
    length: u32,
    blob_id: u32,
}

/// `struct drm_mode_destroy_blob` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm_mode.h#L1420-L1422>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct DrmModeDestroyBlob {
    blob_id: u32,
}
