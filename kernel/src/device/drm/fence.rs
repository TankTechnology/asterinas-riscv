// SPDX-License-Identifier: MPL-2.0

//! Pollable virtio-gpu 3D fences (`sync_file`-like).
//!
//! `VIRTGPU_EXECBUFFER` returns a [`FenceFile`] as the out-fence (`fence_fd`).
//! Mesa's virgl winsys polls this fd for `POLLIN` to learn that the submitted
//! render has completed.
//! A fenced `SUBMIT_3D` is queued asynchronously; its
//! control-queue completion signals the fence from interrupt context.

use core::{
    fmt::Display,
    sync::atomic::{AtomicU8, Ordering},
};

use aster_virtio::device::gpu::{GpuCommandCompletion, device::GpuCommandTicket};
use ostd::sync::WaitQueue;

use crate::{
    events::IoEvents,
    fs::{
        file::{AccessMode, CreationFlags, FileCommon, FileLike, StatusFlags, file_table::FdFlags},
        pseudofs::AnonInodeFs,
    },
    prelude::*,
    process::signal::{PollHandle, Pollable, Pollee},
};

const FENCE_PENDING: u8 = 0;
const FENCE_COMPLETED: u8 = 1;
const FENCE_SUCCEEDED: u8 = 2;
const FENCE_FAILED: u8 = 3;

/// Persistent state for one asynchronous virtio-gpu command.
pub(super) struct Fence {
    state: AtomicU8,
    ticket: Mutex<Option<GpuCommandTicket>>,
    waiters: WaitQueue,
    pollee: Pollee,
}

impl Fence {
    pub(super) fn new() -> Self {
        Self {
            state: AtomicU8::new(FENCE_PENDING),
            ticket: Mutex::new(None),
            waiters: WaitQueue::new(),
            pollee: Pollee::new(),
        }
    }

    pub(super) fn attach(&self, ticket: GpuCommandTicket) {
        let old_ticket = self.ticket.lock().replace(ticket);
        debug_assert!(old_ticket.is_none());
    }

    pub(super) fn is_signaled(&self) -> bool {
        self.state.load(Ordering::Acquire) != FENCE_PENDING
    }

    pub(super) fn try_finish(&self) -> Result<bool> {
        if !self.is_signaled() {
            return Ok(false);
        }
        self.finish_completed()?;
        Ok(true)
    }

    pub(super) fn wait(&self) -> Result<()> {
        self.waiters.wait_until(|| self.is_signaled().then_some(()));
        self.finish_completed()
    }

    fn finish_completed(&self) -> Result<()> {
        let mut ticket = self.ticket.lock();
        match self.state.load(Ordering::Acquire) {
            FENCE_SUCCEEDED => return Ok(()),
            FENCE_FAILED => {
                return_errno_with_message!(Errno::EIO, "virtio-gpu fence completed with an error");
            }
            FENCE_COMPLETED => {}
            _ => return_errno_with_message!(Errno::EBUSY, "virtio-gpu fence is pending"),
        }

        let result = ticket
            .take()
            .expect("completed virtio-gpu fence has no control ticket")
            .wait();
        let final_state = if result.is_ok() {
            FENCE_SUCCEEDED
        } else {
            FENCE_FAILED
        };
        self.state.store(final_state, Ordering::Release);
        result.map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu fence response failed"))
    }

    fn check_io_events(&self) -> IoEvents {
        if !self.is_signaled() {
            return IoEvents::empty();
        }
        match self.finish_completed() {
            Ok(()) => IoEvents::IN,
            Err(_) => IoEvents::IN | IoEvents::ERR,
        }
    }
}

impl GpuCommandCompletion for Fence {
    fn complete(&self) {
        let old_state = self.state.swap(FENCE_COMPLETED, Ordering::AcqRel);
        debug_assert_eq!(old_state, FENCE_PENDING);
        self.pollee.notify(IoEvents::IN);
        self.waiters.wake_all();
    }
}

/// A pollable file for an asynchronous virtio-gpu fence.
pub(super) struct FenceFile {
    common: FileCommon,
    fence: Arc<Fence>,
}

impl FenceFile {
    pub(super) fn new(fence: Arc<Fence>) -> Self {
        let pseudo_path = AnonInodeFs::new_path(|_| "anon_inode:[sync_file]".to_string());
        Self {
            common: FileCommon::new(pseudo_path, StatusFlags::empty()),
            fence,
        }
    }
}

impl Pollable for FenceFile {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        self.fence
            .pollee
            .poll_with(mask, poller, || self.fence.check_io_events())
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

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::Fence;

    #[ktest]
    fn pending_fence_is_nonblocking() {
        let fence = Fence::new();
        assert!(!fence.is_signaled());
        assert!(matches!(fence.try_finish(), Ok(false)));
    }
}
