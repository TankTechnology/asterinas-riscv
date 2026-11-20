// SPDX-License-Identifier: MPL-2.0

//! Handles trap.

#[expect(clippy::module_inception)]
mod trap;

use riscv::{
    interrupt::supervisor::{Exception, Interrupt},
    register::scause::Trap,
};
pub use trap::TrapFrame;
pub(super) use trap::{RawUserContext, SSTATUS_FS_MASK, SSTATUS_SUM};

use crate::{
    arch::{
        cpu::context::CpuException,
        irq::{HwIrqLine, IRQ_CHIP, InterruptSource, disable_local, enable_local},
        timer::TIMER_IRQ,
    },
    cpu::PrivilegeLevel,
    irq::call_irq_callback_functions,
};

/// How many external interrupts one trap may service before its source is
/// masked.
///
/// Well above any legitimate burst (a full FIFO of a dozen devices is still
/// far below it) and far below the rate at which a stuck line re-raises:
/// the storm measured on this board claimed about 633,000 interrupts a
/// second, so it hits the budget within a hundredth of a second.
const EXTERNAL_IRQ_BUDGET: u32 = 4096;

/// Initializes interrupt handling on RISC-V.
///
/// # Safety
///
/// On the current CPU, this function must be called
/// - only once and
/// - before any trap can occur.
pub(crate) unsafe fn init_on_cpu() {
    // SAFETY: The caller ensures the safety conditions.
    unsafe {
        trap::init_on_cpu();
    }
}

/// Handle traps (only from kernel).
// SAFETY: The name does not collide with other symbols.
#[unsafe(no_mangle)]
unsafe extern "C" fn trap_handler(f: &mut TrapFrame) {
    // A liveness stamp for the stall report: if a hart stops taking traps,
    // nothing else it does can update this, and the age at report time says
    // how long it has been out of touch.
    crate::smp::note_trap(f.sepc);

    fn enable_local_if(cond: bool) {
        if cond {
            enable_local();
        }
    }

    fn disable_local_if(cond: bool) {
        if cond {
            disable_local();
        }
    }

    let scause = riscv::register::scause::read();
    crate::smp::note_trap_cause(scause.bits() as u64);
    let Ok(cause) = Trap::<Interrupt, Exception>::try_from(scause.cause()) else {
        panic!(
            "Cannot handle unknown trap: {:#x?}; trapframe: {:#x?}",
            scause, f
        );
    };

    let exception = match cause {
        Trap::Interrupt(interrupt) => {
            handle_irq(f, interrupt, PrivilegeLevel::Kernel);
            return;
        }
        Trap::Exception(raw_exception) => {
            let stval = riscv::register::stval::read();
            CpuException::new(raw_exception, stval)
        }
    };

    // The IRQ state before trapping. We need to ensure that the IRQ state
    // during exception handling is consistent with the state before the trap.
    const SSTATUS_SPIE: usize = 1 << 5;
    let was_irq_enabled = (f.sstatus & SSTATUS_SPIE) != 0;

    enable_local_if(was_irq_enabled);
    match exception {
        CpuException::InstructionPageFault(fault_addr)
        | CpuException::LoadPageFault(fault_addr)
        | CpuException::StorePageFault(fault_addr) => {
            crate::mm::fault::handle_user_page_fault(f, &exception, fault_addr);
        }
        _ => {
            panic!(
                "Cannot handle kernel CPU exception: {:#x?}; trapframe: {:#x?}",
                exception, f
            );
        }
    };
    disable_local_if(was_irq_enabled);
}

pub(super) fn handle_irq(trap_frame: &TrapFrame, interrupt: Interrupt, priv_level: PrivilegeLevel) {
    // One marker at the top of the handler, with the interrupt's kind, so a
    // stall report can say which interrupt the target last started handling
    // and whether it got past the entry of that handler at all.
    crate::smp::note_irq_enter(match interrupt {
        Interrupt::SupervisorTimer => 1,
        Interrupt::SupervisorExternal => 2,
        Interrupt::SupervisorSoft => 3,
    });
    match interrupt {
        Interrupt::SupervisorTimer => {
            // Sampling hooks run before the handlers so the sample is the
            // interrupted context, not the handler's.
            crate::irq::call_timer_sample_hook(
                trap_frame.sepc,
                matches!(priv_level, PrivilegeLevel::User),
            );
            call_irq_callback_functions(
                trap_frame,
                &HwIrqLine::new(TIMER_IRQ.get().unwrap().num(), InterruptSource::Timer),
                priv_level,
            );
        }
        Interrupt::SupervisorExternal => {
            // No races because we're in IRQs.
            let hart_id = crate::arch::boot::smp::get_current_hart_id();
            // The claim loop is bounded: a level-triggered source that its
            // handler cannot quiesce would otherwise keep this hart inside
            // one trap handler forever -- taking no timer, no IPI, and
            // writing no progress -- which is indistinguishable from a dead
            // hart from the outside. The budget is per trap, not cumulative.
            let mut served = 0u32;
            loop {
                // Marked before the claim: if the claim's MMIO read never
                // returns, this is the only marker that will be left behind.
                crate::smp::note_activity(crate::smp::ACT_PLIC_CLAIM);
                let Some(hw_irq_line) = IRQ_CHIP.get().unwrap().claim_interrupt(hart_id) else {
                    crate::smp::note_activity(0);
                    break;
                };
                crate::smp::note_ext_irq(hw_irq_line.irq_num());
                served += 1;
                if served > EXTERNAL_IRQ_BUDGET {
                    // Mask the offending source only, so every other device
                    // keeps working, and say which line it was: the mask is
                    // permanent, so this line is its only memorial.
                    hw_irq_line.ack();
                    hw_irq_line.mask();
                    crate::smp::note_irq_storm(hw_irq_line.irq_num());
                    crate::early_println!(
                        "ASTERINAS_IRQ_STORM line={} served={} action=masked",
                        hw_irq_line.irq_num(),
                        served
                    );
                    break;
                }
                call_irq_callback_functions(trap_frame, &hw_irq_line, priv_level);
            }
        }
        Interrupt::SupervisorSoft => {
            crate::smp::note_ipi_trap();
            // Clear SSIP before draining IPI callbacks. A sender can enqueue
            // another callback while this handler runs; a post-callback clear
            // would erase its new pending interrupt and strand the callback.
            // SAFETY: We are handling the supervisor software interrupt on
            // this hart, so acknowledging its pending bit is permitted.
            unsafe { riscv::register::sip::clear_ssoft() };
            let ipi_irq_num = super::irq::ipi::IPI_IRQ.get().unwrap().num();
            call_irq_callback_functions(
                trap_frame,
                &HwIrqLine::new(ipi_irq_num, InterruptSource::Software),
                priv_level,
            );
        }
    }
}
