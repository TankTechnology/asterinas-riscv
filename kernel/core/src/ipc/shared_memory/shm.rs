// SPDX-License-Identifier: MPL-2.0

//! System V shared memory attachment helpers.
//!
//! The shared-memory segment allocation lives in the IPC namespace; attaching a
//! segment to the caller's address space still relies on the older VMAR layout,
//! so the attach path reports `ENOSYS` until it is re-based.

use super::PermissionMode;
use crate::{
    ipc::{IpcId, IpcNamespace},
    prelude::*,
};

bitflags! {
    #[derive(Clone, Copy, Debug)]
    pub struct ShmFlags: u32 {
        /// Attach read-only.
        const SHM_RDONLY = 0x1000;
        /// Round the attach address down to `SHMLBA` (page) boundary.
        const SHM_RND = 0x2000;
        /// Replace any existing mapping in the attach range.
        const SHM_REMAP = 0x4000;
        /// Grant execution access to the segment.
        const SHM_EXEC = 0x100000;
    }
}

/// Attaches the segment `shmid` to the calling process.
pub fn shm_attach(
    _shmid: IpcId,
    _shmaddr: Vaddr,
    _shmflg: ShmFlags,
    _ipc_ns: &Arc<IpcNamespace>,
    _ctx: &Context,
) -> Result<Vaddr> {
    return_errno_with_message!(Errno::ENOSYS, "SysV shared memory attach is not implemented yet");
}

/// Detaches the segment attached at `shmaddr`.
pub fn shm_detach(_shmaddr: Vaddr, _ipc_ns: &Arc<IpcNamespace>, _ctx: &Context) -> Result<()> {
    return_errno_with_message!(Errno::ENOSYS, "SysV shared memory detach is not implemented yet");
}

/// Returns the permission mode of a segment (unused while attach is disabled).
pub fn shm_mode(_shmid: IpcId, _ipc_ns: &Arc<IpcNamespace>) -> Result<PermissionMode> {
    return_errno_with_message!(Errno::ENOSYS, "SysV shared memory is not implemented yet");
}
