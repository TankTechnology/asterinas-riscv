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

/// Selects the 64-bit base ISA for U-mode on RV64.
/// Reference: <https://riscv.github.io/riscv-isa-manual/snapshot/privileged/#base-isa-control-in-sstatus-register>.
const SSTATUS_UXL_64: usize = 0b10 << 32;

/// Supervisor User Memory access bit.
/// Reference: <https://riscv.github.io/riscv-isa-manual/snapshot/privileged/#sstatus>.
pub(in crate::arch) const SSTATUS_SUM: usize = 0b1 << 18;

/// Supervisor Previous Privilege bit, selecting S-mode on trap return.
/// Reference: <https://docs.riscv.org/reference/isa/v20260120/priv/supervisor.html>.
const SSTATUS_SPP: usize = 1 << 8;

#[cfg(not(ktest))]
global_asm!(
    include_str!("trap.S"),
    SSTATUS_FS_MASK = const SSTATUS_FS_MASK,
    SSTATUS_SUM = const SSTATUS_SUM,
    SSTATUS_SPP = const SSTATUS_SPP
);

// Keep the test helper in the same assembly unit so the production return
// label remains private to this file.
#[cfg(ktest)]
global_asm!(
    include_str!("trap.S"),
    include_str!("return_test.S"),
    SSTATUS_FS_MASK = const SSTATUS_FS_MASK,
    SSTATUS_SUM = const SSTATUS_SUM,
    SSTATUS_SPP = const SSTATUS_SPP
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
        let fpu_status = if has_extensions(IsaExtensions::F)
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
            sstatus: SSTATUS_UXL_64 | fpu_status,
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

        let guard = crate::irq::disable_local();
        crate::task::call_post_user_run_handler(&guard);
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

    use super::{RawUserContext, SSTATUS_UXL_64, clear_previous_virtualization_mode};
    use crate::{
        arch::cpu::extension::{IsaExtensions, has_extensions},
        prelude::ktest,
    };

    const HSTATUS_SPV: usize = 1 << 7;
    const SSTATUS_UXL_MASK: usize = 0b11 << 32;

    unsafe extern "C" {
        fn riscv_test_kernel_trap_return_gp(saved_gp: usize) -> usize;
    }

    #[ktest]
    fn kernel_trap_return_preserves_current_cpu_gp() {
        let _irq_guard = crate::irq::disable_local();
        clear_previous_virtualization_mode();
        let current_gp = crate::arch::cpu::local::get_base() as usize;

        // A sleeping kernel page-fault handler can resume on another CPU. Its
        // saved gp then differs from the live CPU-local base. The helper repairs
        // gp before returning to Rust, so even the regression failure is safe.
        // SAFETY: IRQs stay disabled through the synthetic S-mode return. The
        // helper restores the stack, callee-saved registers, gp, and trap CSRs.
        let observed_gp = unsafe { riscv_test_kernel_trap_return_gp(0) };
        assert_eq!(observed_gp, current_gp, "kernel return restored stale gp");

        // SAFETY: The same helper contract holds when no migration occurred.
        let observed_gp = unsafe { riscv_test_kernel_trap_return_gp(current_gp) };
        assert_eq!(observed_gp, current_gp);
    }

    #[ktest]
    fn user_trap_return_restores_user_gp() {
        let _irq_guard = crate::irq::disable_local();
        clear_previous_virtualization_mode();
        let current_gp = crate::arch::cpu::local::get_base();
        let mut context = RawUserContext::default();
        const USER_GP: usize = 0x1234_5678;
        context.general.gp = USER_GP;
        // This supervisor-only address faults before executing a user
        // instruction, exercising both real user return and trap entry.
        context.sepc = super::trap_entry as *const () as usize;
        let saved_sie: usize;
        let saved_sstatus: usize;
        let saved_sepc: usize;
        let saved_scause: usize;
        let saved_stval: usize;
        // SAFETY: IRQs are disabled and sscratch is zero in kernel context.
        // Mask individual interrupt sources as U-mode ignores sstatus.SIE.
        // No Rust executes with user gp; run_user restores the kernel gp.
        unsafe {
            asm!("csrrw {}, sie, zero", out(reg) saved_sie);
            asm!("csrr {}, sstatus", out(reg) saved_sstatus);
            asm!("csrr {}, sepc", out(reg) saved_sepc);
            asm!("csrr {}, scause", out(reg) saved_scause);
            asm!("csrr {}, stval", out(reg) saved_stval);
            super::run_user(&mut context);
            asm!("csrw sstatus, {}", in(reg) saved_sstatus);
            asm!("csrw sepc, {}", in(reg) saved_sepc);
            asm!("csrw scause, {}", in(reg) saved_scause);
            asm!("csrw stval, {}", in(reg) saved_stval);
            asm!("csrw sie, {}", in(reg) saved_sie);
        }
        assert_eq!(context.general.gp, USER_GP);
        assert_eq!(crate::arch::cpu::local::get_base(), current_gp);
        assert_eq!(
            context.sstatus & super::SSTATUS_SPP,
            0,
            "trap did not originate in U-mode"
        );
    }

    #[ktest]
    fn defaults_to_64_bit_user_mode() {
        let context = RawUserContext::default();

        assert_eq!(context.sstatus & SSTATUS_UXL_MASK, SSTATUS_UXL_64);
    }

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
