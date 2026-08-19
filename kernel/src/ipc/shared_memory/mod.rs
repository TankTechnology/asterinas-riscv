// SPDX-License-Identifier: MPL-2.0

//! System V shared memory.

use bitflags::bitflags;

pub mod shm;
pub mod shm_set;

bitflags! {
    pub struct PermissionMode: u16 {
        const ALTER = 0o002;
        const WRITE = 0o002;
        const READ  = 0o004;
    }
}
