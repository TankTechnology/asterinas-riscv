// SPDX-License-Identifier: MPL-2.0

//! Inter-processor interrupts.

use core::sync::atomic::{AtomicBool, Ordering as AtomicOrdering};

use spin::Once;

use crate::{cpu::PinCurrentCpu, error::Error, irq::IrqLine, prelude::Result};

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
    // reasons mentioned in `init_on_bsp`.
    unsafe { riscv::register::sie::set_ssoft() };
}

/// Firmware calls made to send an inter-processor interrupt.
///
/// Each call enters and leaves machine mode, so this is the number that says
/// how much work the firmware is being asked to do, independent of how many
/// harts a single interrupt reaches.
static SBI_IPI_CALLS: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);

/// Returns how many SBI calls have been made to send interrupts.
pub(crate) fn sbi_ipi_calls() -> u32 {
    SBI_IPI_CALLS.load(core::sync::atomic::Ordering::Relaxed)
}

/// Sends a general inter-processor interrupt (IPI) to the specified CPU.
pub(crate) fn send_ipi(hw_cpu_id: HwCpuId, _guard: &dyn PinCurrentCpu) -> Result<()> {
    SBI_IPI_CALLS.fetch_add(1, core::sync::atomic::Ordering::Relaxed);
    send_ipi_mask(single_hart_mask(hw_cpu_id), hw_cpu_id.0)
}

/// Whether firmware calls that send interrupts are serialized across harts.
///
/// Three harts entering machine mode to send each other interrupts is the one
/// condition the board has been shown to fail under: three senders reset it in
/// 23 seconds while a single sender at a higher rate ran for minutes.
/// Serializing the calls removes the overlap at the price of one atomic per
/// send.
static SERIALIZED: AtomicBool = AtomicBool::new(false);
/// Held across one firmware call by the CPU that is sending.
static SEND_BUSY: AtomicBool = AtomicBool::new(false);

/// Enables or disables serialization of the firmware calls that send interrupts.
pub fn set_sbi_ipi_serialized(enabled: bool) {
    SERIALIZED.store(enabled, AtomicOrdering::Relaxed);
}

fn sbi_send_ipi(mask: sbi_rt::HartMask) -> sbi_rt::SbiRet {
    if !SERIALIZED.load(AtomicOrdering::Relaxed) {
        return sbi_rt::send_ipi(mask);
    }
    while SEND_BUSY.swap(true, AtomicOrdering::Acquire) {
        core::hint::spin_loop();
    }
    let ret = sbi_rt::send_ipi(mask);
    SEND_BUSY.store(false, AtomicOrdering::Release);
    ret
}

fn send_ipi_mask(mask: sbi_rt::HartMask, hart: u32) -> Result<()> {
    // No logging on this path: it runs once per inter-processor interrupt,
    // thousands of times a second during boot whenever the level is lowered.
    let ret = sbi_send_ipi(mask);

    if ret.error == 0 {
        Ok(())
    } else {
        crate::error!(
            "Failed to send IPI to hart {}: error code {}",
            hart,
            ret.error
        );
        Err(Error::IoError)
    }
}

/// Sends one interrupt to every hart in the iterator.
///
/// The SBI hart-mask parameter names a set of harts, so harts that share a
/// mask word are notified with a single firmware call instead of one per
/// hart. Harts beyond the first word fall back to one call each.
pub(crate) fn send_ipi_to_harts(hw_cpu_ids: &[HwCpuId], _guard: &dyn PinCurrentCpu) -> Result<()> {
    if hw_cpu_ids.is_empty() {
        return Ok(());
    }
    if let Some(hart_mask) = first_word_hart_mask(hw_cpu_ids) {
        SBI_IPI_CALLS.fetch_add(1, core::sync::atomic::Ordering::Relaxed);
        let hart = hw_cpu_ids.first().map_or(0, |hw_cpu_id| hw_cpu_id.0);
        return send_ipi_mask(hart_mask, hart);
    }

    for &hw_cpu_id in hw_cpu_ids {
        send_ipi(hw_cpu_id, _guard)?;
    }
    Ok(())
}

/// Flushes the instruction cache on every online hart through SBI RFENCE.
///
/// An SBI hart mask may contain only harts that are enabled and available to
/// the supervisor. Therefore, the mask is derived from the hardware CPU IDs
/// recorded during SMP initialization instead of setting every mask bit.
///
/// Reference:
/// <https://github.com/riscv-non-isa/riscv-sbi-doc/blob/v3.0/src/binary-encoding.adoc#hart-list-parameter>.
pub(crate) fn remote_fence_i_all_online_harts(hw_cpu_ids: &[HwCpuId]) -> Result<(), crate::Error> {
    remote_fence_i_all_online_harts_with(hw_cpu_ids, sbi_rt::remote_fence_i)
}

fn remote_fence_i_all_online_harts_with(
    hw_cpu_ids: &[HwCpuId],
    mut remote_fence_i: impl FnMut(sbi_rt::HartMask) -> sbi_rt::SbiRet,
) -> Result<(), crate::Error> {
    if let Some(hart_mask) = first_word_hart_mask(hw_cpu_ids) {
        let ret = remote_fence_i(hart_mask);
        if ret.error != 0 {
            crate::error!("SBI remote fence.i failed: error code {}", ret.error);
            return Err(crate::Error::IoError);
        }
        return Ok(());
    }

    // A hart mask covers at most `XLEN` consecutive IDs. Platforms with IDs
    // beyond the first word are uncommon, so use one SBI call per hart there.
    for &hw_cpu_id in hw_cpu_ids {
        let ret = remote_fence_i(single_hart_mask(hw_cpu_id));
        if ret.error != 0 {
            crate::error!(
                "SBI remote fence.i to hart {} failed: error code {}",
                hw_cpu_id.0,
                ret.error
            );
            return Err(crate::Error::IoError);
        }
    }

    Ok(())
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
    use super::{
        HwCpuId, XLEN, first_word_hart_mask, remote_fence_i_all_online_harts_with, single_hart_mask,
    };
    use crate::{Error, prelude::ktest};

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

    #[ktest]
    fn propagates_combined_remote_fence_failure() {
        let mut calls = 0;
        // Regression for #100: an SBI RFENCE failure must not be reported as
        // successful instruction-cache synchronization.
        let result = remote_fence_i_all_online_harts_with(&[HwCpuId(0), HwCpuId(2)], |hart_mask| {
            calls += 1;
            assert_eq!(hart_mask.into_inner(), ((1 << 0) | (1 << 2), 0));
            sbi_rt::SbiRet::not_supported()
        });

        assert_eq!(result, Err(Error::IoError));
        assert_eq!(calls, 1);
    }

    #[ktest]
    fn stops_per_hart_remote_fence_after_failure() {
        let mut calls = 0;
        let result =
            remote_fence_i_all_online_harts_with(&[HwCpuId(0), HwCpuId(XLEN as u32)], |_| {
                calls += 1;
                if calls == 1 {
                    sbi_rt::SbiRet::success(0)
                } else {
                    sbi_rt::SbiRet::failed()
                }
            });

        assert_eq!(result, Err(Error::IoError));
        assert_eq!(calls, 2);
    }
}
