// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::prelude::*;

/// `SYS_RISCV_FLUSH_ICACHE_LOCAL`: flush only the current hart.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/arch/riscv/include/asm/unistd.h>.
const SYS_RISCV_FLUSH_ICACHE_LOCAL: u64 = 1;

/// `riscv_flush_icache(start, end, flags)` — syscall 258+1 = 259, RISC-V only.
///
/// Synchronizes the instruction stream with data memory after user space
/// (typically a JIT such as SpiderMonkey or V8) has written code pages.
///
/// Like Linux, the address range is not validated: on platforms without
/// non-coherent instruction caches (QEMU virt included), `fence.i` covers
/// the whole icache regardless of the range. `flags` must be zero or
/// `SYS_RISCV_FLUSH_ICACHE_LOCAL`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/arch/riscv/mm/cacheflush.c>.
pub fn sys_riscv_flush_icache(
    _start: Vaddr,
    _end: Vaddr,
    flags: u64,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    if flags & !SYS_RISCV_FLUSH_ICACHE_LOCAL != 0 {
        return_errno_with_message!(Errno::EINVAL, "invalid riscv_flush_icache flags");
    }

    ostd::arch::flush_icache(flags & SYS_RISCV_FLUSH_ICACHE_LOCAL != 0);

    Ok(SyscallReturn::Return(0))
}
