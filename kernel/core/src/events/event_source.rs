// SPDX-License-Identifier: MPL-2.0

//! A pollable event source for kernel subsystems.
//!
//! Linux lets a driver report events through `poll`/`read` on its file
//! description with `poll_wait` and a wait queue. This type is the same
//! primitive for component crates: it wraps a [`Pollee`] for the `poll`
//! registration, and keeps a raw [`Waker`] list for callers that block in a
//! file operation (`read`) instead of polling.

use alloc::{sync::Arc, vec::Vec};

use ostd::sync::{Mutex, Waker};

use crate::{
    events::IoEvents, prelude::*, process::signal::{PollHandle, Pollee},
};

/// A pollable source of events owned by a kernel object.
pub struct EventSource {
    pollee: Pollee,
    wakers: Mutex<Vec<Arc<Waker>>>,
}

impl core::fmt::Debug for EventSource {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("EventSource").finish_non_exhaustive()
    }
}

impl Default for EventSource {
    fn default() -> Self {
        Self::new()
    }
}

impl EventSource {
    /// Creates an event source with no pending events.
    pub fn new() -> Self {
        Self {
            pollee: Pollee::new(),
            wakers: Mutex::new(Vec::new()),
        }
    }

    /// Reports the current events, registering `poller` for wake-ups.
    ///
    /// `check` is evaluated after the registration, so an event that races
    /// with `notify` is either observed by `check` or wakes the poller.
    pub fn poll<F>(
        &self,
        mask: IoEvents,
        poller: Option<&mut PollHandle>,
        check: F,
    ) -> IoEvents
    where
        F: FnOnce() -> IoEvents,
    {
        self.pollee.poll_with(mask, poller, check)
    }

    /// Notifies pollers and blocking waiters that events are available.
    pub fn notify(&self, events: IoEvents) {
        self.pollee.notify(events);

        let wakers = core::mem::take(&mut *self.wakers.lock());
        for waker in wakers {
            waker.wake_up();
        }
    }

    /// Invalidates the cached event mask.
    ///
    /// Call this when previously reported events are no longer pending.
    pub fn invalidate(&self) {
        self.pollee.invalidate();
    }

    /// Registers a raw waker for the next [`Self::notify`].
    ///
    /// Used by blocking file operations. The waker is consumed by the next
    /// notification; callers re-register after every wake-up.
    pub fn register_waker(&self, waker: Arc<Waker>) {
        self.wakers.lock().push(waker);
    }
}
