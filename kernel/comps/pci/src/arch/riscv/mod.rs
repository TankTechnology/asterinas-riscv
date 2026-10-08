// SPDX-License-Identifier: MPL-2.0

//! PCI bus access

use core::{alloc::Layout, ops::RangeInclusive};

use fdt::node::{CellSizes, FdtNode};
use ostd::{
    Error,
    arch::{boot::DEVICE_TREE, irq::InterruptSourceInFdt},
    io::IoMem,
    mm::{Paddr, VmIoOnce, dma::DmaWindow},
    sync::SpinLock,
    warn,
};
use spin::Once;

use crate::{
    PciDeviceLocation,
    cfg_space::{PciBridgeCfgOffset, PciCommonCfgOffset, PciGeneralDeviceCfgOffset},
};

mod intx;

pub use intx::RiscvPciResourceError;

use self::intx::{
    HostDmaFields, ParentInterruptSpec, PciIntxEndpoint, require_exclusive_route,
    resolve_intx_cells, validate_dma_contract,
};

static PCI_ECAM_CFG_SPACE: Once<IoMem> = Once::new();
static PCI_BUS_RANGE: Once<RangeInclusive<u8>> = Once::new();

#[derive(Clone, Copy, Debug)]
pub struct RiscvPciHostResources {
    pub dma_window: DmaWindow,
    pub interrupt_source: InterruptSourceInFdt,
}

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
    let device_tree = DEVICE_TREE.get().unwrap();
    let root = device_tree.find_node("/").unwrap();
    let Some((pci, parent_address_cells)) = find_pci_host(root) else {
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
        prop.value[3]..=prop.value[7]
    } else {
        // "bus-range: Optional property [..] If absent, defaults to <0 255> (i.e. all buses)."
        0..=255
    };

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

    // A single bump allocator cannot safely combine firmware-assigned and
    // unassigned BARs without first reserving every assigned interval. Until
    // interval reservation is implemented, enable allocation only for the
    // all-unassigned case used by QEMU virt.
    if pci_has_assigned_memory_bars(bus_range.clone()) {
        warn!("PCI memory BARs are already assigned; leaving zero BARs unallocated");
    } else {
        init_mmio_allocator_from_fdt(&pci, parent_address_cells);
    }

    PCI_BUS_RANGE.call_once(|| bus_range.clone());
    Some(bus_range)
}

fn pci_host_node() -> Result<FdtNode<'static, 'static>, RiscvPciResourceError> {
    let device_tree = DEVICE_TREE.get().unwrap();
    let mut hosts = device_tree.all_nodes().filter(|node| {
        node.compatible()
            .is_some_and(|compatible| compatible.all().any(|name| name == "pci-host-ecam-generic"))
    });
    let host = hosts.next().ok_or(RiscvPciResourceError::MissingHost)?;
    if hosts.next().is_some() {
        return Err(RiscvPciResourceError::MissingHost);
    }
    Ok(host)
}

fn resolve_host_intx(
    node: FdtNode<'_, '_>,
    location: PciDeviceLocation,
    pin: u8,
) -> Result<intx::PciIntxRoute, RiscvPciResourceError> {
    let mask = node
        .property("interrupt-map-mask")
        .ok_or(RiscvPciResourceError::InvalidIntxMap)?;
    let map = node
        .property("interrupt-map")
        .ok_or(RiscvPciResourceError::InvalidIntxMap)?;
    let device_tree = DEVICE_TREE.get().unwrap();
    resolve_intx_cells(location, pin, mask.value, map.value, |phandle| {
        let parent = device_tree.find_phandle(phandle)?;
        Some(ParentInterruptSpec {
            address_cells: parent.cell_sizes().address_cells,
            interrupt_cells: parent.interrupt_cells()?,
        })
    })
}

fn present_intx_endpoints() -> Result<alloc::vec::Vec<PciIntxEndpoint>, RiscvPciResourceError> {
    let buses = PCI_BUS_RANGE
        .get()
        .ok_or(RiscvPciResourceError::MissingHost)?;
    let mut endpoints = alloc::vec::Vec::new();
    for bus in buses.clone() {
        for device in PciDeviceLocation::MIN_DEVICE..=PciDeviceLocation::MAX_DEVICE {
            for function in PciDeviceLocation::MIN_FUNCTION..=PciDeviceLocation::MAX_FUNCTION {
                let location = PciDeviceLocation {
                    bus,
                    device,
                    function,
                };
                if location.read16(PciCommonCfgOffset::VendorId as u16) == u16::MAX {
                    continue;
                }
                let pin = location.read8(PciCommonCfgOffset::InterruptPin as u16);
                if pin == 0 {
                    continue;
                }
                if pin > 4 {
                    return Err(RiscvPciResourceError::InvalidIntxMap);
                }
                endpoints.push(PciIntxEndpoint { location, pin });
            }
        }
    }
    Ok(endpoints)
}

pub fn riscv_host_resources(
    location: PciDeviceLocation,
) -> Result<RiscvPciHostResources, RiscvPciResourceError> {
    let host = pci_host_node()?;
    let dma_window = validate_dma_contract(HostDmaFields {
        dma_coherent: host.property("dma-coherent").is_some(),
        dma_ranges: host.property("dma-ranges").map(|property| property.value),
        has_iommu: host.property("iommus").is_some() || host.property("iommu-map").is_some(),
    })?;
    let pin = location.read8(PciCommonCfgOffset::InterruptPin as u16);
    let target = PciIntxEndpoint { location, pin };
    let route = require_exclusive_route(target, present_intx_endpoints()?, |location, pin| {
        resolve_host_intx(host, location, pin)
    })?;
    Ok(RiscvPciHostResources {
        dma_window,
        interrupt_source: InterruptSourceInFdt {
            interrupt_parent: route.interrupt_parent,
            interrupt: route.interrupt,
        },
    })
}

fn find_pci_host<'b, 'a: 'b>(parent: FdtNode<'b, 'a>) -> Option<(FdtNode<'b, 'a>, usize)> {
    let parent_address_cells = parent.cell_sizes().address_cells;
    for child in parent.children() {
        if child
            .compatible()
            .is_some_and(|compatible| compatible.all().any(|name| name == "pci-host-ecam-generic"))
        {
            return Some((child, parent_address_cells));
        }
        if let Some(found) = find_pci_host(child) {
            return Some(found);
        }
    }
    None
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
fn init_mmio_allocator_from_fdt(node: &FdtNode, parent_address_cells: usize) {
    let Some(ranges) = node.property("ranges") else {
        warn!("PCIe node has no 'ranges' property; PCI BARs cannot be allocated");
        return;
    };
    let Some((base, size)) =
        parse_mmio_range(ranges.value, node.cell_sizes(), parent_address_cells)
    else {
        warn!("PCIe 'ranges' has no valid 32-bit memory window");
        return;
    };
    MMIO_ALLOCATOR.call_once(|| SpinLock::new(MmioAllocator::new(base, size)));
}

fn parse_mmio_range(
    data: &[u8],
    cell_sizes: CellSizes,
    parent_address_cells: usize,
) -> Option<(Paddr, Paddr)> {
    const PCI_ADDRESS_CELLS: usize = 3;
    const PCI_SIZE_CELLS: usize = 2;
    let entry_cells = PCI_ADDRESS_CELLS
        .checked_add(parent_address_cells)?
        .checked_add(PCI_SIZE_CELLS)?;
    let entry_size = entry_cells.checked_mul(size_of::<u32>())?;

    if cell_sizes.address_cells != PCI_ADDRESS_CELLS
        || cell_sizes.size_cells != PCI_SIZE_CELLS
        || data.is_empty()
        || !matches!(parent_address_cells, 1 | 2)
        || !data.len().is_multiple_of(entry_size)
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

    for entry in data.chunks_exact(entry_size) {
        let pci_space = read_u32(entry, 0)?;
        // Bits 25:24 select I/O (01), 32-bit memory (10), or 64-bit memory (11).
        // Bit 30 marks a prefetchable window. The single allocator is used for
        // arbitrary memory BARs, so it must use a non-prefetchable window.
        if (pci_space >> 24) & 0b11 != 0b10 || pci_space & (1 << 30) != 0 {
            continue;
        }

        let pci_base = read_u64(entry, 4)?;
        let cpu_offset = PCI_ADDRESS_CELLS * size_of::<u32>();
        let cpu_base = match parent_address_cells {
            1 => read_u32(entry, cpu_offset)? as u64,
            2 => read_u64(entry, cpu_offset)?,
            _ => unreachable!(),
        };
        // MemoryBar currently carries one address for both the BAR value and
        // CPU MMIO acquisition, so translated (non-identity) windows are not
        // representable yet. Reject them instead of programming a wrong BAR.
        if pci_base != cpu_base {
            continue;
        }
        let size_offset = cpu_offset.checked_add(parent_address_cells * size_of::<u32>())?;
        let size_u64 = read_u64(entry, size_offset)?;
        let end = cpu_base.checked_add(size_u64)?;
        if size_u64 == 0 || end > 1u64 << 32 {
            return None;
        }
        let base = Paddr::try_from(cpu_base).ok()?;
        let size = Paddr::try_from(size_u64).ok()?;
        return Some((base, size));
    }
    None
}

fn pci_has_assigned_memory_bars(bus_range: RangeInclusive<u8>) -> bool {
    for bus in bus_range {
        for device in PciDeviceLocation::MIN_DEVICE..=PciDeviceLocation::MAX_DEVICE {
            let function0 = PciDeviceLocation {
                bus,
                device,
                function: PciDeviceLocation::MIN_FUNCTION,
            };
            if function0.read16(PciCommonCfgOffset::VendorId as u16) == u16::MAX {
                continue;
            }
            if location_has_assigned_memory_bar(function0) {
                return true;
            }
            let header_type = function0.read8(PciCommonCfgOffset::HeaderType as u16);
            if header_type & 0x80 == 0 {
                continue;
            }
            for function in (PciDeviceLocation::MIN_FUNCTION + 1)..=PciDeviceLocation::MAX_FUNCTION
            {
                let location = PciDeviceLocation {
                    bus,
                    device,
                    function,
                };
                if location.read16(PciCommonCfgOffset::VendorId as u16) != u16::MAX
                    && location_has_assigned_memory_bar(location)
                {
                    return true;
                }
            }
        }
    }
    false
}

fn location_has_assigned_memory_bar(location: PciDeviceLocation) -> bool {
    let header_type = location.read8(PciCommonCfgOffset::HeaderType as u16) & 0x7f;
    let (count, expansion_rom_offset) = match header_type {
        0 => (6, Some(PciGeneralDeviceCfgOffset::XromBar as u16)),
        1 => (2, Some(PciBridgeCfgOffset::ExpansionRomBaseAddress as u16)),
        _ => (0, None),
    };
    let mut raw_bars = [0; 6];
    for (index, raw) in raw_bars[..count].iter_mut().enumerate() {
        *raw = location.read32(PciGeneralDeviceCfgOffset::Bar0 as u16 + index as u16 * 4);
    }
    let expansion_rom = expansion_rom_offset
        .map(|offset| location.read32(offset))
        .unwrap_or(0);
    has_assigned_memory_bar(&raw_bars[..count], expansion_rom)
}

fn has_assigned_memory_bar(raw_bars: &[u32], expansion_rom: u32) -> bool {
    // Expansion ROM BAR address bits are 31:11; bit 0 only controls decoding.
    if expansion_rom & !0x7ff != 0 {
        return true;
    }
    let mut index = 0;
    while index < raw_bars.len() {
        let raw = raw_bars[index];
        if raw & 1 != 0 {
            index += 1;
            continue;
        }
        let is_64_bit = (raw >> 1) & 3 == 0b10;
        if raw & !0xf != 0
            || (is_64_bit && raw_bars.get(index + 1).is_some_and(|upper| *upper != 0))
        {
            return true;
        }
        index += if is_64_bit { 2 } else { 1 };
    }
    false
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
            parse_mmio_range(&io_then_memory, standard_cells, 2),
            Some((0x4000_0000, 0x1000_0000))
        );
        assert_eq!(
            parse_mmio_range(
                &io_then_memory[..io_then_memory.len() - 1],
                standard_cells,
                2,
            ),
            None
        );
        assert_eq!(
            parse_mmio_range(
                &io_then_memory,
                CellSizes {
                    address_cells: 2,
                    size_cells: 2,
                },
                2,
            ),
            None
        );

        let translated = cells(&[0x0200_0000, 0, 0x4000_0000, 0, 0x5000_0000, 0, 0x1000]);
        assert_eq!(parse_mmio_range(&translated, standard_cells, 2), None);

        let one_cell_parent = cells(&[0x0200_0000, 0, 0x4000_0000, 0x4000_0000, 0, 0x1000]);
        assert_eq!(
            parse_mmio_range(&one_cell_parent, standard_cells, 1),
            Some((0x4000_0000, 0x1000))
        );
        assert_eq!(parse_mmio_range(&one_cell_parent, standard_cells, 3), None);

        let prefetchable_then_memory = cells(&[
            0x4200_0000,
            0,
            0x3000_0000,
            0,
            0x3000_0000,
            0,
            0x1000,
            0x0200_0000,
            0,
            0x4000_0000,
            0,
            0x4000_0000,
            0,
            0x1000,
        ]);
        assert_eq!(
            parse_mmio_range(&prefetchable_then_memory, standard_cells, 2),
            Some((0x4000_0000, 0x1000))
        );

        let ending_at_4g = cells(&[0x0200_0000, 0, 0xffff_f000, 0, 0xffff_f000, 0, 0x1000]);
        assert_eq!(
            parse_mmio_range(&ending_at_4g, standard_cells, 2),
            Some((0xffff_f000, 0x1000))
        );

        let overflowing = cells(&[
            0x0200_0000,
            u32::MAX,
            0xffff_ff00,
            u32::MAX,
            0xffff_ff00,
            0,
            0x200,
        ]);
        assert_eq!(parse_mmio_range(&overflowing, standard_cells, 2), None);

        let above_4g = cells(&[0x0200_0000, 1, 0, 1, 0, 0, 0x1000]);
        assert_eq!(parse_mmio_range(&above_4g, standard_cells, 2), None);
    }

    #[ktest]
    fn assigned_memory_bar_detection_is_conservative() {
        assert!(!has_assigned_memory_bar(&[0, 0, 0, 0, 0, 0], 0));
        assert!(!has_assigned_memory_bar(&[0x1, 0, 0, 0, 0, 0], 0));
        assert!(has_assigned_memory_bar(&[0x4000_0000, 0, 0, 0, 0, 0], 0));
        assert!(has_assigned_memory_bar(&[0x4, 1, 0, 0, 0, 0], 0));
        assert!(has_assigned_memory_bar(&[0, 0, 0, 0, 0, 0], 0x5000_0001));
    }
}
