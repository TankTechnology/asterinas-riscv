// SPDX-License-Identifier: MPL-2.0

//! System V shared memory.

use bitflags::bitflags;

pub mod shm;
pub mod shm_set;

bitflags! {
    pub struct PermissionMode: u16 {
        const EXECUTE = 0o001;
        const ALTER = 0o002;
        const WRITE = 0o002;
        const READ  = 0o004;
    }
}

impl PermissionMode {
    /// Collapses owner, group, and other request bits into one access mask.
    ///
    /// Linux interprets mode bits passed to `shmget` as the kinds of access
    /// requested by the caller; the segment's ownership selects which class
    /// of its stored mode grants that access.
    ///
    /// Reference: <https://github.com/torvalds/linux/blob/master/ipc/util.c>.
    pub(in crate::ipc) fn from_requested_mode(mode: u16) -> Self {
        Self::from_bits_truncate((mode | (mode >> 3) | (mode >> 6)) & 0o7)
    }
}
