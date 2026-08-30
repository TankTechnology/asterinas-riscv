// SPDX-License-Identifier: MPL-2.0 OR MIT
//
// The original source code is from [trapframe-rs](https://github.com/rcore-os/trapframe-rs),
// which is released under the following license:
//
// SPDX-License-Identifier: MIT
//
// Copyright (c) 2020 - 2024 Runji Wang
//
// We make the following new changes:
// * Implement the `trap_handler` of Asterinas.
// * Remove riscv32 code.
// * Move XLENB, LOAD_SP, and STORE_SP into trap.S.
//
// These changes are released under the following license:
//
// SPDX-License-Identifier: MPL-2.0

use core::arch::{asm, global_asm};

use crate::arch::cpu::{
    context::GeneralRegs,
    extension::{IsaExtensions, has_extensions},
};

/// FPU status bits.
/// Reference: <https://riscv.github.io/riscv-isa-manual/snapshot/privileged/#sstatus>.
pub(in crate::arch) const SSTATUS_FS_MASK: usize = 0b11 << 13;
/// Supervisor User Memory access bit.
/// Reference: <https://riscv.github.io/riscv-isa-manual/snapshot/privileged/#sstatus>.
pub(in crate::arch) const SSTATUS_SUM: usize = 0b1 << 18;

global_asm!(
    include_str!("trap.S"),
    SSTATUS_FS_MASK = const SSTATUS_FS_MASK,
    SSTATUS_SUM = const SSTATUS_SUM
);

/// Initialize interrupt handling for the current HART.
///
/// This function will:
/// - Set `sscratch` to 0.
/// - Set `stvec` to internal exception vector.
///
/// # Safety
///
/// On the current CPU, this function must be called
/// - only once and
/// - before any trap can occur.
pub(super) unsafe fn init_on_cpu() {
    // SAFETY: We believe that these assembly instructions correctly set up
    // the trap handling for the current CPU without side effects.
    unsafe {
        // Set sscratch register to 0, indicating to exception vector that we
        // are presently executing in the kernel.
        asm!("csrw sscratch, zero");
        // Set the exception vector address.
        asm!("csrw stvec, {}", in(reg) trap_entry as *const () as usize);
    }
}

/// Trap frame of kernel interrupt
///
/// # Trap handler
///
/// You need to define a handler function like this:
///
/// ```no_run
/// // SAFETY: The name does not collide with other symbols.
/// #[unsafe(no_mangle)]
/// pub extern "C" fn trap_handler(tf: &mut TrapFrame) {
///     println!("TRAP! tf: {:#x?}", tf);
/// }
/// ```
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct TrapFrame {
    /// General registers
    pub general: GeneralRegs,
    /// Supervisor Status
    pub sstatus: usize,
    /// Supervisor Exception Program Counter
    pub sepc: usize,
}

/// Saved registers on a trap.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub(in crate::arch) struct RawUserContext {
    /// General registers
    pub(in crate::arch) general: GeneralRegs,
    /// Supervisor Status
    pub(in crate::arch) sstatus: usize,
    /// Supervisor Exception Program Counter
    pub(in crate::arch) sepc: usize,
}

impl Default for RawUserContext {
    fn default() -> Self {
        let sstatus = if has_extensions(IsaExtensions::F)
            || has_extensions(IsaExtensions::D)
            || has_extensions(IsaExtensions::Q)
        {
            const SSTATUS_FS_INITIAL: usize = 0b01 << 13;
            SSTATUS_FS_INITIAL
        } else {
            0
        };

        Self {
            general: GeneralRegs::default(),
            sstatus,
            sepc: 0,
        }
    }
}

impl RawUserContext {
    /// Goes to user space with the context, and comes back when a trap occurs.
    ///
    /// On return, the context will be reset to the status before the trap.
    /// Trap reason and error code will be placed at `scause` and `stval`.
    pub(in crate::arch) fn run(&mut self) {
        let guard = crate::irq::disable_local();

        crate::task::call_pre_user_run_handler(&guard);

        if clear_previous_virtualization_mode() {
            crate::warn!("Cleared stale hstatus.SPV before returning to user mode");
        }

        // Return to userspace with interrupts disabled. Otherwise, interrupts
        // after switching `sscratch` will mess up the CPU state.
        core::mem::forget(guard);

        unsafe { run_user(self) };
    }
}

/// Ensures that `sret` enters ordinary U-mode rather than virtual U-mode.
///
/// The H extension makes `hstatus.SPV` part of the return-mode state used by
/// `sret`. A non-hypervisor kernel must not inherit a stale SPV bit from
/// firmware or an earlier boot stage.
fn clear_previous_virtualization_mode() -> bool {
    if !has_extensions(IsaExtensions::H) {
        return false;
    }

    const HSTATUS_SPV: usize = 1 << 7;
    let previous: usize;
    // SAFETY: The H extension was detected on every application hart. Clearing
    // SPV only selects ordinary (non-virtualized) mode for the next `sret`.
    unsafe {
        asm!(
            "csrrc {previous}, hstatus, {mask}",
            previous = out(reg) previous,
            mask = in(reg) HSTATUS_SPV,
            options(nostack)
        )
    };
    previous & HSTATUS_SPV != 0
}

unsafe extern "C" {
    unsafe fn trap_entry();
    unsafe fn run_user(regs: &mut RawUserContext);
}

#[cfg(ktest)]
mod tests {
    use core::arch::asm;

    use super::clear_previous_virtualization_mode;
    use crate::{
        arch::cpu::extension::{IsaExtensions, has_extensions},
        prelude::ktest,
    };

    const HSTATUS_SPV: usize = 1 << 7;

    #[ktest]
    fn clears_stale_hypervisor_virtualization_before_user_return() {
        if !has_extensions(IsaExtensions::H) {
            return;
        }

        let interrupt_guard = crate::irq::disable_local();

        // SAFETY: H is present, and the test restores the only bit that it
        // changes before returning to the rest of the kernel tests.
        unsafe { asm!("csrs hstatus, {mask}", mask = in(reg) HSTATUS_SPV) };

        assert!(clear_previous_virtualization_mode());

        let hstatus: usize;
        // SAFETY: H is present, so hstatus is accessible from HS-mode.
        unsafe { asm!("csrr {value}, hstatus", value = out(reg) hstatus) };
        assert_eq!(hstatus & HSTATUS_SPV, 0);
        drop(interrupt_guard);
    }
}
