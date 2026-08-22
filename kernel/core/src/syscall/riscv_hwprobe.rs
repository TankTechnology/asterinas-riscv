// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::prelude::*;

/// RISC-V hardware probing (syscall 258).
///
/// glibc probes hardware capabilities at startup.  QEMU virt guests
/// don't need HWPROBE; returning ENOSYS is the correct fallback.
pub fn sys_riscv_hwprobe(
    _pairs: u64,
    _pair_count: u64,
    _cpu_set_size: u64,
    _cpu_set: u64,
    _flags: u64,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    debug!("riscv_hwprobe called — ENOSYS (not implemented)");
    return_errno_with_message!(Errno::ENOSYS, "riscv_hwprobe is not implemented");
}
