// SPDX-License-Identifier: MPL-2.0

//! Interrupt operations.

// FIXME: Mark this as unsafe. See
// <https://github.com/asterinas/asterinas/issues/1120#issuecomment-2748696592>.
pub(crate) fn enable_local() {
    crate::irq::note_irq_off_exit();
    // SAFETY: The safety is upheld by the caller.
    unsafe { riscv::interrupt::enable() }
}

/// Whether to enable local IRQs before the idle `wfi`.
///
/// The architectural contract is that a locally enabled pending interrupt
/// resumes `wfi` even with the global enable clear. A platform that does not
/// honor that contract would leave an idle CPU asleep with interrupts
/// disabled, which looks exactly like an interrupt that was never delivered.
/// This switch lets the two be told apart on hardware.
static WFI_ENABLE_FIRST: core::sync::atomic::AtomicBool =
    core::sync::atomic::AtomicBool::new(false);

/// Selects whether the idle path enables local IRQs before `wfi`.
pub fn set_wfi_enable_first(enabled: bool) {
    WFI_ENABLE_FIRST.store(enabled, core::sync::atomic::Ordering::Relaxed);
}

/// Enables local IRQs and halts the CPU to wait for interrupts.
///
/// This method guarantees that no interrupts can occur in the middle. In other words, IRQs must
/// either have been processed before this method is called, or they must wake the CPU up from the
/// halting state.
//
// FIXME: Mark this as unsafe. See
// <https://github.com/asterinas/asterinas/issues/1120#issuecomment-2748696592>.
pub(crate) fn enable_local_and_halt() {
    // RISC-V Instruction Set Manual, Machine-Level ISA, Version 1.13 says:
    // "The WFI instruction can also be executed when interrupts are disabled. The operation of WFI
    // must be unaffected by the global interrupt bits in `mstatus` (MIE and SIE) [..]"
    //
    // So we can use `wfi` even if IRQs are disabled. Pending IRQs can still wake up the CPU, but
    // they will only occur later when we enable local IRQs.
    if WFI_ENABLE_FIRST.load(core::sync::atomic::Ordering::Relaxed) {
        crate::irq::note_irq_off_exit();
        // SAFETY: The safety is upheld by the caller.
        unsafe { riscv::interrupt::enable() };
        riscv::asm::wfi();
    } else {
        riscv::asm::wfi();
        crate::irq::note_irq_off_exit();
        // SAFETY: The safety is upheld by the caller.
        unsafe { riscv::interrupt::enable() }
    }
}

pub(crate) fn disable_local() {
    riscv::interrupt::disable();
}

/// Disables local IRQs and halts the CPU forever.
pub(crate) fn disable_local_and_halt() -> ! {
    riscv::interrupt::disable();
    loop {
        riscv::asm::wfi();
    }
}

pub(crate) fn is_local_enabled() -> bool {
    riscv::register::sstatus::read().sie()
}
