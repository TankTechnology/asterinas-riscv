// SPDX-License-Identifier: MPL-2.0

//! Direct Memory Access (DMA).
//!
//! This module provides [`DmaCoherent`] and [`DmaStream`] abstractions for
//! managing DMA memory regions with different remapping, caching and
//! synchronization requirements.
//!
//! # Usage in IRQs
//!
//! Creating DMA objects (via `alloc`, `alloc_uninit`, or `map` constructors)
//! requires IRQs to be enabled, to avoid deadlocks during cross-CPU TLB
//! flushes. Note that it means DMA objects cannot be created from (hard)
//! interrupt context.
//!
//! Other operations on DMA objects may still be performed in any context,
//! even with IRQs disabled. For example, it is valid to drop a [`DmaStream`]
//! from an IRQ handler after the device has finished processing it.

#[cfg(ktest)]
mod test;

mod dma_coherent;
mod dma_stream;
mod dma_window;
#[cfg(target_arch = "riscv64")]
mod usb_kernel_op;
mod util;

pub use dma_coherent::DmaCoherent;
pub use dma_stream::{DmaDirection, DmaStream, FromAndToDevice, FromDevice, ToDevice};
pub use dma_window::DmaWindow;
#[cfg(target_arch = "riscv64")]
pub use usb_kernel_op::UsbKernelOp;

use crate::{
    error::Error,
    mm::{PAGE_SIZE, Paddr, frame, paddr_to_vaddr},
};

/// The cache-maintenance direction for [`sync_dma_range`].
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DmaSyncDirection {
    /// The CPU wrote the range and the device will read it.
    ToDevice,
    /// The device wrote the range and the CPU will read it.
    FromDevice,
    /// Both sides wrote the range and both will read it.
    Both,
}

/// Performs cache maintenance for a physically contiguous DMA range.
///
/// This is the escape hatch for DMA memory that is not owned by a
/// [`DmaStream`] or [`DmaCoherent`] — for example, a buffer carved out of a
/// device's shared pool. On cache-coherent systems it is a no-op.
///
/// The range must start on a page boundary. Every page it covers is checked
/// against the frame metadata, so an out-of-range or unbacked physical range
/// is refused rather than handed to a cache-maintenance instruction.
pub fn sync_dma_range(paddr: Paddr, size: usize, direction: DmaSyncDirection) -> Result<(), Error> {
    if size == 0 {
        return Ok(());
    }
    if !paddr.is_multiple_of(PAGE_SIZE) {
        return Err(Error::InvalidArgs);
    }
    let end = paddr.checked_add(size).ok_or(Error::InvalidArgs)?;
    let mut page = paddr;
    while page < end {
        if !frame::meta::is_in_use(page) {
            return Err(Error::InvalidArgs);
        }
        page = page.checked_add(PAGE_SIZE).ok_or(Error::InvalidArgs)?;
    }
    if !crate::arch::mm::can_sync_dma() {
        return Ok(());
    }
    let start = paddr_to_vaddr(paddr);
    let range = start..start + size;
    // SAFETY: every page in the range is an allocated frame, so the
    // direct-map range is backed by normal RAM and the cache operations are
    // safe to perform. Device access is the caller's synchronization
    // responsibility, as with any DMA API.
    unsafe {
        match direction {
            DmaSyncDirection::ToDevice => crate::arch::mm::sync_dma_range::<ToDevice>(range),
            DmaSyncDirection::FromDevice => crate::arch::mm::sync_dma_range::<FromDevice>(range),
            DmaSyncDirection::Both => crate::arch::mm::sync_dma_range::<FromAndToDevice>(range),
        }
    }
    Ok(())
}
