// SPDX-License-Identifier: MPL-2.0

//! A pollable fence file (`sync_file`-like) for virtio-gpu 3D synchronization.
//!
//! `VIRTGPU_EXECBUFFER` returns a [`FenceFile`] as the out-fence (`fence_fd`).
//! Mesa's virgl winsys polls this fd for `POLLIN` to learn that the submitted
//! render has completed. Because the Asterinas virtio-gpu transport is
//! synchronous — a fenced `SUBMIT_3D` defers its response until the host
//! finishes the command — the fence is already signaled by the time the ioctl
//! returns, so [`FenceFile`] reports `POLLIN` immediately.

use core::fmt::Display;

use crate::{
    events::IoEvents,
    fs::{
        file::{
            AccessMode, CreationFlags, FileCommon, FileLike, StatusFlags, file_table::FdFlags,
        },
        pseudofs::AnonInodeFs,
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
};

/// A pre-signaled fence file: always readable (`POLLIN`).
pub(super) struct FenceFile {
    common: FileCommon,
}

impl FenceFile {
    pub(super) fn new() -> Self {
        let pseudo_path = AnonInodeFs::new_path(|_| "anon_inode:[sync_file]".to_string());
        Self {
            common: FileCommon::new(pseudo_path, StatusFlags::empty()),
        }
    }
}

impl Pollable for FenceFile {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        // The fence is already signaled (the render completed synchronously),
        // so the file is always readable.
        mask & IoEvents::IN
    }
}

impl FileLike for FenceFile {
    fn access_mode(&self) -> AccessMode {
        AccessMode::O_RDWR
    }

    fn common(&self) -> &FileCommon {
        &self.common
    }

    fn dump_proc_fdinfo(self: Arc<Self>, fd_flags: FdFlags) -> Box<dyn Display> {
        struct FdInfo {
            flags: u32,
        }
        impl Display for FdInfo {
            fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
                writeln!(f, "pos:\t{}", 0)?;
                writeln!(f, "flags:\t0{:o}", self.flags)
            }
        }
        let mut flags = self.common.status_flags().bits() | self.access_mode() as u32;
        if fd_flags.contains(FdFlags::CLOEXEC) {
            flags |= CreationFlags::O_CLOEXEC.bits();
        }
        Box::new(FdInfo { flags })
    }
}
