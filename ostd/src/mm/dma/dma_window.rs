// SPDX-License-Identifier: MPL-2.0

use core::ops::Range;

use crate::mm::{Daddr, Paddr};

/// A linear address window between CPU physical memory and a DMA device.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DmaWindow {
    device_start: Daddr,
    cpu_start: Paddr,
    size: usize,
}

impl DmaWindow {
    /// Creates a non-empty DMA window whose endpoints are representable.
    pub fn new(device_start: Daddr, cpu_start: Paddr, size: usize) -> Option<Self> {
        if size == 0
            || device_start.checked_add(size).is_none()
            || cpu_start.checked_add(size).is_none()
        {
            return None;
        }

        Some(Self {
            device_start,
            cpu_start,
            size,
        })
    }

    /// Returns the first device-visible address in the window.
    pub fn device_start(&self) -> Daddr {
        self.device_start
    }

    /// Returns the first CPU physical address in the window.
    pub fn cpu_start(&self) -> Paddr {
        self.cpu_start
    }

    /// Returns the size of the window in bytes.
    pub fn size(&self) -> usize {
        self.size
    }

    /// Translates a non-empty CPU physical address range for the device.
    pub fn translate(&self, cpu_range: Range<Paddr>) -> Option<Range<Daddr>> {
        if cpu_range.start >= cpu_range.end {
            return None;
        }

        let cpu_end = self.cpu_start.checked_add(self.size)?;
        if cpu_range.start < self.cpu_start || cpu_range.end > cpu_end {
            return None;
        }

        let start_offset = cpu_range.start.checked_sub(self.cpu_start)?;
        let end_offset = cpu_range.end.checked_sub(self.cpu_start)?;
        Some(
            self.device_start.checked_add(start_offset)?
                ..self.device_start.checked_add(end_offset)?,
        )
    }
}
