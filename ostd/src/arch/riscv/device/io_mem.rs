// SPDX-License-Identifier: MPL-2.0

//! Memory-mapped I/O synchronization.

/// Orders device I/O before and after this fence.
///
/// See the RISC-V FENCE specification:
/// <https://docs.riscv.org/reference/isa/unpriv/rv32.html#_memory_ordering_instructions>.
#[inline]
pub fn fence() {
    // SAFETY: A FENCE only constrains the calling hart's observable I/O ordering.
    unsafe { core::arch::asm!("fence iorw, iorw", options(nostack)) };
}
