// SPDX-License-Identifier: MPL-2.0

use alloc::boxed::ThinBox;
use core::sync::atomic::{AtomicI32, Ordering};

use crate::{
    fs::{
        file::flock::FlockList,
        vfs::{inode::Inode, notify::FsEventPublisher, range_lock::RangeLockList},
    },
    prelude::*,
};

/// Context for FS locks.
pub struct FsLockContext {
    range_lock_list: RangeLockList,
    flock_list: FlockList,
}

impl FsLockContext {
    pub(self) fn new() -> Self {
        Self {
            range_lock_list: RangeLockList::new(),
            flock_list: FlockList::new(),
        }
    }

    /// Returns a reference to the range lock list.
    pub fn range_lock_list(&self) -> &RangeLockList {
        &self.range_lock_list
    }

    /// Returns a reference to the FS lock context.
    pub fn flock_list(&self) -> &FlockList {
        &self.flock_list
    }
}

/// Tracks write access to an inode, mirroring Linux's `i_writecount`.
///
/// A non-negative counter value is the number of opens for writing. A
/// negative value means write access is denied (e.g. the file is being
/// executed via `execve`), in which case the absolute value is the deny
/// depth. Opening a file for writing while access is denied, or denying
/// write access while the file is open for writing, fails with `ETXTBSY`.
#[derive(Debug)]
pub struct WriteAccessTracker {
    count: AtomicI32,
}

impl WriteAccessTracker {
    fn new() -> Self {
        Self {
            count: AtomicI32::new(0),
        }
    }

    /// Registers an open for writing (`get_write_access()` in Linux).
    pub fn acquire_write(&self) -> Result<()> {
        let mut cur = self.count.load(Ordering::Acquire);
        loop {
            if cur < 0 {
                return_errno_with_message!(
                    Errno::ETXTBSY,
                    "the file is being executed, write access is denied"
                );
            }
            match self
                .count
                .compare_exchange_weak(cur, cur + 1, Ordering::AcqRel, Ordering::Acquire)
            {
                Ok(_) => return Ok(()),
                Err(new_cur) => cur = new_cur,
            }
        }
    }

    /// Unregisters an open for writing (`put_write_access()` in Linux).
    pub fn release_write(&self) {
        let prev = self.count.fetch_sub(1, Ordering::AcqRel);
        debug_assert!(prev > 0, "unbalanced write-access release");
    }

    /// Denies write access while the file is being executed
    /// (`deny_write_access()` in Linux).
    pub fn deny_write(&self) -> Result<()> {
        let mut cur = self.count.load(Ordering::Acquire);
        loop {
            if cur > 0 {
                return_errno_with_message!(
                    Errno::ETXTBSY,
                    "the file is open for writing, execute access is denied"
                );
            }
            match self
                .count
                .compare_exchange_weak(cur, cur - 1, Ordering::AcqRel, Ordering::Acquire)
            {
                Ok(_) => return Ok(()),
                Err(new_cur) => cur = new_cur,
            }
        }
    }

    /// Re-allows write access after the file is done being executed
    /// (`allow_write_access()` in Linux).
    pub fn allow_write(&self) {
        let prev = self.count.fetch_add(1, Ordering::AcqRel);
        debug_assert!(prev < 0, "unbalanced write-access allow");
    }
}

/// A guard that keeps write access to an inode denied while alive.
#[derive(Debug)]
pub struct WriteAccessDenyGuard {
    inode: Arc<dyn Inode>,
}

impl WriteAccessDenyGuard {
    /// Denies write access to the inode, returning a guard that re-allows
    /// write access when dropped.
    pub fn new(inode: Arc<dyn Inode>) -> Result<Self> {
        inode.write_access_tracker_or_init().deny_write()?;
        Ok(Self { inode })
    }
}

impl Drop for WriteAccessDenyGuard {
    fn drop(&mut self) {
        self.inode.write_access_tracker_or_init().allow_write();
    }
}

/// A trait that instantiates kernel types for the inode [`Extension`].
///
/// [`Extension`]: super::inode::Extension
pub trait InodeExt {
    /// Gets or initializes the FS event publisher.
    ///
    /// If the publisher does not exist for this inode, it will be created.
    fn fs_event_publisher_or_init(&self) -> &FsEventPublisher;

    /// Returns a reference to the FS event publisher.
    ///
    /// If the publisher does not exist for this inode, a [`None`] will be returned.
    fn fs_event_publisher(&self) -> Option<&FsEventPublisher>;

    /// Gets or initializes the FS lock context.
    ///
    /// If the context does not exist for this inode, it will be created.
    fn fs_lock_context_or_init(&self) -> &FsLockContext;

    /// Returns a reference to the FS lock context.
    ///
    /// If the context does not exist for this inode, a [`None`] will be returned.
    fn fs_lock_context(&self) -> Option<&FsLockContext>;

    /// Gets or initializes the write-access tracker.
    ///
    /// If the tracker does not exist for this inode, it will be created.
    fn write_access_tracker_or_init(&self) -> &WriteAccessTracker;

    /// Returns a reference to the write-access tracker.
    ///
    /// If the tracker does not exist for this inode, a [`None`] will be returned.
    fn write_access_tracker(&self) -> Option<&WriteAccessTracker>;
}

impl InodeExt for dyn Inode {
    fn fs_event_publisher_or_init(&self) -> &FsEventPublisher {
        self.extension()
            .group1()
            .call_once(|| ThinBox::new_unsize(FsEventPublisher::new()))
            .downcast_ref()
            .unwrap()
    }

    fn fs_event_publisher(&self) -> Option<&FsEventPublisher> {
        Some(self.extension().group1().get()?.downcast_ref().unwrap())
    }

    fn fs_lock_context_or_init(&self) -> &FsLockContext {
        self.extension()
            .group2()
            .call_once(|| ThinBox::new_unsize(FsLockContext::new()))
            .downcast_ref()
            .unwrap()
    }

    fn fs_lock_context(&self) -> Option<&FsLockContext> {
        Some(self.extension().group2().get()?.downcast_ref().unwrap())
    }

    fn write_access_tracker_or_init(&self) -> &WriteAccessTracker {
        self.extension()
            .group3()
            .call_once(|| ThinBox::new_unsize(WriteAccessTracker::new()))
            .downcast_ref()
            .unwrap()
    }

    fn write_access_tracker(&self) -> Option<&WriteAccessTracker> {
        Some(self.extension().group3().get()?.downcast_ref().unwrap())
    }
}
