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

use core::{
    fmt::Display,
    sync::atomic::{AtomicBool, AtomicU64, Ordering},
    time::Duration,
};

use ostd::sync::WaitQueue;
use spin::Once;

use crate::{
    events::IoEvents,
    fs::{
        file::{AccessMode, CreationFlags, FileCommon, FileLike, StatusFlags, file_table::FdFlags},
        pseudofs::AnonInodeFs,
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
};

/// One point on a monotonic fence context.
///
/// This is the minimal dma-fence equivalent: a fence is created unsignalled,
/// and the code that completes the work runs [`FenceContext::signal`] on it.
/// Signalling wakes every waiter on the context, so a waiter that watches
/// several fences only has to sleep on one queue.
#[derive(Debug)]
pub(crate) struct FenceState {
    seqno: u64,
    signaled: AtomicBool,
}

impl FenceState {
    pub(crate) fn is_signaled(&self) -> bool {
        self.signaled.load(Ordering::Acquire)
    }

    /// Returns whether this call was the one that signalled the fence.
    fn signal(&self) -> bool {
        !self.signaled.swap(true, Ordering::AcqRel)
    }
}

/// Allocates monotonically increasing fence sequence numbers and owns the
/// wait queue every fence in the context signals.
pub(crate) struct FenceContext {
    next_seqno: AtomicU64,
    signaled: AtomicU64,
    waited: AtomicU64,
    waiters: WaitQueue,
}

impl FenceContext {
    pub(crate) const fn new() -> Self {
        Self {
            next_seqno: AtomicU64::new(0),
            signaled: AtomicU64::new(0),
            waited: AtomicU64::new(0),
            waiters: WaitQueue::new(),
        }
    }

    /// Allocates an unsignalled fence at the next sequence number.
    pub(crate) fn new_fence(&self) -> Arc<FenceState> {
        let seqno = self.next_seqno.fetch_add(1, Ordering::AcqRel) + 1;
        Arc::new(FenceState {
            seqno,
            signaled: AtomicBool::new(false),
        })
    }

    pub(crate) fn signal(&self, fence: &FenceState) {
        debug_assert!(fence.seqno <= self.next_seqno.load(Ordering::Relaxed));
        if fence.signal() {
            self.signaled.fetch_add(1, Ordering::Relaxed);
            self.waiters.wake_all();
        }
    }

    #[cfg(any(ktest, all(target_arch = "riscv64", feature = "powervr_probe")))]
    pub(crate) fn created(&self) -> u64 {
        self.next_seqno.load(Ordering::Relaxed)
    }

    #[cfg(all(target_arch = "riscv64", feature = "powervr_probe"))]
    pub(crate) fn signaled(&self) -> u64 {
        self.signaled.load(Ordering::Relaxed)
    }

    #[cfg(all(target_arch = "riscv64", feature = "powervr_probe"))]
    pub(crate) fn waited(&self) -> u64 {
        self.waited.load(Ordering::Relaxed)
    }

    /// Waits until one (or, with `all`, every) fence is signalled.
    ///
    /// Returns the index of the fence that satisfied the wait. A zero timeout
    /// is answered without creating a timer, which keeps the rule usable from
    /// a unit test; an infinite wait uses the wait queue directly.
    pub(crate) fn wait_any(
        &self,
        fences: &[Arc<FenceState>],
        all: bool,
        timeout: Option<Duration>,
    ) -> Result<usize> {
        let done = || -> Option<usize> {
            let mut first = None;
            for (index, fence) in fences.iter().enumerate() {
                if fence.is_signaled() {
                    first.get_or_insert(index);
                    if !all {
                        return first;
                    }
                } else if all {
                    return None;
                }
            }
            if all { first } else { None }
        };

        let index = if let Some(index) = done() {
            index
        } else {
            match timeout {
                Some(duration) if duration.is_zero() => {
                    return_errno_with_message!(Errno::ETIME, "fence wait timed out")
                }
                Some(duration) => self.waiters.wait_until_or_timeout(done, &duration)?,
                None => self.waiters.wait_until(done),
            }
        };
        self.waited.fetch_add(1, Ordering::Relaxed);
        Ok(index)
    }
}

static FENCE_CONTEXT: Once<FenceContext> = Once::new();
static LAST_COMPLETED: Once<Arc<FenceState>> = Once::new();

/// Records the fence of the most recently completed device job.
///
/// Exporters use this as the minimal implicit-sync attachment: a dma-buf
/// handed to another device is ordered after the last job this kernel
/// completed, rather than pretending no work was ever submitted.
#[cfg(all(target_arch = "riscv64", feature = "powervr_probe"))]
pub(crate) fn record_completed(fence: &Arc<FenceState>) {
    LAST_COMPLETED.call_once(|| fence.clone());
}

/// Returns the last completed job's fence, or a freshly signalled fence when
/// no job has completed yet.
pub(crate) fn completed_or_signaled() -> Arc<FenceState> {
    if let Some(fence) = LAST_COMPLETED.get() {
        return fence.clone();
    }
    let fence = context().new_fence();
    context().signal(&fence);
    fence
}

/// The device-wide fence context.
///
/// A single context is enough while completions are signalled from kernel
/// code: sequence numbers only have to be monotonic and all waiters can share
/// one queue. Per-job contexts arrive with the vendor sync bridge.
pub(crate) fn context() -> &'static FenceContext {
    FENCE_CONTEXT.call_once(FenceContext::new)
}

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
    /// A fence describes one completed submission and retains no other file.
    fn is_scm_rights_leaf(&self) -> bool {
        true
    }

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

    #[ktest]
    fn gpu_fence_signal_and_wait() {
        let context = FenceContext::new();
        let first = context.new_fence();
        let second = context.new_fence();
        assert_eq!(context.created(), 2);
        assert!(!first.is_signaled());
        assert!(!second.is_signaled());

        // An unsignalled fence is a non-blocking miss, not a timeout timer.
        let result = context.wait_any(core::slice::from_ref(&first), false, Some(Duration::ZERO));
        assert!(result.is_err_and(|error| error.error() == Errno::ETIME));

        context.signal(&first);
        assert!(first.is_signaled());
        assert!(!second.is_signaled());
        assert_eq!(
            context
                .wait_any(core::slice::from_ref(&first), false, None)
                .unwrap(),
            0
        );
        // Signalling twice is idempotent and must not wake anyone twice.
        context.signal(&first);
        assert_eq!(
            context
                .wait_any(&[first, second], true, Some(Duration::ZERO))
                .unwrap_err()
                .error(),
            Errno::ETIME
        );
    }

    #[ktest]
    fn gpu_fence_wait_all_requires_every_fence() {
        let context = FenceContext::new();
        let first = context.new_fence();
        let second = context.new_fence();
        context.signal(&first);
        assert_eq!(
            context
                .wait_any(&[first.clone(), second.clone()], true, Some(Duration::ZERO))
                .unwrap_err()
                .error(),
            Errno::ETIME
        );
        context.signal(&second);
        assert_eq!(
            context
                .wait_any(&[first, second], true, Some(Duration::ZERO))
                .unwrap(),
            0
        );
    }
}
