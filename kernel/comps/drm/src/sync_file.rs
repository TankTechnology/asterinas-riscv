// SPDX-License-Identifier: MPL-2.0

//! Sync-file descriptors.
//!
//! `DRM_IOCTL_SYNCOBJ_HANDLE_TO_FD` with `EXPORT_SYNC_FILE` produces one of
//! these; `SYNCOBJ_FD_TO_HANDLE` with `IMPORT_SYNC_FILE` consumes it. The
//! descriptor is pollable and carries the fence plus the timeline point it was
//! exported at.

use alloc::{boxed::Box, sync::Arc};

use aster_core::{
    events::{EventSource, IoEvents},
    fs::{
        file::{Mappable, MappableObject, MappedObject, PerOpenFileOps, StatusFlags, SyncMode},
        vfs::{inode::FileOps, path::Path},
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
    util::ioctl::RawIoctl,
    vm::vmar::{FileMmapRequest, MapHandle},
};
use ostd::mm::{VmReader, VmWriter};

use crate::fence::DrmFence;

/// The anonymous-file name shown for a sync-file descriptor.
pub(crate) const SYNC_FILE_NAME: &str = "anon_inode:[drm-sync-file]";

/// A sync-file descriptor naming a fence at a timeline point.
pub(crate) struct DrmSyncFile {
    fence: Arc<DrmFence>,
    point: u64,
    timeline: bool,
    events: Arc<EventSource>,
}

impl core::fmt::Debug for DrmSyncFile {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("DrmSyncFile")
            .field("fence", &self.fence)
            .field("point", &self.point)
            .field("timeline", &self.timeline)
            .finish_non_exhaustive()
    }
}

impl DrmSyncFile {
    pub(crate) fn new(fence: Arc<DrmFence>, point: u64, timeline: bool) -> Self {
        // Wake pollers when the shared fence changes state.
        let events = Arc::new(EventSource::new());
        let event_sink = events.clone();
        fence.add_listener(Arc::new(move || event_sink.notify(IoEvents::IN)));

        Self {
            fence,
            point,
            timeline,
            events,
        }
    }

    pub(crate) fn fence(&self) -> Arc<DrmFence> {
        self.fence.clone()
    }

    pub(crate) fn point(&self) -> u64 {
        self.point
    }

    pub(crate) fn timeline(&self) -> bool {
        self.timeline
    }

    /// Returns whether the fence has reached this descriptor's point.
    pub(crate) fn is_signaled(&self) -> bool {
        if self.timeline {
            self.fence.is_signaled_at(self.point)
        } else {
            self.fence.is_signaled()
        }
    }
}

impl Pollable for DrmSyncFile {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        self.events.poll(mask, poller, || {
            if self.is_signaled() {
                IoEvents::IN
            } else {
                IoEvents::empty()
            }
        })
    }
}

impl FileOps for DrmSyncFile {
    fn read_at(
        &self,
        _offset: usize,
        _writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EINVAL, "reading a sync file is not supported")
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EINVAL, "writing a sync file is not supported")
    }
}

impl PerOpenFileOps for DrmSyncFile {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        false
    }

    fn mappable(&self, _request: FileMmapRequest) -> Result<MappableObject<'_>> {
        return_errno_with_message!(Errno::ENODEV, "a sync file is not mappable")
    }

    fn ioctl(&self, _path: &Path, _raw_ioctl: RawIoctl) -> Result<i32> {
        return_errno_with_message!(Errno::ENOTTY, "the sync file supports no ioctl")
    }

    fn sync(&self, _mode: SyncMode) -> Result<()> {
        Ok(())
    }
}
