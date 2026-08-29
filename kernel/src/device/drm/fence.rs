// SPDX-License-Identifier: MPL-2.0

//! Pollable virtio-gpu 3D fences (`sync_file`-like).
//!
//! `VIRTGPU_EXECBUFFER` returns a [`FenceFile`] as the out-fence (`fence_fd`)
//! when the caller requests `VIRTGPU_EXECBUF_FENCE_FD_OUT`.
//! Mesa's virgl winsys polls this fd for `POLLIN`
//! to learn that the submitted render has completed.
//! A fenced `SUBMIT_3D` is queued asynchronously;
//! its control-queue completion signals the fence from interrupt context.

use core::{
    fmt::Display,
    mem,
    sync::atomic::{AtomicBool, AtomicU8, AtomicUsize, Ordering},
    time::Duration,
};

use aster_virtio::device::gpu::{
    GpuCommandCompletion,
    device::{GpuCommandPollHandle, GpuCommandTicket},
};
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
    command_quota: Mutex<Option<ExecbufferMemoryQuota>>,
    dependencies: SpinLock<Vec<Arc<Fence>>, LocalIrqDisabled>,
    callbacks: SpinLock<Option<Arc<FenceCallback>>, LocalIrqDisabled>,
    dependency_callbacks: SpinLock<Vec<FenceCallbackRegistration>, LocalIrqDisabled>,
    poll_handle: SpinLock<Option<GpuCommandPollHandle>, LocalIrqDisabled>,
    pollee: Pollee,
    waiters: WaitQueue,
    has_chain_queue_slot: AtomicBool,
    ticket_attached: AtomicBool,
    completion_received: AtomicBool,
}

struct FenceCallback {
    active: AtomicBool,
    callback_fn: SpinLock<Option<Box<dyn FnOnce() + Send>>, LocalIrqDisabled>,
    next: SpinLock<Option<Arc<FenceCallback>>, LocalIrqDisabled>,
    _quota: FenceCallbackQuota,
}

/// A fully allocated callback that can later be linked to any fence.
pub(super) struct PreparedFenceCallback {
    node: Arc<FenceCallback>,
}

impl PreparedFenceCallback {
    fn run_if_active(self) {
        if self.node.active.swap(false, Ordering::AcqRel) {
            let callback_fn = self.node.callback_fn.lock().take();
            if let Some(callback_fn) = callback_fn {
                callback_fn();
            }
        }
    }
}

pub(super) struct FenceCallbackRegistration {
    fence: Weak<Fence>,
    node: Arc<FenceCallback>,
}

impl FenceCallbackRegistration {
    pub(super) fn is_active(&self) -> bool {
        self.node.active.load(Ordering::Acquire)
    }
}

impl Drop for FenceCallbackRegistration {
    fn drop(&mut self) {
        if !self.node.active.swap(false, Ordering::AcqRel) {
            return;
        }
        let Some(fence) = self.fence.upgrade() else {
            return;
        };
        let mut head = fence.callbacks.lock();
        let Some(first) = head.as_ref() else {
            return;
        };
        if Arc::ptr_eq(first, &self.node) {
            *head = self.node.next.lock().take();
            return;
        }

        let mut current = first.clone();
        loop {
            let Some(next) = current.next.lock().clone() else {
                return;
            };
            if Arc::ptr_eq(&next, &self.node) {
                let replacement = self.node.next.lock().take();
                *current.next.lock() = replacement;
                return;
            }
            current = next;
        }
    }
}

struct ChainCompletionQueue {
    is_draining: bool,
    pending: Vec<ChainWork>,
    reserved_slots: usize,
}

enum ChainWork {
    Signal(Arc<Fence>),
    DropDependencies(Vec<Arc<Fence>>),
}

const MAX_CHAIN_DEPENDENCIES: usize = 2;
const MAX_PENDING_CHAIN_FENCES: usize = 16384;
const MAX_SYSTEM_FENCE_CALLBACKS: usize = 16384;
const MAX_SYSTEM_EXECBUFFER_BYTES: usize = 64 * 1024 * 1024;

static SYSTEM_FENCE_CALLBACK_COUNT: AtomicUsize = AtomicUsize::new(0);
static SYSTEM_EXECBUFFER_BYTES: AtomicUsize = AtomicUsize::new(0);

static CHAIN_COMPLETION_QUEUE: SpinLock<ChainCompletionQueue, LocalIrqDisabled> =
    SpinLock::new(ChainCompletionQueue {
        is_draining: false,
        pending: Vec::new(),
        reserved_slots: 0,
    });

pub(super) struct FenceChainSlot {
    active: bool,
}

impl Drop for FenceChainSlot {
    fn drop(&mut self) {
        if self.active {
            CHAIN_COMPLETION_QUEUE.lock().reserved_slots -= 1;
        }
    }
}

/// All heap storage required to publish one two-edge fence chain.
pub(super) struct PreparedFenceChain {
    fence: Arc<Fence>,
    barrier: Arc<FenceBarrier>,
    callbacks: [PreparedFenceCallback; MAX_CHAIN_DEPENDENCIES],
    queue_slot: FenceChainSlot,
}

struct FenceCallbackQuota;

/// Accounts for a userspace command stream until its ticket-owned DMA copy is
/// released, rather than merely until device submission returns.
pub(super) struct ExecbufferMemoryQuota {
    bytes: usize,
}

impl ExecbufferMemoryQuota {
    pub(super) fn reserve(bytes: usize) -> Result<Self> {
        SYSTEM_EXECBUFFER_BYTES
            .try_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(bytes)
                    .filter(|total| *total <= MAX_SYSTEM_EXECBUFFER_BYTES)
            })
            .map_err(|_| {
                Error::with_message(Errno::ENOSPC, "system execbuffer memory limit reached")
            })?;
        Ok(Self { bytes })
    }
}

impl Drop for ExecbufferMemoryQuota {
    fn drop(&mut self) {
        let old_bytes = SYSTEM_EXECBUFFER_BYTES.fetch_sub(self.bytes, Ordering::AcqRel);
        debug_assert!(old_bytes >= self.bytes);
    }
}

impl FenceCallbackQuota {
    fn reserve() -> Result<Self> {
        SYSTEM_FENCE_CALLBACK_COUNT
            .try_update(Ordering::AcqRel, Ordering::Acquire, |count| {
                (count < MAX_SYSTEM_FENCE_CALLBACKS).then_some(count + 1)
            })
            .map_err(|_| {
                Error::with_message(Errno::ENOSPC, "system DRM fence callback limit reached")
            })?;
        Ok(Self)
    }
}

impl Drop for FenceCallbackQuota {
    fn drop(&mut self) {
        let old_count = SYSTEM_FENCE_CALLBACK_COUNT.fetch_sub(1, Ordering::AcqRel);
        debug_assert!(old_count > 0);
    }
}

impl Fence {
    pub(super) fn new() -> Self {
        Self::with_dependencies(Vec::new())
    }

    fn with_dependencies(dependencies: Vec<Arc<Fence>>) -> Self {
        Self::from_storage(dependencies, Vec::new())
    }

    fn from_storage(
        dependencies: Vec<Arc<Fence>>,
        dependency_callbacks: Vec<FenceCallbackRegistration>,
    ) -> Self {
        Self {
            state: AtomicU8::new(FenceState::Pending as u8),
            ticket: Mutex::new(None),
            command_quota: Mutex::new(None),
            dependencies: SpinLock::new(dependencies),
            callbacks: SpinLock::new(None),
            dependency_callbacks: SpinLock::new(dependency_callbacks),
            poll_handle: SpinLock::new(None),
            pollee: Pollee::new(),
            waiters: WaitQueue::new(),
            has_chain_queue_slot: AtomicBool::new(false),
            ticket_attached: AtomicBool::new(false),
            completion_received: AtomicBool::new(false),
        }
    }

    fn try_new_chain_fence() -> Result<Arc<Self>> {
        let mut dependencies = Vec::new();
        dependencies
            .try_reserve_exact(MAX_CHAIN_DEPENDENCIES)
            .map_err(|_| {
                Error::with_message(Errno::ENOMEM, "cannot allocate fence dependencies")
            })?;
        let mut dependency_callbacks = Vec::new();
        dependency_callbacks
            .try_reserve_exact(MAX_CHAIN_DEPENDENCIES)
            .map_err(|_| {
                Error::with_message(Errno::ENOMEM, "cannot allocate fence registrations")
            })?;
        Arc::try_new(Self::from_storage(dependencies, dependency_callbacks))
            .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate chained fence"))
    }

    pub(super) fn reserve_chain_slot() -> Result<FenceChainSlot> {
        let mut queue = CHAIN_COMPLETION_QUEUE.lock();
        let retained = queue
            .pending
            .len()
            .checked_add(queue.reserved_slots)
            .and_then(|count| count.checked_add(1))
            .ok_or_else(|| Error::with_message(Errno::ENOSPC, "fence chain queue overflows"))?;
        if retained > MAX_PENDING_CHAIN_FENCES {
            return_errno_with_message!(Errno::ENOSPC, "too many pending DRM fence chains");
        }
        let additional = queue.reserved_slots + 1;
        queue.pending.try_reserve(additional).map_err(|_| {
            Error::with_message(Errno::ENOMEM, "cannot reserve fence completion queue")
        })?;
        queue.reserved_slots += 1;
        Ok(FenceChainSlot { active: true })
    }

    /// Returns an already-signaled software fence.
    #[cfg(ktest)]
    pub(super) fn new_signaled() -> Arc<Self> {
        let fence = Arc::new(Self::new());
        fence.signal_success();
        fence
    }

    /// Preallocates a fence chain and both possible dependency callbacks.
    pub(super) fn prepare_chain(queue_slot: FenceChainSlot) -> Result<PreparedFenceChain> {
        let fence = Self::try_new_chain_fence()?;
        let barrier = Arc::try_new(FenceBarrier {
            remaining: AtomicUsize::new(0),
            output: Arc::downgrade(&fence),
        })
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate fence barrier"))?;
        let callbacks = core::array::try_from_fn(|_| {
            let barrier = barrier.clone();
            Self::prepare_callback(move || barrier.complete_one())
        })?;
        Ok(PreparedFenceChain {
            fence,
            barrier,
            callbacks,
            queue_slot,
        })
    }

    /// Builds a fence from preallocated storage after choosing dependencies.
    ///
    /// Error status is deliberately not propagated:
    /// DRM synchronization treats a failed producer as a completed dependency,
    /// just like a `sync_file` wait.
    pub(super) fn finish_chain(
        mut prepared: PreparedFenceChain,
        previous: Option<Arc<Self>>,
        current: Arc<Self>,
    ) -> Arc<Self> {
        let mut dependencies = prepared.fence.dependencies.lock();
        if let Some(previous) = previous
            && !Arc::ptr_eq(&previous, &current)
            && !previous.is_signaled()
        {
            dependencies.push(previous);
        }
        if !current.is_signaled() {
            dependencies.push(current);
        }
        if dependencies.is_empty() {
            drop(dependencies);
            prepared.fence.signal_success();
            return prepared.fence;
        }

        // Each append adds at most two edges. Chained completions are drained
        // iteratively below, so a long timeline neither re-registers callbacks
        // on every old leaf nor recursively consumes the interrupt stack.
        prepared
            .fence
            .has_chain_queue_slot
            .store(true, Ordering::Release);
        prepared.queue_slot.active = false;
        prepared
            .barrier
            .remaining
            .store(dependencies.len(), Ordering::Release);
        let dependency_snapshot = [dependencies.first().cloned(), dependencies.get(1).cloned()];
        *prepared.fence.poll_handle.lock() = dependencies
            .iter()
            .find_map(|dependency| dependency.device_poll_handle());
        drop(dependencies);
        for (dependency, callback) in dependency_snapshot
            .into_iter()
            .flatten()
            .zip(prepared.callbacks)
        {
            if let Some(registration) = dependency.register_callback(callback) {
                let mut registrations = prepared.fence.dependency_callbacks.lock();
                registrations.push(registration);
                // The callback may have signaled the output after registration
                // but before its cancellation handle reached this list.
                if prepared.fence.is_signaled_raw() {
                    registrations.clear();
                }
            }
        }
        prepared.fence
    }

    pub(super) fn attach(&self, ticket: GpuCommandTicket, command_quota: ExecbufferMemoryQuota) {
        let poll_handle = ticket.poll_handle();
        let old_quota = self.command_quota.lock().replace(command_quota);
        debug_assert!(old_quota.is_none());
        *self.poll_handle.lock() = Some(poll_handle);
        let mut ticket_slot = self.ticket.lock();
        let old_ticket = ticket_slot.replace(ticket);
        debug_assert!(old_ticket.is_none());
        drop(ticket_slot);
        // Sequential consistency prevents the attachment and completion sides
        // from both observing the other's flag as false.
        self.ticket_attached.store(true, Ordering::SeqCst);
        if self.completion_received.load(Ordering::SeqCst) {
            self.publish_device_completion();
        }
    }

    pub(super) fn is_signaled(&self) -> bool {
        self.is_signaled_raw()
    }

    /// Actively advances device-backed dependencies before checking the state.
    pub(super) fn poll_and_is_signaled(&self) -> bool {
        self.poll_device_completion();
        self.is_signaled_raw()
    }

    /// Allocates callback storage before entering a commit-critical path.
    pub(super) fn prepare_callback(
        callback_fn: impl FnOnce() + Send + 'static,
    ) -> Result<PreparedFenceCallback> {
        let quota = FenceCallbackQuota::reserve()?;
        let callback_fn: Box<dyn FnOnce() + Send> = Box::try_new(callback_fn)
            .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate fence callback"))?;
        let node = Arc::try_new(FenceCallback {
            active: AtomicBool::new(true),
            callback_fn: SpinLock::new(Some(callback_fn)),
            next: SpinLock::new(None),
            _quota: quota,
        })
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate fence callback node"))?;
        Ok(PreparedFenceCallback { node })
    }

    /// Links a fully allocated callback without allocating.
    ///
    /// Registration is safe against a concurrent virtio completion: the state
    /// is checked again while holding the callback lock, after the completion
    /// side has published its state.
    pub(super) fn register_callback(
        self: &Arc<Self>,
        prepared: PreparedFenceCallback,
    ) -> Option<FenceCallbackRegistration> {
        if self.is_signaled() {
            prepared.run_if_active();
            return None;
        }

        let mut callbacks = self.callbacks.lock();
        // Do not poll while holding the callback lock: completing a ticket
        // invokes `run_callbacks` and would deadlock on this same lock.
        if self.is_signaled_raw() {
            drop(callbacks);
            prepared.run_if_active();
            None
        } else {
            *prepared.node.next.lock() = callbacks.take();
            *callbacks = Some(prepared.node.clone());
            Some(FenceCallbackRegistration {
                fence: Arc::downgrade(self),
                node: prepared.node,
            })
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
        let dependencies = mem::take(&mut *self.dependencies.lock());
        self.poll_handle.lock().take();
        drop(dependencies);
        self.dependency_callbacks.lock().clear();
        let mut callback = self.callbacks.lock().take();
        while let Some(current) = callback {
            callback = current.next.lock().take();
            if current.active.swap(false, Ordering::AcqRel) {
                let callback_fn = current.callback_fn.lock().take();
                if let Some(callback_fn) = callback_fn {
                    callback_fn();
                }
            }
        }
    }

    fn publish_device_completion(&self) {
        if self
            .state
            .compare_exchange(
                FenceState::Pending as u8,
                FenceState::Completed as u8,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_err()
        {
            return;
        }
        self.run_callbacks();
        self.waiters.wake_all();
        // `ERR` is always reported by poll, even when userspace did not ask
        // for it. `check_io_events` filters successful completions to `IN`.
        self.pollee.notify(IoEvents::IN | IoEvents::ERR);
    }

    fn enqueue_chain_completion(fence: Arc<Self>) {
        debug_assert!(fence.has_chain_queue_slot.swap(false, Ordering::AcqRel));
        Self::enqueue_chain_work(ChainWork::Signal(fence));
    }

    fn enqueue_chain_work(work: ChainWork) {
        let should_drain = {
            let mut queue = CHAIN_COMPLETION_QUEUE.lock();
            debug_assert!(queue.reserved_slots > 0);
            queue.reserved_slots -= 1;
            debug_assert!(queue.pending.len() < queue.pending.capacity());
            queue.pending.push(work);
            if queue.is_draining {
                false
            } else {
                queue.is_draining = true;
                true
            }
        };
        if !should_drain {
            return;
        }

        loop {
            let next = {
                let mut queue = CHAIN_COMPLETION_QUEUE.lock();
                let Some(next) = queue.pending.pop() else {
                    queue.is_draining = false;
                    return;
                };
                next
            };
            match next {
                ChainWork::Signal(fence) => fence.signal_success(),
                ChainWork::DropDependencies(dependencies) => {
                    for dependency in dependencies {
                        match Arc::try_unwrap(dependency) {
                            Ok(fence) => drop(fence),
                            Err(shared) => drop(shared),
                        }
                    }
                }
            }
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

    /// Waits for completion until a signal or a bounded device deadline.
    pub(super) fn wait_interruptible_or_timeout(&self, timeout: &Duration) -> Result<()> {
        if timeout.is_zero() {
            if self.try_finish()? {
                return Ok(());
            }
            return_errno_with_message!(Errno::ETIME, "the fence wait timed out");
        }
        self.waiters.pause_until_or_timeout(
            || {
                self.poll_device_completion();
                self.is_signaled().then_some(())
            },
            timeout,
        )?;
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
        let poll_handle = self.poll_handle.lock().clone();
        if let Some(poll_handle) = poll_handle {
            // The queue handle remains valid after any one ticket is consumed,
            // so a cached handle cannot become a stale chain leaf.
            poll_handle.poll_completion();
        } else {
            self.poll_own_ticket();
        }
    }

    fn device_poll_handle(&self) -> Option<GpuCommandPollHandle> {
        self.poll_handle.lock().clone()
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
        self.command_quota.lock().take();
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

impl Drop for Fence {
    fn drop(&mut self) {
        let dependencies = mem::take(&mut *self.dependencies.lock());
        self.poll_handle.lock().take();
        if self.has_chain_queue_slot.swap(false, Ordering::AcqRel) {
            Self::enqueue_chain_work(ChainWork::DropDependencies(dependencies));
        } else {
            debug_assert!(dependencies.is_empty());
        }
    }
}

impl GpuCommandCompletion for Fence {
    fn complete(&self) {
        let old_completion = self.completion_received.swap(true, Ordering::SeqCst);
        debug_assert!(!old_completion);
        // Do not lock `ticket`: completion can run synchronously while
        // `poll_own_ticket` already holds that mutex.
        if self.ticket_attached.load(Ordering::SeqCst) {
            self.publish_device_completion();
        }
    }
}

struct FenceBarrier {
    remaining: AtomicUsize,
    output: Weak<Fence>,
}

impl FenceBarrier {
    fn complete_one(&self) {
        if self.remaining.fetch_sub(1, Ordering::AcqRel) == 1
            && let Some(output) = self.output.upgrade()
        {
            Fence::enqueue_chain_completion(output);
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
    use core::{
        sync::atomic::{AtomicBool, Ordering},
        time::Duration,
    };

    use aster_virtio::device::gpu::GpuCommandCompletion;
    use ostd::prelude::ktest;

    use super::{Fence, FenceState};
    use crate::thread::kernel_thread::ThreadOptions;

    #[ktest]
    fn pending_fence_is_nonblocking() {
        let fence = Fence::new();
        assert!(!fence.is_signaled());
        assert!(matches!(fence.try_finish(), Ok(false)));
    }

    #[ktest]
    fn pending_fence_wait_honors_timeout() {
        let fence = Fence::new();
        let error = fence
            .wait_interruptible_or_timeout(&Duration::ZERO)
            .unwrap_err();
        assert_eq!(error.error(), crate::error::Errno::ETIME);
    }

    #[ktest]
    fn completion_is_hidden_until_ticket_attachment() {
        let fence = Fence::new();
        fence.complete();
        assert_eq!(fence.state(), FenceState::Pending);
        assert!(fence.completion_received.load(Ordering::Acquire));
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
        let fence = Arc::new(Fence::new());
        let registration = {
            let before = before.clone();
            fence.register_callback(
                Fence::prepare_callback(move || before.store(true, Ordering::Release)).unwrap(),
            )
        };
        assert!(registration.is_some());
        assert!(!before.load(Ordering::Acquire));
        fence.signal_success();
        assert!(before.load(Ordering::Acquire));

        let after = Arc::new(AtomicBool::new(false));
        {
            let after = after.clone();
            assert!(
                fence
                    .register_callback(
                        Fence::prepare_callback(move || after.store(true, Ordering::Release))
                            .unwrap(),
                    )
                    .is_none()
            );
        }
        assert!(after.load(Ordering::Acquire));
    }

    #[ktest]
    fn dropping_callback_registration_cancels_pending_callback() {
        let called = Arc::new(AtomicBool::new(false));
        let fence = Arc::new(Fence::new());
        let registration = {
            let called = called.clone();
            fence
                .register_callback(
                    Fence::prepare_callback(move || called.store(true, Ordering::Release)).unwrap(),
                )
                .unwrap()
        };
        drop(registration);
        fence.signal_success();
        assert!(!called.load(Ordering::Acquire));
    }

    #[ktest]
    fn chained_fence_waits_for_every_dependency() {
        let first = Arc::new(Fence::new());
        let second = Arc::new(Fence::new());
        let prepared = Fence::prepare_chain(Fence::reserve_chain_slot().unwrap()).unwrap();
        let chained = Fence::finish_chain(prepared, Some(first.clone()), second.clone());
        second.signal_success();
        assert!(!chained.is_signaled());
        first.signal_failure();
        assert!(chained.is_signaled());
    }

    #[ktest]
    fn chained_fence_completes_long_timeline_iteratively() {
        let leaf = Arc::new(Fence::new());
        let prepared = Fence::prepare_chain(Fence::reserve_chain_slot().unwrap()).unwrap();
        let mut chained = Fence::finish_chain(prepared, None, leaf.clone());
        for _ in 0..4096 {
            let prepared = Fence::prepare_chain(Fence::reserve_chain_slot().unwrap()).unwrap();
            chained = Fence::finish_chain(prepared, Some(chained), Fence::new_signaled());
        }
        leaf.signal_success();
        assert!(chained.is_signaled());
    }

    #[ktest]
    fn dropping_long_pending_chain_is_iterative() {
        let leaf = Arc::new(Fence::new());
        let prepared = Fence::prepare_chain(Fence::reserve_chain_slot().unwrap()).unwrap();
        let mut chained = Fence::finish_chain(prepared, None, leaf);
        for _ in 0..4096 {
            let prepared = Fence::prepare_chain(Fence::reserve_chain_slot().unwrap()).unwrap();
            chained = Fence::finish_chain(prepared, Some(chained), Fence::new_signaled());
        }
        drop(chained);
    }
}
