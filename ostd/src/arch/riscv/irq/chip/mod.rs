// SPDX-License-Identifier: MPL-2.0

//! Interrupts.

mod plic;

use alloc::boxed::Box;
use core::{
    fmt,
    ops::{Deref, DerefMut},
};

use spin::Once;

use crate::{
    Result,
    arch::{
        boot::DEVICE_TREE,
        irq::{HwIrqLine, chip::plic::Plic},
    },
    io::IoMemAllocatorBuilder,
    irq::IrqLine,
    sync::{LocalIrqDisabled, SpinLock},
};

/// The [`IrqChip`] singleton.
pub static IRQ_CHIP: Once<IrqChip> = Once::new();

/// Initializes the Platform-Level Interrupt Controller (PLIC) on the BSP.
///
/// # Safety
///
/// This function is safe to call on the following conditions:
/// 1. It is called once and at most once at a proper timing in the boot context
///    of the BSP.
/// 2. It is called before any other public functions of this module is called.
pub(in crate::arch) unsafe fn init_on_bsp(io_mem_builder: &IoMemAllocatorBuilder) {
    let device_tree = DEVICE_TREE.get().unwrap();
    let mut plics = Plic::from_fdt(device_tree, io_mem_builder);
    plics.iter_mut().for_each(|plic| plic.init());
    IRQ_CHIP.call_once(|| IrqChip {
        plics: SpinLock::new(plics.into_boxed_slice()),
    });
    // SAFETY: Accessing the `sie` CSR to enable the external interrupt is safe
    // here because this function is only called during PLIC initialization,
    // and we ensure that only the external interrupt bit is set without
    // affecting other interrupt sources.
    unsafe { riscv::register::sie::set_sext() };
}

/// Initializes application-processor-specific PLIC state.
///
/// # Safety
///
/// This function is safe to call on the following conditions:
/// 1. It is called once and at most once on this AP.
/// 2. It is called before any other public functions of this module is called
///    on this AP.
pub(in crate::arch) unsafe fn init_on_ap() {
    // SAFETY: Accessing the `sie` CSR to enable the external interrupt is safe
    // here due to the same reasons mentioned in `init`.
    unsafe { riscv::register::sie::set_sext() };
}

/// An IRQ chip.
///
/// This abstracts the hardware IRQ chips (or IRQ controllers), allowing the bus
/// or device drivers to enable [`IrqLine`]s (via, e.g., [`map_fdt_pin_to`])
/// regardless of the specifics of the IRQ chip.
///
/// In the RISC-V architecture, the underlying hardware is typically Platform-Level
/// Interrupt Controller (PLIC).
///
/// [`map_fdt_pin_to`]: Self::map_fdt_pin_to
pub struct IrqChip {
    plics: SpinLock<Box<[Plic]>, LocalIrqDisabled>,
}

impl IrqChip {
    /// Maps an IRQ pin specified by `interrupt_source_in_fdt` to an IRQ line.
    pub fn map_fdt_pin_to(
        &self,
        interrupt_source_in_fdt: InterruptSourceInFdt,
        irq_line: IrqLine,
    ) -> Result<MappedIrqLine> {
        self.map_fdt_pin_to_with_state(interrupt_source_in_fdt, irq_line, InterruptState::Unmasked)
    }

    /// Maps an IRQ pin while keeping the interrupt source priority masked.
    pub fn map_fdt_pin_to_masked(
        &self,
        interrupt_source_in_fdt: InterruptSourceInFdt,
        irq_line: IrqLine,
    ) -> Result<DeferredMappedIrqLine> {
        if !irq_line.is_dedicated_and_empty() {
            return Err(crate::Error::InvalidArgs);
        }
        let mapped_irq_line = self.map_fdt_pin_to_with_state(
            interrupt_source_in_fdt,
            irq_line,
            InterruptState::Masked,
        )?;
        Ok(DeferredMappedIrqLine {
            mapped_irq_line,
            callback_registered: false,
        })
    }

    fn map_fdt_pin_to_with_state(
        &self,
        interrupt_source_in_fdt: InterruptSourceInFdt,
        irq_line: IrqLine,
        initial_state: InterruptState,
    ) -> Result<MappedIrqLine> {
        let mut plics = self.plics.lock();
        let (index, plic) = plics
            .iter_mut()
            .enumerate()
            .find(|(_, plic)| plic.phandle() == interrupt_source_in_fdt.interrupt_parent)
            .unwrap();

        plic.map_interrupt_source_to(interrupt_source_in_fdt.interrupt, &irq_line)?;
        plic.set_priority(interrupt_source_in_fdt.interrupt, initial_state.priority());
        plic.managed_harts().for_each(|hart| {
            plic.set_interrupt_enabled(hart, interrupt_source_in_fdt.interrupt, true)
        });
        crate::arch::device::io_mem::fence();

        Ok(MappedIrqLine {
            irq_line,
            interrupt_source_on_chip: InterruptSourceOnChip {
                index,
                interrupt: interrupt_source_in_fdt.interrupt,
            },
        })
    }

    fn set_interrupt_state(
        &self,
        interrupt_source_on_chip: InterruptSourceOnChip,
        state: InterruptState,
    ) {
        let mut plics = self.plics.lock();
        write_priority_with_fences(
            state.priority(),
            |priority| {
                plics[interrupt_source_on_chip.index]
                    .set_priority(interrupt_source_on_chip.interrupt, priority);
            },
            crate::arch::device::io_mem::fence,
        );
    }

    /// Claims an external interrupt that is pending on a specific hart.
    ///
    /// It returns the software IRQ number if there's a pending interrupt on the
    /// hart, otherwise it will return `None`.
    pub(in crate::arch) fn claim_interrupt(&self, hart: u32) -> Option<HwIrqLine> {
        self.plics
            .lock()
            .iter()
            .enumerate()
            .find_map(|(index, plic)| {
                let interrupt = plic.claim_interrupt(hart);
                plic.interrupt_number_mapping(interrupt).map(|irq_num| {
                    HwIrqLine::new_external(irq_num, InterruptSourceOnChip { index, interrupt })
                })
            })
    }

    /// Acknowledges the completion of an interrupt.
    pub(super) fn complete_interrupt(
        &self,
        hart: u32,
        interrupt_source_on_chip: InterruptSourceOnChip,
    ) {
        let plics = self.plics.lock();
        plics[interrupt_source_on_chip.index]
            .complete_interrupt(hart, interrupt_source_on_chip.interrupt);
        crate::arch::device::io_mem::fence();
    }

    /// Unmaps an IRQ line from the IRQ chip.
    fn unmap_irq_line(&self, mapped_irq_line: &MappedIrqLine) {
        let mut plics = self.plics.lock();

        let InterruptSourceOnChip { index, interrupt } = &mapped_irq_line.interrupt_source_on_chip;
        let plic = &mut plics[*index];

        plic.managed_harts()
            .for_each(|hart| plic.set_interrupt_enabled(hart, *interrupt, false));
        plic.set_priority(*interrupt, 0);
        plic.unmap_interrupt_source(*interrupt);
        crate::arch::device::io_mem::fence();
    }
}

/// An [`IrqLine`] mapped to an IRQ pin managed by the [`IRQ_CHIP`].
///
/// When the object is dropped, the IRQ line will be unmapped by the IRQ chip.
pub struct MappedIrqLine {
    irq_line: IrqLine,
    interrupt_source_on_chip: InterruptSourceOnChip,
}

impl Deref for MappedIrqLine {
    type Target = IrqLine;

    fn deref(&self) -> &Self::Target {
        &self.irq_line
    }
}

impl DerefMut for MappedIrqLine {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.irq_line
    }
}

impl fmt::Debug for MappedIrqLine {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("MappedIrqLine")
            .field("irq_line", &self.irq_line)
            .field("interrupt_source_on_chip", &self.interrupt_source_on_chip)
            .finish_non_exhaustive()
    }
}

impl Drop for MappedIrqLine {
    fn drop(&mut self) {
        IRQ_CHIP.get().unwrap().unmap_irq_line(self)
    }
}

/// A dedicated IRQ mapping for level-triggered work deferred to task context.
///
/// Unlike [`MappedIrqLine`], this type does not expose or clone its underlying
/// [`IrqLine`], so one phased callback always controls exactly one PLIC source.
pub struct DeferredMappedIrqLine {
    mapped_irq_line: MappedIrqLine,
    callback_registered: bool,
}

impl DeferredMappedIrqLine {
    fn mask(&self) {
        IRQ_CHIP.get().unwrap().set_interrupt_state(
            self.mapped_irq_line.interrupt_source_on_chip,
            InterruptState::Masked,
        );
    }

    /// Rearms this interrupt source after its deferred work has drained it.
    pub fn rearm(&mut self) -> Result<()> {
        if !self.callback_registered {
            return Err(crate::Error::InvalidArgs);
        }
        IRQ_CHIP.get().unwrap().set_interrupt_state(
            self.mapped_irq_line.interrupt_source_on_chip,
            InterruptState::Unmasked,
        );
        Ok(())
    }

    /// Masks this source before completion and runs `callback` afterwards.
    ///
    /// The callback runs after the completion write is fenced. It must remain
    /// short and non-blocking, and must not clear callbacks or drop the owning
    /// mapping. The source remains priority-masked until [`Self::rearm`] is
    /// called by deferred work.
    pub fn on_active_and_mask<F>(&mut self, callback: F) -> Result<()>
    where
        F: Fn(&crate::arch::trap::TrapFrame) + Sync + Send + 'static,
    {
        if self.callback_registered {
            return Err(crate::Error::InvalidArgs);
        }
        let interrupt_source_on_chip = self.mapped_irq_line.interrupt_source_on_chip;
        self.mapped_irq_line.irq_line.on_active_with_post_ack(
            move |_| {
                IRQ_CHIP
                    .get()
                    .unwrap()
                    .set_interrupt_state(interrupt_source_on_chip, InterruptState::Masked);
            },
            callback,
        )?;
        self.callback_registered = true;
        Ok(())
    }
}

impl fmt::Debug for DeferredMappedIrqLine {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("DeferredMappedIrqLine")
            .field("mapped_irq_line", &self.mapped_irq_line)
            .field("callback_registered", &self.callback_registered)
            .finish_non_exhaustive()
    }
}

impl Drop for DeferredMappedIrqLine {
    fn drop(&mut self) {
        // Do not hold the IRQ-chip lock while waiting for in-flight callbacks:
        // callbacks mask through that same lock before they return.
        self.mask();
        self.mapped_irq_line.irq_line.clear_callbacks();
        // `mapped_irq_line` is dropped next and removes the PLIC mapping only
        // after all claim-time callback snapshots have been released.
    }
}

/// Interrupt source identifier in the device tree.
#[derive(Clone, Copy, Debug)]
pub struct InterruptSourceInFdt {
    /// Phandle of the interrupt controller it connects to.
    pub interrupt_parent: u32,
    /// Interrupt source number on the interrupt controller.
    pub interrupt: u32,
}

/// Interrupt source identifier on the `IRQ_CHIP`.
#[derive(Clone, Copy, Debug)]
pub(super) struct InterruptSourceOnChip {
    /// Index of the interrupt controller it connects to on `IRQ_CHIP`.
    index: usize,
    /// Interrupt source number on the interrupt controller.
    interrupt: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum InterruptState {
    Masked,
    Unmasked,
}

impl InterruptState {
    fn priority(self) -> u32 {
        match self {
            Self::Masked => 0,
            Self::Unmasked => 1,
        }
    }
}

fn write_priority_with_fences(
    priority: u32,
    mut write_priority: impl FnMut(u32),
    mut fence: impl FnMut(),
) {
    fence();
    write_priority(priority);
    fence();
}

#[cfg(ktest)]
mod tests {
    use core::cell::Cell;

    use super::*;
    use crate::prelude::ktest;

    fn exercise_masked_irq_api(mapped: &mut DeferredMappedIrqLine) {
        let _ = mapped.on_active_and_mask(|_| {});
        let _ = mapped.rearm();
    }

    #[ktest]
    fn exposes_owned_masked_irq_control() {
        let _map_masked: fn(
            &IrqChip,
            InterruptSourceInFdt,
            IrqLine,
        ) -> Result<DeferredMappedIrqLine> = IrqChip::map_fdt_pin_to_masked;
        let _exercise = exercise_masked_irq_api as fn(&mut DeferredMappedIrqLine);
    }

    #[ktest]
    fn interrupt_state_selects_source_priority() {
        assert_eq!(InterruptState::Masked.priority(), 0);
        assert_eq!(InterruptState::Unmasked.priority(), 1);
    }

    #[ktest]
    fn priority_write_is_fenced_on_both_sides() {
        let stage = Cell::new(0);
        write_priority_with_fences(
            InterruptState::Unmasked.priority(),
            |priority| {
                assert_eq!(stage.get(), 1);
                assert_eq!(priority, 1);
                stage.set(2);
            },
            || match stage.get() {
                0 => stage.set(1),
                2 => stage.set(3),
                unexpected => panic!("unexpected fence at stage {unexpected}"),
            },
        );
        assert_eq!(stage.get(), 3);
    }

    const TEST_SOURCE: u32 = 1;

    struct GatewayModel {
        enabled: bool,
        priority: u32,
        pending: bool,
        claimed: bool,
        level_asserted: bool,
    }

    impl GatewayModel {
        fn new() -> Self {
            Self {
                enabled: true,
                priority: 1,
                pending: false,
                claimed: false,
                level_asserted: false,
            }
        }

        fn set_level(&mut self, asserted: bool) {
            self.level_asserted = asserted;
            if asserted && !self.claimed {
                self.pending = true;
            }
        }

        fn claim(&mut self) -> u32 {
            if self.enabled && self.priority > 0 && self.pending && !self.claimed {
                self.pending = false;
                self.claimed = true;
                TEST_SOURCE
            } else {
                0
            }
        }

        fn complete(&mut self, source: u32) {
            if source != TEST_SOURCE || !self.enabled || !self.claimed {
                return;
            }

            self.claimed = false;
            if self.level_asserted {
                self.pending = true;
            }
        }
    }

    #[ktest]
    fn disabling_target_makes_claim_completion_ignored() {
        let mut gateway = GatewayModel::new();
        gateway.set_level(true);
        assert_eq!(gateway.claim(), TEST_SOURCE);

        gateway.enabled = false;
        gateway.complete(TEST_SOURCE);

        assert!(gateway.claimed);
    }

    #[ktest]
    fn priority_mask_preserves_completion_and_bounds_stale_claim() {
        let mut gateway = GatewayModel::new();
        gateway.set_level(true);
        assert_eq!(gateway.claim(), TEST_SOURCE);

        gateway.priority = InterruptState::Masked.priority();
        gateway.complete(TEST_SOURCE);
        assert!(!gateway.claimed);
        assert!(gateway.pending);
        assert_eq!(gateway.claim(), 0);

        gateway.set_level(false);
        gateway.priority = InterruptState::Unmasked.priority();
        assert_eq!(gateway.claim(), TEST_SOURCE);
        gateway.complete(TEST_SOURCE);
        assert_eq!(gateway.claim(), 0);
    }

    #[ktest]
    fn external_claim_holds_registered_callback_until_hw_token_drop() {
        let mut irq_line = IrqLine::alloc().unwrap();
        irq_line.on_active_with_post_ack(|_| {}, |_| {}).unwrap();

        let claimed_irq = HwIrqLine::new_external(
            irq_line.num(),
            InterruptSourceOnChip {
                index: 0,
                interrupt: TEST_SOURCE,
            },
        );

        // While the registry slot exists, the handle and slot own two baseline
        // Arcs. A third Arc proves that `new_external` captured the callback at
        // claim time. Moving that snapshot back into `process` makes this fail.
        assert!(irq_line.has_claim_time_phased_snapshot());
        drop(claimed_irq);
        assert!(!irq_line.has_claim_time_phased_snapshot());

        irq_line.clear_callbacks();
        assert!(irq_line.is_empty());
    }
}
