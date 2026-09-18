// SPDX-License-Identifier: MPL-2.0

//! A file that reports a completed 3D submission.
//!
//! A client that submits work can ask for a descriptor it may `poll` until the
//! work retires, and that descriptor has to be a real file: the alternative is
//! handing back a number that names nothing, and `poll` on a descriptor that
//! names nothing is `EBADF` rather than a wait.
//!
//! There is no wake-up here because there is nothing to wake up for. The
//! submission this fence stands for has already completed by the time the
//! fence exists — the driver waits for the host before returning — so the
//! fence is created in the state a client is waiting to reach, and polling it
//! succeeds immediately.

use core::fmt::Display;

use crate::{
    events::IoEvents,
    fs::{
        file::{AccessMode, CreationFlags, FileCommon, FileLike, StatusFlags, file_table::FdFlags},
        pseudofs::AnonInodeFs,
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
};

/// A descriptor that reports its work as already finished.
pub(super) struct FenceFile {
    common: FileCommon,
}

impl FenceFile {
    /// Creates a fence for work that has already completed.
    pub(super) fn new_signalled() -> Self {
        let path = AnonInodeFs::new_path(|_| "anon_inode:[drm-fence]".to_string());
        Self {
            common: FileCommon::new(path, StatusFlags::empty()),
        }
    }
}

impl Pollable for FenceFile {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        // Readable from the moment it exists, and no poller is registered
        // because there is no later state to reach.
        mask & IoEvents::IN
    }
}

impl FileLike for FenceFile {
    /// Reading a completed fence succeeds without producing bytes: the
    /// completion is the whole of what it has to report.
    fn read(&self, _writer: &mut VmWriter) -> Result<usize> {
        Ok(0)
    }

    fn access_mode(&self) -> AccessMode {
        AccessMode::O_RDONLY
    }

    fn common(&self) -> &FileCommon {
        &self.common
    }

    fn dump_proc_fdinfo(self: Arc<Self>, fd_flags: FdFlags) -> Box<dyn Display> {
        struct FdInfo {
            inner: Arc<FenceFile>,
            fd_flags: FdFlags,
        }

        impl Display for FdInfo {
            fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
                let mut flags =
                    self.inner.common.status_flags().bits() | self.inner.access_mode() as u32;
                if self.fd_flags.contains(FdFlags::CLOEXEC) {
                    flags |= CreationFlags::O_CLOEXEC.bits();
                }
                writeln!(f, "pos:\t{}", 0)?;
                writeln!(f, "flags:\t0{:o}", flags)?;
                writeln!(f, "mnt_id:\t{}", AnonInodeFs::mount_node().id())?;
                writeln!(f, "ino:\t{}", AnonInodeFs::shared_inode().ino())?;
                write!(f, "drm-fence:\tsignalled")
            }
        }

        Box::new(FdInfo {
            inner: self,
            fd_flags,
        })
    }
}
