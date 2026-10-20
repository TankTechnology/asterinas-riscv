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

/// The readiness a completed fence reports for a given request mask.
///
/// A free function so the rule can be checked without a file: building one
/// needs `FileCommon`, which needs the system clock, which a kernel unit test
/// does not have — the rule itself needs none of that.
fn signalled_readiness(mask: IoEvents) -> IoEvents {
    // Readable from the moment it exists. Only the bits the caller asked
    // about are reported: `epoll` requests `EPOLLOUT` whenever the caller
    // lists it, so a descriptor that answers writable without being asked
    // makes `epoll_wait` return immediately, every time — the caller is not
    // waiting, it is spinning. The DRM descriptor itself was fixed for exactly
    // this; the fence has the same shape and gets the same rule.
    mask & IoEvents::IN
}

impl Pollable for FenceFile {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        // No poller is registered because there is no later state to reach.
        signalled_readiness(mask)
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

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    // What is testable here is the readiness rule, not a `FenceFile`: building
    // one goes through `FileCommon::new`, which reads the system clock, and a
    // kernel unit test does not have one — the first attempt at these tests
    // panicked in `time/clocks/system_wide.rs` and then poisoned the clock's
    // `Once`, taking three unrelated tests with it. So the rule is a free
    // function and these drive it directly. `read`, `access_mode` and the
    // fdinfo line are unreachable from here and are covered by the guest
    // gates, which exercise a real fence through `VIRTGPU_EXECBUFFER`.

    #[ktest]
    fn a_signalled_fence_reports_readable() {
        assert_eq!(signalled_readiness(IoEvents::IN), IoEvents::IN);
        assert!(signalled_readiness(IoEvents::IN).contains(IoEvents::IN));
    }

    #[ktest]
    fn a_signalled_fence_reports_only_what_it_was_asked_about() {
        assert_eq!(signalled_readiness(IoEvents::OUT), IoEvents::empty());
        assert_eq!(signalled_readiness(IoEvents::empty()), IoEvents::empty());
        assert_eq!(
            signalled_readiness(IoEvents::IN | IoEvents::OUT),
            IoEvents::IN,
            "the fence must not claim writability nobody asked about",
        );
    }
}
