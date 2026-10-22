// SPDX-License-Identifier: MPL-2.0

//! DRM sync-object ioctls.
//!
//! The handled surface is the one a compositor uses directly: create,
//! destroy, binary signal/reset/wait, timeline signal/wait, and query. Fence
//! import/export (`SYNCOBJ_HANDLE_TO_FD`/`FD_TO_HANDLE`) is not implemented;
//! this kernel has no dma-fence layer to back it yet.

use alloc::{boxed::Box, sync::Arc, vec::Vec};
use core::time::Duration;

use aster_core::{
    fs::file::{create_anon_file, with_anon_file_as},
    monotonic_now, prelude::*, wait_until_or_timeout,
};
use ostd::sync::Waker;

use super::{copy_array_from_user, copy_array_to_user};
use crate::{
    file::DrmFile,
    ioctl::ioctl_defs,
    sync_file::{DrmSyncFile, SYNC_FILE_NAME},
    syncobj::DrmSyncObj,
};

/// `DRM_SYNCOBJ_CREATE_SIGNALED`.
const SYNC_CREATE_SIGNALED: u32 = 1 << 0;

/// `DRM_SYNCOBJ_WAIT_FLAGS_WAIT_ALL`.
const WAIT_ALL: u32 = 1 << 0;
/// `DRM_SYNCOBJ_WAIT_FLAGS_WAIT_FOR_SUBMIT`.
const WAIT_FOR_SUBMIT: u32 = 1 << 1;
/// `DRM_SYNCOBJ_WAIT_FLAGS_WAIT_AVAILABLE`.
const WAIT_AVAILABLE: u32 = 1 << 2;
/// `DRM_SYNCOBJ_WAIT_FLAGS_WAIT_DEADLINE`.
const WAIT_DEADLINE: u32 = 1 << 3;

/// `DRM_SYNCOBJ_QUERY_FLAGS_LAST_SUBMITTED`.
const QUERY_LAST_SUBMITTED: u32 = 1 << 0;

/// `DRM_SYNCOBJ_HANDLE_TO_FD_FLAGS_EXPORT_SYNC_FILE` /
/// `DRM_SYNCOBJ_FD_TO_HANDLE_FLAGS_IMPORT_SYNC_FILE`.
const SYNC_FILE_FLAG: u32 = 1 << 0;
/// `DRM_SYNCOBJ_*_FLAGS_TIMELINE`.
const TIMELINE_FLAG: u32 = 1 << 1;

impl DrmFile {
    pub(super) fn drm_syncobj_create(&self, cmd: ioctl_defs::SyncObjCreate) -> Result<i32> {
        let mut args: DrmSyncObjCreate = cmd.read()?;
        if args.flags & !SYNC_CREATE_SIGNALED != 0 {
            return_errno_with_message!(Errno::EINVAL, "the sync-object create flags are invalid");
        }

        let object = Arc::new(DrmSyncObj::new(args.flags & SYNC_CREATE_SIGNALED != 0));
        args.handle = self.add_syncobj(object)?;
        cmd.write(&args)?;

        Ok(0)
    }

    pub(super) fn drm_syncobj_destroy(&self, cmd: ioctl_defs::SyncObjDestroy) -> Result<i32> {
        let args: DrmSyncObjDestroy = cmd.read()?;
        self.remove_syncobj(args.handle)?;

        Ok(0)
    }

    pub(super) fn drm_syncobj_handle_to_fd(
        &self,
        cmd: ioctl_defs::SyncObjHandleToFd,
    ) -> Result<i32> {
        let mut args: DrmSyncObjHandle = cmd.read()?;
        if args.flags & !(SYNC_FILE_FLAG | TIMELINE_FLAG) != 0 {
            return_errno_with_message!(Errno::EINVAL, "the sync-file export flags are invalid");
        }
        if args.flags & SYNC_FILE_FLAG == 0 {
            return_errno_with_message!(
                Errno::EOPNOTSUPP,
                "only sync-file export is supported"
            );
        }

        let point = if args.flags & TIMELINE_FLAG != 0 {
            args.point
        } else {
            0
        };
        let object = self.lookup_syncobj(args.handle)?;
        let timeline = args.flags & TIMELINE_FLAG != 0;
        let file = DrmSyncFile::new(object.fence(), point, timeline);
        args.fd = create_anon_file(Box::new(file), false, SYNC_FILE_NAME)?;
        cmd.write(&args)?;

        Ok(0)
    }

    pub(super) fn drm_syncobj_fd_to_handle(
        &self,
        cmd: ioctl_defs::SyncObjFdToHandle,
    ) -> Result<i32> {
        let mut args: DrmSyncObjHandle = cmd.read()?;
        if args.flags & !(SYNC_FILE_FLAG | TIMELINE_FLAG) != 0 {
            return_errno_with_message!(Errno::EINVAL, "the sync-file import flags are invalid");
        }
        if args.flags & SYNC_FILE_FLAG == 0 {
            return_errno_with_message!(
                Errno::EOPNOTSUPP,
                "only sync-file import is supported"
            );
        }

        let (fence, timeline) = with_anon_file_as::<DrmSyncFile, _>(args.fd, |file| {
            (file.fence(), file.timeline())
        })?;

        // The imported object shares the exported fence. A signal through the
        // imported handle therefore also signals the exporter, which differs
        // from Linux's copied-fence semantics; waits are unaffected.
        let object = Arc::new(DrmSyncObj::from_fence(fence, timeline));
        args.handle = self.add_syncobj(object)?;
        cmd.write(&args)?;

        Ok(0)
    }

    pub(super) fn drm_syncobj_signal(&self, cmd: ioctl_defs::SyncObjSignal) -> Result<i32> {
        let args: DrmSyncObjArray = cmd.read()?;
        let handles = cmd.with_data_ptr(|args_ptr| {
            copy_array_from_user::<u32>(args_ptr.vm(), args.handles, args.count_handles)
        })?;

        for handle in handles {
            self.lookup_syncobj(handle)?.signal();
        }

        Ok(0)
    }

    pub(super) fn drm_syncobj_timeline_signal(
        &self,
        cmd: ioctl_defs::SyncObjTimelineSignal,
    ) -> Result<i32> {
        let args: DrmSyncObjTimelineArray = cmd.read()?;
        let (handles, points) = cmd.with_data_ptr(|args_ptr| {
            let vm = args_ptr.vm();
            let handles = copy_array_from_user::<u32>(vm, args.handles, args.count_handles)?;
            let points = copy_array_from_user::<u64>(vm, args.points, args.count_handles)?;
            Ok((handles, points))
        })?;

        for (handle, point) in handles.into_iter().zip(points) {
            self.lookup_syncobj(handle)?.signal_timeline(point);
        }

        Ok(0)
    }

    pub(super) fn drm_syncobj_reset(&self, cmd: ioctl_defs::SyncObjReset) -> Result<i32> {
        let args: DrmSyncObjArray = cmd.read()?;
        let handles = cmd.with_data_ptr(|args_ptr| {
            copy_array_from_user::<u32>(args_ptr.vm(), args.handles, args.count_handles)
        })?;

        for handle in handles {
            self.lookup_syncobj(handle)?.reset();
        }

        Ok(0)
    }

    pub(super) fn drm_syncobj_query(&self, cmd: ioctl_defs::SyncObjQuery) -> Result<i32> {
        let args: DrmSyncObjTimelineArray = cmd.read()?;
        if args.flags & !QUERY_LAST_SUBMITTED != 0 {
            return_errno_with_message!(Errno::EINVAL, "the sync-object query flags are invalid");
        }
        let handles = cmd.with_data_ptr(|args_ptr| {
            copy_array_from_user::<u32>(args_ptr.vm(), args.handles, args.count_handles)
        })?;

        let mut points = Vec::with_capacity(handles.len());
        for handle in handles {
            points.push(self.lookup_syncobj(handle)?.timeline_value());
        }

        cmd.with_data_ptr(|args_ptr| {
            copy_array_to_user(args_ptr.vm(), args.points, args.count_handles, &points)
        })?;

        Ok(0)
    }

    pub(super) fn drm_syncobj_wait(&self, cmd: ioctl_defs::SyncObjWait) -> Result<i32> {
        let mut args: DrmSyncObjWait = cmd.read()?;
        let handles = cmd.with_data_ptr(|args_ptr| {
            copy_array_from_user::<u32>(args_ptr.vm(), args.handles, args.count_handles)
        })?;
        let objects = self.lookup_syncobjs_with_points(&handles, None)?;
        let timeout = timeout_from_nsec(args.timeout_nsec)?;

        let index = wait_on_objects(&objects, args.flags, timeout)?;
        if args.flags & WAIT_ALL == 0 {
            args.first_signaled = index as u32;
            cmd.write(&args)?;
        }

        Ok(0)
    }

    pub(super) fn drm_syncobj_timeline_wait(
        &self,
        cmd: ioctl_defs::SyncObjTimelineWait,
    ) -> Result<i32> {
        let mut args: DrmSyncObjTimelineWait = cmd.read()?;
        let (handles, points) = cmd.with_data_ptr(|args_ptr| {
            let vm = args_ptr.vm();
            let handles = copy_array_from_user::<u32>(vm, args.handles, args.count_handles)?;
            let points = copy_array_from_user::<u64>(vm, args.points, args.count_handles)?;
            Ok((handles, points))
        })?;
        let objects = self.lookup_syncobjs_with_points(&handles, Some(points))?;
        let timeout = timeout_from_nsec(args.timeout_nsec)?;

        let index = wait_on_objects(&objects, args.flags, timeout)?;
        if args.flags & WAIT_ALL == 0 {
            args.first_signaled = index as u32;
            cmd.write(&args)?;
        }

        Ok(0)
    }

    fn lookup_syncobjs_with_points(
        &self,
        handles: &[u32],
        points: Option<Vec<u64>>,
    ) -> Result<Vec<(Arc<DrmSyncObj>, Option<u64>)>> {
        if handles.is_empty() {
            return_errno_with_message!(Errno::EINVAL, "no sync-object handles were given");
        }
        let mut objects = Vec::with_capacity(handles.len());
        for (index, handle) in handles.iter().enumerate() {
            let point = points.as_ref().map(|points| {
                debug_assert_eq!(points.len(), handles.len());
                points[index]
            });
            objects.push((self.lookup_syncobj(*handle)?, point));
        }
        Ok(objects)
    }
}

/// Validates the wait flags and converts the UAPI timeout.
///
/// A timeout of zero is a non-blocking probe; a negative value is relative to
/// now; a positive value is an absolute `CLOCK_MONOTONIC` deadline.
fn timeout_from_nsec(timeout_nsec: i64) -> Result<Option<Duration>> {
    if timeout_nsec == 0 {
        return Ok(Some(Duration::ZERO));
    }
    if timeout_nsec < 0 {
        return Ok(Some(Duration::from_nanos(timeout_nsec.unsigned_abs())));
    }

    let deadline = Duration::from_nanos(timeout_nsec as u64);
    Ok(Some(deadline.saturating_sub(monotonic_now())))
}

/// Waits on the objects and returns the index of a satisfied one.
fn wait_on_objects(
    objects: &[(Arc<DrmSyncObj>, Option<u64>)],
    flags: u32,
    timeout: Option<Duration>,
) -> Result<usize> {
    if flags & !(WAIT_ALL | WAIT_FOR_SUBMIT) != 0 {
        return_errno_with_message!(
            Errno::EINVAL,
            "the sync-object wait flags are not supported"
        );
    }
    let wait_all = flags & WAIT_ALL != 0;

    let satisfied = || {
        if wait_all {
            objects
                .iter()
                .all(|(object, point)| object.is_satisfied(*point))
                .then_some(0)
        } else {
            objects
                .iter()
                .position(|(object, point)| object.is_satisfied(*point))
        }
    };

    if let Some(index) = satisfied() {
        return Ok(index);
    }
    if timeout == Some(Duration::ZERO) {
        return_errno_with_message!(Errno::ETIME, "the sync objects are not signaled");
    }

    wait_until_or_timeout(
        || satisfied().is_some(),
        |waker: Arc<Waker>| {
            for (object, _) in objects {
                object.register_waker(waker.clone());
            }
        },
        timeout,
    )?;

    satisfied().ok_or_else(|| Error::with_message(Errno::ETIME, "the sync objects are not signaled"))
}

/// `struct drm_syncobj_create` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm.h#L918-L922>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct DrmSyncObjCreate {
    handle: u32,
    flags: u32,
}

/// `struct drm_syncobj_destroy` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm.h#L924-L927>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct DrmSyncObjDestroy {
    handle: u32,
    pad: u32,
}

/// `struct drm_syncobj_array` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm.h#L1016-L1020>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct DrmSyncObjArray {
    handles: u64,
    count_handles: u32,
    pad: u32,
}

/// `struct drm_syncobj_timeline_array` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm.h#L1023-L1028>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct DrmSyncObjTimelineArray {
    handles: u64,
    points: u64,
    count_handles: u32,
    flags: u32,
}

/// `struct drm_syncobj_wait` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm.h#L956-L972>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct DrmSyncObjWait {
    handles: u64,
    timeout_nsec: i64,
    count_handles: u32,
    flags: u32,
    first_signaled: u32,
    pad: u32,
    deadline_nsec: u64,
}

/// `struct drm_syncobj_timeline_wait` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm.h#L974-L992>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct DrmSyncObjTimelineWait {
    handles: u64,
    points: u64,
    timeout_nsec: i64,
    count_handles: u32,
    flags: u32,
    first_signaled: u32,
    pad: u32,
    deadline_nsec: u64,
}

/// `struct drm_syncobj_handle` in Linux, shared by handle-to-fd and
/// fd-to-handle.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/drm/drm.h#L933-L941>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct DrmSyncObjHandle {
    handle: u32,
    flags: u32,
    fd: i32,
    pad: u32,
    point: u64,
}
