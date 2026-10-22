// SPDX-License-Identifier: MPL-2.0

//! A minimal dma-fence equivalent.
//!
//! The device has no asynchronous engine yet, so a fence here is a shared
//! signal state that kernel code and other processes can wait on. It backs
//! sync objects and sync-file descriptors; page-flip completion is recorded by
//! signalling one. The state is IRQ-safe: the software vblank tick may signal
//! fences from the timer softirq.

use alloc::{sync::Arc, vec::Vec};
use core::time::Duration;

use aster_core::{prelude::*, wait_until_or_timeout};
use ostd::sync::{SpinLock, Waker};

/// The signal state of one fence.
#[derive(Debug, Default)]
pub(crate) struct DrmFenceState {
    signaled: bool,
    timeline_value: u64,
}

/// A fence that can be waited on from any thread.
pub(crate) struct DrmFence {
    state: SpinLock<DrmFenceState>,
    wakers: SpinLock<Vec<Arc<Waker>>>,
    /// Callbacks invoked on every state change (pollers of sync files).
    listeners: SpinLock<Vec<Arc<dyn Fn() + Send + Sync>>>,
}

impl core::fmt::Debug for DrmFence {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("DrmFence")
            .field("signaled", &self.is_signaled())
            .field("timeline_value", &self.timeline_value())
            .finish_non_exhaustive()
    }
}

impl DrmFence {
    pub(crate) fn new() -> Arc<Self> {
        Arc::new(Self {
            state: SpinLock::new(DrmFenceState::default()),
            wakers: SpinLock::new(Vec::new()),
            listeners: SpinLock::new(Vec::new()),
        })
    }

    pub(crate) fn new_signaled() -> Arc<Self> {
        Arc::new(Self {
            state: SpinLock::new(DrmFenceState {
                signaled: true,
                timeline_value: 1,
            }),
            wakers: SpinLock::new(Vec::new()),
            listeners: SpinLock::new(Vec::new()),
        })
    }

    pub(crate) fn is_signaled(&self) -> bool {
        self.state.disable_irq().lock().signaled
    }

    pub(crate) fn timeline_value(&self) -> u64 {
        self.state.disable_irq().lock().timeline_value
    }

    /// Returns whether a wait at `point` is satisfied; point zero is always
    /// satisfied.
    pub(crate) fn is_signaled_at(&self, point: u64) -> bool {
        point == 0 || self.state.disable_irq().lock().timeline_value >= point
    }

    pub(crate) fn signal(&self) {
        {
            let mut state = self.state.disable_irq().lock();
            state.signaled = true;
            state.timeline_value = state.timeline_value.max(1);
        }
        self.wake_waiters();
    }

    pub(crate) fn signal_timeline(&self, point: u64) {
        {
            let mut state = self.state.disable_irq().lock();
            if point > state.timeline_value {
                state.timeline_value = point;
            }
            if point > 0 {
                state.signaled = true;
            }
        }
        self.wake_waiters();
    }

    pub(crate) fn reset(&self) {
        *self.state.disable_irq().lock() = DrmFenceState::default();
    }

    pub(crate) fn register_waker(&self, waker: Arc<Waker>) {
        self.wakers.disable_irq().lock().push(waker);
    }

    /// Registers a callback invoked whenever the fence changes state.
    pub(crate) fn add_listener(&self, listener: Arc<dyn Fn() + Send + Sync>) {
        self.listeners.disable_irq().lock().push(listener);
    }

    /// Blocks until the binary signal is set, or the timeout expires.
    pub(crate) fn wait_signaled(&self, timeout: Option<Duration>) -> Result<()> {
        if self.is_signaled() {
            return Ok(());
        }
        wait_until_or_timeout(
            || self.is_signaled(),
            |waker| self.register_waker(waker),
            timeout,
        )
    }

    /// Blocks until the fence is signaled at `point`, or the timeout expires.
    pub(crate) fn wait(&self, point: u64, timeout: Option<Duration>) -> Result<()> {
        if self.is_signaled_at(point) {
            return Ok(());
        }
        wait_until_or_timeout(
            || self.is_signaled_at(point),
            |waker| self.register_waker(waker),
            timeout,
        )
    }

    fn wake_waiters(&self) {
        let wakers = core::mem::take(&mut *self.wakers.disable_irq().lock());
        for waker in wakers {
            waker.wake_up();
        }

        for listener in self.listeners.disable_irq().lock().iter() {
            listener();
        }
    }
}
