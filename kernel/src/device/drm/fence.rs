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
    mem,
    sync::atomic::{AtomicU8, AtomicUsize, Ordering},
};

use aster_virtio::device::gpu::{GpuCommandCompletion, device::GpuCommandTicket};
use ostd::sync::{LocalIrqDisabled, WaitQueue};

use crate::{
    events::IoEvents,
    fs::{
        file::{AccessMode, CreationFlags, FileCommon, FileLike, StatusFlags, file_table::FdFlags},
        pseudofs::AnonInodeFs,
    },
    prelude::*,
    process::signal::{PollHandle, Pollable, Pollee},
};

#[repr(u8)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum FenceState {
    Pending,
    Completed,
    Succeeded,
    Failed,
}

/// Persistent state for one asynchronous virtio-gpu command.
pub(super) struct Fence {
    state: AtomicU8,
    ticket: Mutex<Option<GpuCommandTicket>>,
    dependencies: SpinLock<Vec<Arc<Fence>>, LocalIrqDisabled>,
    callbacks: SpinLock<Vec<Box<dyn FnOnce() + Send>>, LocalIrqDisabled>,
    pollee: Pollee,
    waiters: WaitQueue,
}

impl Fence {
    pub(super) fn new() -> Self {
        Self::with_dependencies(Vec::new())
    }

    fn with_dependencies(dependencies: Vec<Arc<Fence>>) -> Self {
        Self {
            state: AtomicU8::new(FenceState::Pending as u8),
            ticket: Mutex::new(None),
            dependencies: SpinLock::new(dependencies),
            callbacks: SpinLock::new(Vec::new()),
            pollee: Pollee::new(),
            waiters: WaitQueue::new(),
        }
    }

    /// Returns an already-signaled software fence.
    pub(super) fn new_signaled() -> Arc<Self> {
        let fence = Arc::new(Self::new());
        fence.signal_success();
        fence
    }

    /// Builds a fence that signals after every dependency has completed.
    ///
    /// Error status is deliberately not propagated:
    /// DRM synchronization treats a failed producer as a completed dependency,
    /// just like a `sync_file` wait.
    pub(super) fn chain(previous: Option<Arc<Self>>, current: Arc<Self>) -> Arc<Self> {
        let mut pending = Vec::with_capacity(2);
        if let Some(previous) = previous
            && !Arc::ptr_eq(&previous, &current)
        {
            pending.push(previous);
        }
        pending.push(current);

        // Flatten nested chains to their unsignaled leaves. Besides making
        // polling linear in the dependency graph, this prevents completion of
        // one old fence from recursively signaling thousands of timeline
        // points on the interrupt stack.
        let mut dependencies = Vec::new();
        let mut visited = BTreeSet::new();
        while let Some(dependency) = pending.pop() {
            if !visited.insert(Arc::as_ptr(&dependency) as usize) || dependency.is_signaled() {
                continue;
            }
            let nested = dependency.dependencies.lock().clone();
            if nested.is_empty() {
                dependencies.push(dependency);
            } else {
                pending.extend(nested);
            }
        }
        if dependencies.is_empty() {
            return Self::new_signaled();
        }

        // Keep the dependencies until the chained fence signals. Besides
        // preserving their lifetime, this lets a syncobj waiter actively poll
        // the underlying virtio command ticket when no fence fd is being
        // polled by userspace.
        let chained = Arc::new(Self::with_dependencies(dependencies.clone()));
        let barrier = Arc::new(FenceBarrier {
            remaining: AtomicUsize::new(dependencies.len()),
            output: chained.clone(),
        });
        for dependency in dependencies {
            let barrier = barrier.clone();
            dependency.on_signal(move || barrier.complete_one());
        }
        chained
    }

    pub(super) fn attach(&self, ticket: GpuCommandTicket) {
        let old_ticket = self.ticket.lock().replace(ticket);
        debug_assert!(old_ticket.is_none());
    }

    pub(super) fn is_signaled(&self) -> bool {
        self.is_signaled_raw()
    }

    /// Actively advances device-backed dependencies before checking the state.
    pub(super) fn poll_and_is_signaled(&self) -> bool {
        self.poll_device_completion();
        self.is_signaled_raw()
    }

    /// Runs `callback` exactly once after this fence becomes signaled.
    ///
    /// Registration is safe against a concurrent virtio completion: the state
    /// is checked again while holding the callback lock, after the completion
    /// side has published its state.
    pub(super) fn on_signal(&self, callback_fn: impl FnOnce() + Send + 'static) {
        if self.is_signaled() {
            callback_fn();
            return;
        }

        let mut callbacks = self.callbacks.lock();
        // Do not poll while holding the callback lock: completing a ticket
        // invokes `run_callbacks` and would deadlock on this same lock.
        if self.is_signaled_raw() {
            drop(callbacks);
            callback_fn();
        } else {
            callbacks.push(Box::new(callback_fn));
        }
    }

    /// Signals a software-backed fence after a successful KMS commit.
    pub(super) fn signal_success(&self) {
        self.signal(FenceState::Succeeded, IoEvents::IN);
    }

    /// Signals a software-backed fence after a failed asynchronous KMS commit.
    pub(super) fn signal_failure(&self) {
        self.signal(FenceState::Failed, IoEvents::IN | IoEvents::ERR);
    }

    fn signal(&self, state: FenceState, events: IoEvents) {
        let old_state = self.state.swap(state as u8, Ordering::AcqRel);
        debug_assert_eq!(old_state, FenceState::Pending as u8);
        self.run_callbacks();
        self.waiters.wake_all();
        self.pollee.notify(events);
    }

    fn run_callbacks(&self) {
        // A signaled chain no longer needs to retain its dependency graph.
        self.dependencies.lock().clear();
        let callbacks = mem::take(&mut *self.callbacks.lock());
        for callback in callbacks {
            callback();
        }
    }

    pub(super) fn try_finish(&self) -> Result<bool> {
        self.poll_device_completion();
        if !self.is_signaled() {
            return Ok(false);
        }
        self.finish_completed()?;
        Ok(true)
    }

    pub(super) fn wait(&self) -> Result<()> {
        self.wait_until_signaled();
        self.finish_completed()
    }

    fn wait_until_signaled(&self) {
        self.waiters.wait_until(|| {
            self.poll_device_completion();
            self.is_signaled().then_some(())
        });
    }

    /// Waits for a producer dependency without propagating its stored status.
    ///
    /// Linux input fences gate consumers on completion. A signaled fence is a
    /// satisfied dependency even when its producer recorded an error.
    pub(super) fn wait_for_dependency(&self) {
        self.wait_until_signaled();
        let _ = self.finish_completed();
    }

    fn poll_device_completion(&self) {
        if self.is_signaled_raw() {
            return;
        }

        // Walk chains iteratively so a long userspace timeline cannot consume
        // the small kernel stack. Clone each edge before polling because a
        // completion may signal its chain and clear that dependency list.
        let mut pending = self.dependencies.lock().clone();
        let mut visited = BTreeSet::new();
        while let Some(dependency) = pending.pop() {
            if !visited.insert(Arc::as_ptr(&dependency) as usize) {
                continue;
            }
            if dependency.is_signaled_raw() {
                continue;
            }
            pending.extend(dependency.dependencies.lock().iter().cloned());
            dependency.poll_own_ticket();
        }
        if self.is_signaled_raw() {
            return;
        }

        self.poll_own_ticket();
    }

    fn poll_own_ticket(&self) {
        if let Some(ticket) = self.ticket.lock().as_ref() {
            ticket.poll_completion();
        }
    }

    fn finish_completed(&self) -> Result<()> {
        let mut ticket = self.ticket.lock();
        match self.state() {
            FenceState::Succeeded => return Ok(()),
            FenceState::Failed => {
                return_errno_with_message!(Errno::EIO, "virtio-gpu fence completed with an error");
            }
            FenceState::Completed => {}
            FenceState::Pending => {
                return_errno_with_message!(Errno::EBUSY, "virtio-gpu fence is pending");
            }
        }

        let result = ticket
            .take()
            .expect("completed virtio-gpu fence has no control ticket")
            .wait();
        let final_state = if result.is_ok() {
            FenceState::Succeeded
        } else {
            FenceState::Failed
        };
        self.state.store(final_state as u8, Ordering::Release);
        result.map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu fence response failed"))
    }

    fn state(&self) -> FenceState {
        match self.state.load(Ordering::Acquire) {
            value if value == FenceState::Pending as u8 => FenceState::Pending,
            value if value == FenceState::Completed as u8 => FenceState::Completed,
            value if value == FenceState::Succeeded as u8 => FenceState::Succeeded,
            value if value == FenceState::Failed as u8 => FenceState::Failed,
            _ => unreachable!("invalid DRM fence state"),
        }
    }

    fn is_signaled_raw(&self) -> bool {
        self.state() != FenceState::Pending
    }

    fn check_io_events(&self) -> IoEvents {
        self.poll_device_completion();
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
        let old_state = self
            .state
            .swap(FenceState::Completed as u8, Ordering::AcqRel);
        debug_assert_eq!(old_state, FenceState::Pending as u8);
        self.run_callbacks();
        self.waiters.wake_all();
        // `ERR` is always reported by poll, even when userspace did not ask
        // for it. Wake those pollers now; `check_io_events` filters successful
        // completions back to `IN` only.
        self.pollee.notify(IoEvents::IN | IoEvents::ERR);
    }
}

struct FenceBarrier {
    remaining: AtomicUsize,
    output: Arc<Fence>,
}

impl FenceBarrier {
    fn complete_one(&self) {
        if self.remaining.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.output.signal_success();
        }
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

    pub(super) fn fence(&self) -> Arc<Fence> {
        self.fence.clone()
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
    use alloc::sync::Arc;
    use core::sync::atomic::{AtomicBool, Ordering};

    use ostd::prelude::ktest;

    use super::Fence;
    use crate::thread::kernel_thread::ThreadOptions;

    #[ktest]
    fn pending_fence_is_nonblocking() {
        let fence = Fence::new();
        assert!(!fence.is_signaled());
        assert!(matches!(fence.try_finish(), Ok(false)));
    }

    #[ktest]
    fn software_fence_reports_success() {
        let fence = Fence::new();
        fence.signal_success();
        assert!(fence.is_signaled());
        assert!(matches!(fence.try_finish(), Ok(true)));
    }

    #[ktest]
    fn software_fence_reports_failure() {
        let fence = Fence::new();
        fence.signal_failure();
        assert!(fence.is_signaled());
        assert!(fence.try_finish().is_err());
        fence.wait_for_dependency();
    }

    #[ktest]
    fn dependency_wait_parks_until_signal() {
        let fence = Arc::new(Fence::new());
        let started = Arc::new(AtomicBool::new(false));
        let finished = Arc::new(AtomicBool::new(false));
        let waiter = {
            let fence = fence.clone();
            let started = started.clone();
            let finished = finished.clone();
            ThreadOptions::new(move || {
                started.store(true, Ordering::Release);
                fence.wait_for_dependency();
                finished.store(true, Ordering::Release);
            })
            .spawn()
        };
        while !started.load(Ordering::Acquire) {
            ostd::task::Task::yield_now();
        }
        assert!(!finished.load(Ordering::Acquire));
        fence.signal_success();
        waiter.join();
        assert!(finished.load(Ordering::Acquire));
    }

    #[ktest]
    fn callback_handles_signal_registration_race_boundaries() {
        let before = Arc::new(AtomicBool::new(false));
        let fence = Fence::new();
        {
            let before = before.clone();
            fence.on_signal(move || before.store(true, Ordering::Release));
        }
        assert!(!before.load(Ordering::Acquire));
        fence.signal_success();
        assert!(before.load(Ordering::Acquire));

        let after = Arc::new(AtomicBool::new(false));
        {
            let after = after.clone();
            fence.on_signal(move || after.store(true, Ordering::Release));
        }
        assert!(after.load(Ordering::Acquire));
    }

    #[ktest]
    fn chained_fence_waits_for_every_dependency() {
        let first = Arc::new(Fence::new());
        let second = Arc::new(Fence::new());
        let chained = Fence::chain(Some(first.clone()), second.clone());
        second.signal_success();
        assert!(!chained.is_signaled());
        first.signal_failure();
        assert!(chained.is_signaled());
    }

    #[ktest]
    fn chained_fence_flattens_long_timeline_dependencies() {
        let leaf = Arc::new(Fence::new());
        let mut chained = Fence::chain(None, leaf.clone());
        for _ in 0..1024 {
            chained = Fence::chain(Some(chained), Fence::new_signaled());
        }
        assert_eq!(chained.dependencies.lock().len(), 1);
        leaf.signal_success();
        assert!(chained.is_signaled());
    }
}
