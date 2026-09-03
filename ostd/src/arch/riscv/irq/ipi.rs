// SPDX-License-Identifier: MPL-2.0

//! Inter-processor interrupts.

use spin::Once;

use crate::{cpu::PinCurrentCpu, irq::IrqLine};

const XLEN: usize = usize::BITS as usize;
const XLEN_MASK: usize = XLEN - 1;

/// Hardware-specific, architecture-dependent CPU ID.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct HwCpuId(u32);

impl HwCpuId {
    pub(crate) fn read_current(_guard: &dyn PinCurrentCpu) -> Self {
        // No races because of `_guard`.
        Self(crate::arch::boot::smp::get_current_hart_id())
    }
}

pub(in crate::arch) static IPI_IRQ: Once<IrqLine> = Once::new();

/// Initializes the global IPI-related state and local state on the BSP.
///
/// # Safety
///
/// This function can only be called on the BSP and before any other
/// IPI-related function is called.
pub(in crate::arch) unsafe fn init_on_bsp() {
    let mut irq = IrqLine::alloc().unwrap();
    // SAFETY: This will be called upon an inter-processor interrupt.
    irq.on_active(|f| unsafe { crate::smp::do_inter_processor_call(f) });
    IPI_IRQ.call_once(|| irq);

    // SAFETY: Enabling the software interrupts is safe here because this
    // function cannot be called when others can perform IPI-related
    // operations. And it has no side-effects.
    unsafe { riscv::register::sie::set_ssoft() };
}

/// Initializes the IPI-related state on this AP.
///
/// # Safety
///
/// This function can only be called before any other harts can send IPIs to
/// this application hart.
pub(in crate::arch) unsafe fn init_on_ap() {
    // SAFETY: Enabling the software interrupts is safe here due to the same
    // reasons mentioned in `init`.
    unsafe { riscv::register::sie::set_ssoft() };
}

/// Sends a general inter-processor interrupt (IPI) to the specified CPU.
pub(crate) fn send_ipi(hw_cpu_id: HwCpuId, _guard: &dyn PinCurrentCpu) {
    let ret = sbi_rt::send_ipi(single_hart_mask(hw_cpu_id));

    if ret.error == 0 {
        crate::debug!("Successfully sent IPI to hart {}", hw_cpu_id.0);
    } else {
        crate::error!(
            "Failed to send IPI to hart {}: error code {}",
            hw_cpu_id.0,
            ret.error
        );
    }
}

/// Flushes the instruction cache on every online hart through SBI RFENCE.
///
/// An SBI hart mask may contain only harts that are enabled and available to
/// the supervisor. Therefore, the mask is derived from the hardware CPU IDs
/// recorded during SMP initialization instead of setting every mask bit.
///
/// Reference:
/// <https://github.com/riscv-non-isa/riscv-sbi-doc/blob/v3.0/src/binary-encoding.adoc#hart-list-parameter>.
pub(crate) fn remote_fence_i_all_online_harts(hw_cpu_ids: &[HwCpuId]) {
    if let Some(hart_mask) = first_word_hart_mask(hw_cpu_ids) {
        let ret = sbi_rt::remote_fence_i(hart_mask);
        if ret.error != 0 {
            crate::warn!("SBI remote fence.i failed: error code {}", ret.error);
        }
        return;
    }

    // A hart mask covers at most `XLEN` consecutive IDs. Platforms with IDs
    // beyond the first word are uncommon, so use one SBI call per hart there.
    for &hw_cpu_id in hw_cpu_ids {
        let ret = sbi_rt::remote_fence_i(single_hart_mask(hw_cpu_id));
        if ret.error != 0 {
            crate::warn!(
                "SBI remote fence.i to hart {} failed: error code {}",
                hw_cpu_id.0,
                ret.error
            );
        }
    }
}

fn single_hart_mask(hw_cpu_id: HwCpuId) -> sbi_rt::HartMask {
    let hart_id = hw_cpu_id.0 as usize;
    let hart_mask_base = hart_id & !XLEN_MASK;
    let hart_mask = 1 << (hart_id & XLEN_MASK);
    sbi_rt::HartMask::from_mask_base(hart_mask, hart_mask_base)
}

fn first_word_hart_mask(hw_cpu_ids: &[HwCpuId]) -> Option<sbi_rt::HartMask> {
    let mut hart_mask = 0usize;
    for hw_cpu_id in hw_cpu_ids {
        let hart_id = hw_cpu_id.0 as usize;
        if hart_id >= XLEN {
            return None;
        }
        hart_mask |= 1 << hart_id;
    }
    Some(sbi_rt::HartMask::from_mask_base(hart_mask, 0))
}

#[cfg(ktest)]
mod tests {
    use super::{HwCpuId, first_word_hart_mask, single_hart_mask};
    use crate::prelude::ktest;

    #[ktest]
    fn combines_sparse_harts_in_first_mask_word() {
        let hart_mask = first_word_hart_mask(&[HwCpuId(0), HwCpuId(2), HwCpuId(63)]).unwrap();
        assert_eq!(hart_mask.into_inner(), ((1 << 0) | (1 << 2) | (1 << 63), 0));
    }

    #[ktest]
    fn rejects_harts_beyond_first_mask_word() {
        assert!(first_word_hart_mask(&[HwCpuId(0), HwCpuId(64)]).is_none());
    }

    #[ktest]
    fn locates_single_hart_in_its_mask_word() {
        assert_eq!(single_hart_mask(HwCpuId(130)).into_inner(), (1 << 2, 128));
    }
}
