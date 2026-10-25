// SPDX-License-Identifier: MPL-2.0
use alloc::fmt;
use core::ptr;

use ostd::sync::{WaitQueue, Waiter, Waker};

use super::{FileLike, InodeHandle};
use crate::prelude::*;

/// Represents a file lock (FLOCK) with an owner and type.
#[derive(Clone, Debug)]
struct Flock {
    /// Owner of the lock, which is an opened file descriptor.
    owner: Weak<dyn FileLike>,
    /// Type of the lock, either shared or exclusive.
    type_: FlockType,
}

/// Represents a Flock item that can be held in a list of file locks.
///
/// Waiters are queued on the inode-wide [`FlockList::waiters`] queue rather
/// than on an individual item.  Otherwise a waiter could be enqueued on a
/// lock item that is dropped before it is woken, and every unlock would have
/// to wake every waiter.
pub(crate) struct FlockItem {
    lock: Flock,
}

impl FlockItem {
    /// Creates a new FlockItem with the specified owner and lock type.
    pub(crate) fn new(owner: &Arc<dyn FileLike>, type_: FlockType) -> Self {
        Self {
            lock: Flock {
                owner: Arc::downgrade(owner),
                type_,
            },
        }
    }

    /// Returns the owner of the lock if it exists.
    pub(crate) fn owner(&self) -> Option<Arc<dyn FileLike>> {
        Weak::upgrade(&self.lock.owner)
    }

    /// Checks if this lock has the same owner as another lock.
    pub(crate) fn same_owner_with(&self, other: &Self) -> bool {
        self.lock.owner.ptr_eq(&other.lock.owner)
    }

    /// Returns true if this lock conflicts with another lock.
    /// Two locks conflict if they have different owners and at least one of them is an exclusive lock.
    pub(crate) fn conflict_with(&self, other: &Self) -> bool {
        if self.same_owner_with(other) {
            return false;
        }
        if self.lock.type_ == FlockType::ExclusiveLock
            || other.lock.type_ == FlockType::ExclusiveLock
        {
            return true;
        }
        false
    }
}

impl Clone for FlockItem {
    fn clone(&self) -> Self {
        Self {
            lock: self.lock.clone(),
        }
    }
}

impl Debug for FlockItem {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Flock")
            .field("owner", &self.lock.owner.as_ptr())
            .field("type_", &self.lock.type_)
            .finish()
    }
}

/// Represents a list of non-POSIX file advisory locks (FLOCK).
/// The list is used to manage file locks and resolve conflicts between them.
pub(crate) struct FlockList {
    inner: Mutex<Vec<FlockItem>>,
    /// Waiters for this inode's flock.  A single queue, rather than one queue
    /// per held lock, keeps waiters alive independent of which item they
    /// conflicted with.
    waiters: WaitQueue,
}

impl FlockList {
    /// Creates a new FlockList.
    pub(crate) fn new() -> Self {
        Self {
            inner: Mutex::new(Vec::new()),
            waiters: WaitQueue::new(),
        }
    }

    /// Attempts to set a lock on the file.
    ///
    /// If no conflicting locks exist, the lock is set and the function returns `Ok(())`.
    /// If a conflicting lock exists:
    /// - If waker is not `None`, it is added to the inode's wait queue, and the function returns `EAGAIN`.
    /// - If waker is `None`, the function returns `EAGAIN`.
    fn try_set_lock(&self, req_lock: &FlockItem, waker: Option<&Arc<Waker>>) -> Result<()> {
        let mut list = self.inner.lock();
        if let Some(conflict_lock) = list.iter().find(|l| req_lock.conflict_with(l)) {
            if let Some(waker) = waker {
                self.waiters.enqueue(waker.clone());
            }
            return_errno_with_message!(Errno::EAGAIN, "the file is locked");
        } else {
            match list.iter().position(|l| req_lock.same_owner_with(l)) {
                Some(idx) => {
                    list[idx] = req_lock.clone();
                }
                None => {
                    list.push(req_lock.clone());
                }
            }
            Ok(())
        }
    }

    /// Sets a lock on the file.
    ///
    /// If no conflicting locks exist, the lock is set and the function returns `Ok(())`.
    /// If is_nonblocking is true and a conflicting lock exists, the function returns `EAGAIN`.
    /// Otherwise, the function waits until the lock can be acquired or until it is interrupted by a signal.
    pub(crate) fn set_lock(&self, req_lock: FlockItem, is_nonblocking: bool) -> Result<()> {
        debug!(
            "set_lock with Flock: {:?}, is_nonblocking: {}",
            req_lock, is_nonblocking
        );
        if is_nonblocking {
            self.try_set_lock(&req_lock, None)
        } else {
            let (waiter, waker) = Waiter::new_pair();
            waiter.pause_until(|| {
                let result = self.try_set_lock(&req_lock, Some(&waker));
                if result.is_err_and(|err| err.error() == Errno::EAGAIN) {
                    None
                } else {
                    Some(result)
                }
            })?
        }
    }

    /// Unlocks the specified owner and wakes one waiting thread.
    ///
    /// If the owner is no longer valid, the lock is removed from the list.
    /// The function does nothing if the owner is not found in the list.
    /// The function is called when the file is closed or the lock is released.
    ///
    /// Only one waiter is woken.  Waking every waiter on every unlock made
    /// the scheduler consider many context switches at the same time, which
    /// repeatedly hit the non-atomic switch-decision race and turned a
    /// contended flock into a multi-second (or multi-minute) stall.  One
    /// wake-up is enough: the woken thread either acquires the lock or
    /// re-enqueues itself, and the next unlock wakes the next waiter.
    pub(crate) fn unlock(&self, req_owner: &InodeHandle) {
        debug!("unlock with owner: {:?}", req_owner as *const InodeHandle);
        let removed = {
            let mut list = self.inner.lock();
            let old_len = list.len();
            list.retain(|lock| {
                if let Some(owner) = lock.owner() {
                    !ptr::eq(
                        Arc::as_ptr(&owner) as *const InodeHandle,
                        req_owner as *const InodeHandle,
                    )
                } else {
                    false // Remove lock if the owner is no longer valid.
                }
            });
            old_len != list.len()
        };

        if removed {
            self.waiters.wake_one();
        }
    }
}

impl Default for FlockList {
    fn default() -> Self {
        Self::new()
    }
}

/// Represents the type of a Flock - either shared or exclusive.
#[repr(u16)]
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum FlockType {
    /// Represents a shared lock.
    SharedLock = 0,
    /// Represents an exclusive lock.
    ExclusiveLock = 1,
}
