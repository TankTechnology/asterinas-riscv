// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::prelude::*;

/// bpf (syscall 280 in the asm-generic numbering).
///
/// A real implementation needs the full eBPF subsystem (maps, the verifier,
/// program attachment, and a JIT or interpreter), which Asterinas does not
/// have. Callers observed in the guest (systemd probing during early boot)
/// treat ENOSYS as "BPF unavailable" and degrade gracefully, which matches a
/// kernel built without CONFIG_BPF_SYSCALL.
pub fn sys_bpf(_cmd: u64, _attr: u64, _size: u64, _ctx: &Context) -> Result<SyscallReturn> {
    debug!("bpf called — ENOSYS (eBPF is not implemented)");
    return_errno_with_message!(Errno::ENOSYS, "bpf is not implemented");
}
