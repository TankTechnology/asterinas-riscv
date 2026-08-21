// SPDX-License-Identifier: MPL-2.0

//! PCI bus access

use core::{alloc::Layout, ops::RangeInclusive};

use fdt::node::{CellSizes, FdtNode};
use ostd::{
    Error,
    arch::boot::DEVICE_TREE,
    io::IoMem,
    mm::{Paddr, VmIoOnce},
    sync::SpinLock,
    warn,
};
use spin::Once;

use crate::PciDeviceLocation;

static PCI_ECAM_CFG_SPACE: Once<IoMem> = Once::new();

pub(crate) fn write32(location: &PciDeviceLocation, offset: u32, value: u32) -> Result<(), Error> {
    if offset > PCI_ECAM_MAX_OFFSET {
        return Err(Error::InvalidArgs);
    }
    PCI_ECAM_CFG_SPACE.get().ok_or(Error::IoError)?.write_once(
        (encode_as_address_offset(location) | offset) as usize,
        &value,
    )
}

pub(crate) fn read32(location: &PciDeviceLocation, offset: u32) -> Result<u32, Error> {
    if offset > PCI_ECAM_MAX_OFFSET {
        return Err(Error::InvalidArgs);
    }
    PCI_ECAM_CFG_SPACE
        .get()
        .ok_or(Error::IoError)?
        .read_once((encode_as_address_offset(location) | offset) as usize)
}

/// The maximum offset in the 12-bit configuration space when using [`encode_as_address_offset`].
const PCI_ECAM_MAX_OFFSET: u32 = 0xffc;

/// Encodes the bus, device, and function into an address offset in the PCI MMIO region.
fn encode_as_address_offset(location: &PciDeviceLocation) -> u32 {
    // We only support ECAM here for RISC-V platforms. Offsets are from
    // <https://www.kernel.org/doc/Documentation/devicetree/bindings/pci/host-generic-pci.txt>.
    ((location.bus as u32) << 20)
        | ((location.device as u32) << 15)
        | ((location.function as u32) << 12)
}

/// Initializes the platform-specific module for accessing the PCI configuration space.
///
/// Returns a range for the PCI bus number, or [`None`] if there is no PCI bus.
pub(crate) fn init() -> Option<RangeInclusive<u8>> {
    // We follow the Linux's PCI device tree to obtain the register information
    // about the PCI bus. See also the specification at
    // <https://www.kernel.org/doc/Documentation/devicetree/bindings/pci/host-generic-pci.txt>.
    //
    // TODO: Support multiple PCIe segment groups instead of assuming only one
    // PCIe segment group is in use.
    let Some(pci) = DEVICE_TREE
        .get()
        .unwrap()
        .find_compatible(&["pci-host-ecam-generic"])
    else {
        warn!("no generic host controller node found in the device tree");
        return None;
    };

    let Some(mut reg) = pci.reg() else {
        warn!("node should have exactly one `reg` property, but found zero `reg`s");
        return None;
    };
    let Some(region) = reg.next() else {
        warn!("node should have exactly one `reg` property, but found zero `reg`s");
        return None;
    };
    if reg.next().is_some() {
        warn!(
            "node should have exactly one `reg` property, but found {} `reg`s",
            reg.count() + 2
        );
        return None;
    }

    let bus_range = if let Some(prop) = pci.property("bus-range") {
        if prop.value.len() != 8 || prop.value[0..3] != [0, 0, 0] || prop.value[4..7] != [0, 0, 0] {
            warn!(
                "node should have a `bus-range` property with two bytes, but found `{:?}`",
                prop.value
            );
            return None;
        }
        if prop.value[3] != 0 {
            // TODO: We don't support this case because the base address corresponds to the first
            // bus. Therefore, an offset must be applied to the bus value in `read32`/`write32`.
            warn!(
                "node with a non-zero bus start `{}` is not supported yet",
                prop.value[3]
            );
            return None;
        }
        Some(prop.value[3]..=prop.value[7])
    } else {
        // "bus-range: Optional property [..] If absent, defaults to <0 255> (i.e. all buses)."
        Some(0..=255)
    };

    // RISC-V firmware does not initialize PCI BARs; allocate them from the
    // PCIe node's memory ranges.
    init_mmio_allocator_from_fdt(&pci);

    let addr_start = region.starting_address as usize;
    let Some(addr_end) = region.size.and_then(|size| addr_start.checked_add(size)) else {
        warn!("PCI ECAM region has no valid bounded size");
        return None;
    };
    let Ok(ecam) = IoMem::acquire(addr_start..addr_end) else {
        warn!("failed to acquire PCI ECAM region");
        return None;
    };
    PCI_ECAM_CFG_SPACE.call_once(|| ecam);

    bus_range
}

pub(crate) const MSIX_DEFAULT_MSG_ADDR: u32 = 0x2400_0000;

pub(crate) fn construct_remappable_msix_address(_remapping_index: u32) -> u32 {
    unimplemented!()
}

/// Allocates an MMIO address range using the global allocator.
///
/// RISC-V platforms (QEMU virt, SiFive) do not initialize PCI BARs in
/// firmware; the kernel must assign base addresses from the PCIe node's
/// memory ranges. Mirrors the LoongArch `alloc_mmio`.
pub(crate) fn alloc_mmio(layout: Layout) -> Option<Paddr> {
    let allocator = MMIO_ALLOCATOR.get()?;
    allocator.lock().allocate(layout)
}

/// A simple MMIO allocator managing a linear region.
///
/// The starting address of a PCI memory BAR is allocated within the
/// PCIe node's memory ranges (the `ranges` property entry with PCI space
/// type 0x02, i.e. memory).
struct MmioAllocator {
    base: Paddr,
    size: Paddr,
    offset: Paddr,
}

impl MmioAllocator {
    /// Creates a new MMIO allocator with a given base and size.
    const fn new(base: Paddr, size: Paddr) -> Self {
        Self {
            base,
            size,
            offset: 0,
        }
    }

    /// Allocates a region of the given layout.
    fn allocate(&mut self, layout: Layout) -> Option<Paddr> {
        let region_end = self.base.checked_add(self.size)?;
        let current = self.base.checked_add(self.offset)?;
        let align_mask = layout.align() - 1;
        let aligned = current.checked_add(align_mask)? & !align_mask;
        let allocation_end = aligned.checked_add(layout.size())?;
        if allocation_end > region_end {
            return None;
        }
        self.offset = allocation_end.checked_sub(self.base)?;
        Some(aligned)
    }
}

static MMIO_ALLOCATOR: Once<SpinLock<MmioAllocator>> = Once::new();

/// Initializes the MMIO allocator from the PCIe node's `ranges` property.
fn init_mmio_allocator_from_fdt(node: &FdtNode) {
    let Some(ranges) = node.property("ranges") else {
        warn!("PCIe node has no 'ranges' property; PCI BARs cannot be allocated");
        return;
    };
    let Some((base, size)) = parse_mmio_range(ranges.value, node.cell_sizes()) else {
        warn!("PCIe 'ranges' has no valid 32-bit memory window");
        return;
    };
    MMIO_ALLOCATOR.call_once(|| SpinLock::new(MmioAllocator::new(base, size)));
}

fn parse_mmio_range(data: &[u8], cell_sizes: CellSizes) -> Option<(Paddr, Paddr)> {
    const PCI_ADDRESS_CELLS: usize = 3;
    const PCI_SIZE_CELLS: usize = 2;
    const PARENT_ADDRESS_CELLS: usize = 2;
    const ENTRY_CELLS: usize = PCI_ADDRESS_CELLS + PARENT_ADDRESS_CELLS + PCI_SIZE_CELLS;
    const ENTRY_SIZE: usize = ENTRY_CELLS * size_of::<u32>();

    if cell_sizes.address_cells != PCI_ADDRESS_CELLS
        || cell_sizes.size_cells != PCI_SIZE_CELLS
        || data.is_empty()
        || !data.len().is_multiple_of(ENTRY_SIZE)
    {
        return None;
    }

    let read_u32 = |entry: &[u8], offset: usize| -> Option<u32> {
        Some(u32::from_be_bytes(
            entry.get(offset..offset + 4)?.try_into().ok()?,
        ))
    };
    let read_u64 = |entry: &[u8], offset: usize| -> Option<u64> {
        Some(u64::from_be_bytes(
            entry.get(offset..offset + 8)?.try_into().ok()?,
        ))
    };

    for entry in data.chunks_exact(ENTRY_SIZE) {
        let pci_space = read_u32(entry, 0)?;
        // Bits 25:24 select I/O (01), 32-bit memory (10), or 64-bit memory (11).
        if (pci_space >> 24) & 0b11 != 0b10 {
            continue;
        }

        let pci_base = read_u64(entry, 4)?;
        let cpu_base = read_u64(entry, 12)?;
        // MemoryBar currently carries one address for both the BAR value and
        // CPU MMIO acquisition, so translated (non-identity) windows are not
        // representable yet. Reject them instead of programming a wrong BAR.
        if pci_base != cpu_base {
            continue;
        }
        let base = Paddr::try_from(cpu_base).ok()?;
        let size = Paddr::try_from(read_u64(entry, 20)?).ok()?;
        if size == 0 || base.checked_add(size).is_none() {
            return None;
        }
        return Some((base, size));
    }
    None
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    fn cells(values: &[u32]) -> alloc::vec::Vec<u8> {
        values
            .iter()
            .flat_map(|value| value.to_be_bytes())
            .collect()
    }

    #[ktest]
    fn mmio_allocator_aligns_absolute_addresses_and_checks_bounds() {
        let mut allocator = MmioAllocator::new(0x1003, 0x1ffd);
        assert_eq!(
            allocator.allocate(Layout::from_size_align(0x100, 0x1000).unwrap()),
            Some(0x2000)
        );
        assert_eq!(
            allocator.allocate(Layout::from_size_align(1, 1).unwrap()),
            Some(0x2100)
        );
        assert_eq!(
            allocator.allocate(Layout::from_size_align(0x1000, 0x1000).unwrap()),
            None
        );

        let mut overflowing = MmioAllocator::new(usize::MAX - 7, 16);
        assert_eq!(
            overflowing.allocate(Layout::from_size_align(8, 8).unwrap()),
            None
        );
    }

    #[ktest]
    fn ranges_parser_accepts_only_well_formed_32_bit_memory_windows() {
        let io_then_memory = cells(&[
            0x0100_0000,
            0,
            0,
            0,
            0x1000,
            0,
            0x100,
            0x0200_0000,
            0,
            0x4000_0000,
            0,
            0x4000_0000,
            0,
            0x1000_0000,
        ]);
        let standard_cells = CellSizes {
            address_cells: 3,
            size_cells: 2,
        };
        assert_eq!(
            parse_mmio_range(&io_then_memory, standard_cells),
            Some((0x4000_0000, 0x1000_0000))
        );
        assert_eq!(
            parse_mmio_range(&io_then_memory[..io_then_memory.len() - 1], standard_cells),
            None
        );
        assert_eq!(
            parse_mmio_range(
                &io_then_memory,
                CellSizes {
                    address_cells: 2,
                    size_cells: 2,
                }
            ),
            None
        );

        let translated = cells(&[0x0200_0000, 0, 0x4000_0000, 0, 0x5000_0000, 0, 0x1000]);
        assert_eq!(parse_mmio_range(&translated, standard_cells), None);

        let overflowing = cells(&[
            0x0200_0000,
            u32::MAX,
            0xffff_ff00,
            u32::MAX,
            0xffff_ff00,
            0,
            0x200,
        ]);
        assert_eq!(parse_mmio_range(&overflowing, standard_cells), None);
    }
}
