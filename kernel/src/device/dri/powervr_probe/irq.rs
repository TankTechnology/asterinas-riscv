// SPDX-License-Identifier: MPL-2.0

//! Minimal RGX host interrupt observation for the selected Megrez GPU.
//!
//! The GPU's dedicated line is PLIC source 15 in the prepared DTB.  The
//! vendor LISR acknowledges it through `RGX_CR_IRQ_OS0_EVENT_STATUS` and
//! `RGX_CR_IRQ_OS0_EVENT_CLEAR` (source bits [1:0]); `RGX_CR_MTS_GPU_INT_STATUS`
//! is sampled only for diagnosis.  The handler must clear the firmware event
//! before the top half completes the PLIC claim, otherwise a level-triggered
//! source would immediately re-enter.

use core::sync::atomic::{AtomicU32, AtomicU64, Ordering};

use ostd::{
    arch::{
        boot::DEVICE_TREE,
        irq::{IRQ_CHIP, InterruptSourceInFdt, MappedIrqLine},
    },
    io::IoMem,
    irq::IrqLine,
    mm::VmIoOnce,
};

/// MTS GPU interrupt status (diagnostic).
pub(super) const RGX_CR_MTS_GPU_INT_STATUS: usize = 0x0bb0;
/// Dedicated host event status; bits [1:0] are the event sources.
pub(super) const RGX_CR_IRQ_OS0_EVENT_STATUS: usize = 0x0bd0;
/// Dedicated host event clear; write the pending source bits back.
pub(super) const RGX_CR_IRQ_OS0_EVENT_CLEAR: usize = 0x0be0;
const RGX_IRQ_EVENT_MASK: u32 = 0x3;

static EVENT_COUNT: AtomicU64 = AtomicU64::new(0);
static SPURIOUS_COUNT: AtomicU64 = AtomicU64::new(0);
static LAST_STATUS: AtomicU32 = AtomicU32::new(0);
static LAST_MTS_STATUS: AtomicU32 = AtomicU32::new(0);

/// Maps an IRQ line to the GPU pin and keeps it (and its callback) alive.
///
/// Dropping the guard unregisters the callback and disables/removes the PLIC
/// mapping, so it must happen before the GPU clocks are gated again.
pub(super) struct GpuIrq {
    _mapped: MappedIrqLine,
}

impl GpuIrq {
    /// Installs the handler on the GPU's PLIC pin and starts a fresh count.
    pub(super) fn install(gpu: &IoMem) -> Result<Self, &'static str> {
        let source = gpu_interrupt_source()?;
        let line = IrqLine::alloc().map_err(|_| "gpu_irq_line_unavailable")?;
        let chip = IRQ_CHIP.get().ok_or("gpu_irq_chip_missing")?;
        let mut mapped = chip
            .map_fdt_pin_to(source, line)
            .map_err(|_| "gpu_irq_map_failed")?;

        // Mapping enables the PLIC source, so a boot-time event may arrive
        // before the callback exists. Clear it here to start from a known
        // state; the handler repeats the same ack for every later event.
        let io = gpu.clone();
        if let Ok(status) = io.read_once::<u32>(RGX_CR_IRQ_OS0_EVENT_STATUS) {
            let pending = status & RGX_IRQ_EVENT_MASK;
            if pending != 0 {
                let _ = io.write_once(RGX_CR_IRQ_OS0_EVENT_CLEAR, &pending);
            }
        }
        EVENT_COUNT.store(0, Ordering::Relaxed);
        SPURIOUS_COUNT.store(0, Ordering::Relaxed);
        LAST_STATUS.store(0, Ordering::Relaxed);
        LAST_MTS_STATUS.store(0, Ordering::Relaxed);

        mapped.on_active(move |_| {
            let status = io
                .read_once::<u32>(RGX_CR_IRQ_OS0_EVENT_STATUS)
                .unwrap_or(0)
                & RGX_IRQ_EVENT_MASK;
            let mts_status = io.read_once::<u32>(RGX_CR_MTS_GPU_INT_STATUS).unwrap_or(0);
            match event_ack_value(status) {
                Some(pending) => {
                    let _ = io.write_once(RGX_CR_IRQ_OS0_EVENT_CLEAR, &pending);
                    LAST_STATUS.store(pending, Ordering::Relaxed);
                    LAST_MTS_STATUS.store(mts_status, Ordering::Relaxed);
                    EVENT_COUNT.fetch_add(1, Ordering::Relaxed);
                }
                None => {
                    SPURIOUS_COUNT.fetch_add(1, Ordering::Relaxed);
                }
            }
        });

        Ok(Self { _mapped: mapped })
    }
}

/// The value to write to `RGX_CR_IRQ_OS0_EVENT_CLEAR`, or `None` for a
/// spurious interrupt with no pending source bit.
pub(super) fn event_ack_value(status: u32) -> Option<u32> {
    let pending = status & RGX_IRQ_EVENT_MASK;
    (pending != 0).then_some(pending)
}

pub(super) fn event_count() -> u64 {
    EVENT_COUNT.load(Ordering::Relaxed)
}

/// `(events, spurious, last_status, last_mts_status)`.
pub(super) fn diagnostics() -> (u64, u64, u32, u32) {
    (
        EVENT_COUNT.load(Ordering::Relaxed),
        SPURIOUS_COUNT.load(Ordering::Relaxed),
        LAST_STATUS.load(Ordering::Relaxed),
        LAST_MTS_STATUS.load(Ordering::Relaxed),
    )
}

/// `(interrupt_parent phandle, interrupt source)` for logging.
pub(super) fn interrupt_source() -> Result<(u32, u32), &'static str> {
    let source = gpu_interrupt_source()?;
    Ok((source.interrupt_parent, source.interrupt))
}

/// The GPU pin from the prepared DTB, with an explicit one-cell contract.
fn gpu_interrupt_source() -> Result<InterruptSourceInFdt, &'static str> {
    let tree = DEVICE_TREE.get().ok_or("gpu_irq_no_device_tree")?;
    let node = tree
        .find_compatible(&["img,gpu"])
        .ok_or("gpu_irq_no_gpu_node")?;
    let interrupt_parent = node
        .property("interrupt-parent")
        .and_then(|property| property.as_usize())
        .ok_or("gpu_irq_missing_interrupt_parent")?;
    let interrupt_cells = node
        .interrupt_parent()
        .and_then(|parent| parent.interrupt_cells())
        .ok_or("gpu_irq_missing_interrupt_cells")?;
    if interrupt_cells != 1 {
        return Err("gpu_irq_unsupported_interrupt_cells");
    }
    let interrupt = node
        .interrupts()
        .and_then(|mut interrupts| interrupts.next())
        .ok_or("gpu_irq_missing_interrupt")?;
    Ok(InterruptSourceInFdt {
        interrupt_parent: u32::try_from(interrupt_parent)
            .map_err(|_| "gpu_irq_parent_out_of_range")?,
        interrupt: u32::try_from(interrupt).map_err(|_| "gpu_irq_source_out_of_range")?,
    })
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::event_ack_value;

    #[ktest]
    fn gpu_irq_ack_requires_a_pending_event_source() {
        assert_eq!(event_ack_value(0), None);
        assert_eq!(event_ack_value(0x4), None);
        assert_eq!(event_ack_value(0x1), Some(0x1));
        assert_eq!(event_ack_value(0x2), Some(0x2));
        assert_eq!(event_ack_value(0x3), Some(0x3));
        // Bits outside the two event sources are not acknowledged.
        assert_eq!(event_ack_value(0xffff_fffc), None);
        assert_eq!(event_ack_value(0xffff_ffff), Some(0x3));
    }
}
