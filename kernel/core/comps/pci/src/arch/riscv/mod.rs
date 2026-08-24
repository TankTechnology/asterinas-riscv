// SPDX-License-Identifier: MPL-2.0

//! PCI bus access

use core::ops::RangeInclusive;

use fdt::node::{CellSizes, FdtNode};
use fdt_util::AcquireIoMems;
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

const PCI_ECAM_BUS_SHIFT: u32 = 20;
const PCI_ECAM_DEVICE_SHIFT: u32 = 15;
const PCI_ECAM_FUNCTION_SHIFT: u32 = 12;

/// Encodes the bus, device, and function into an address offset in the PCI MMIO region.
fn encode_as_address_offset(location: &PciDeviceLocation) -> u32 {
    // We only support ECAM here for RISC-V platforms. Offsets are from
    // <https://www.kernel.org/doc/Documentation/devicetree/bindings/pci/host-generic-pci.txt>.
    ((location.bus as u32) << PCI_ECAM_BUS_SHIFT)
        | ((location.device as u32) << PCI_ECAM_DEVICE_SHIFT)
        | ((location.function as u32) << PCI_ECAM_FUNCTION_SHIFT)
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

    // RISC-V firmware does not initialize PCI BARs; allocate them from the
    // PCIe node's memory ranges.
    init_mmio_allocator_from_fdt(&pci);

    let io_size = (*bus_range.end() as usize + 1) << PCI_ECAM_BUS_SHIFT;
    let [io_mem] = pci.acquire_io_mems([io_size])?;

    PCI_ECAM_CFG_SPACE.call_once(|| io_mem);

    PCI_BUS_RANGE.call_once(|| bus_range.clone());
    Some(bus_range)
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
pub(crate) fn alloc_mmio(layout: core::alloc::Layout) -> Option<ostd::mm::Paddr> {
    let allocator = MMIO_ALLOCATOR.get()?;
    allocator.lock().allocate(layout)
}

/// A simple MMIO allocator managing a linear region.
///
/// The starting address of a PCI memory BAR is allocated within the
/// PCIe node's memory ranges (the `ranges` property entry with PCI space
/// type 0x02, i.e. memory).
struct MmioAllocator {
    base: ostd::mm::Paddr,
    size: ostd::mm::Paddr,
    offset: ostd::mm::Paddr,
}

impl MmioAllocator {
    /// Creates a new MMIO allocator with a given base and size.
    fn new(base: ostd::mm::Paddr, size: ostd::mm::Paddr) -> Self {
        Self {
            base,
            size,
            offset: 0,
        }
    }

    /// Allocates a region of the given layout.
    fn allocate(&mut self, layout: core::alloc::Layout) -> Option<ostd::mm::Paddr> {
        let aligned_offset = self.offset.next_multiple_of(layout.align());
        if aligned_offset.checked_add(layout.size())? > self.size {
            return None;
        }
        let result = self.base + aligned_offset;
        self.offset = aligned_offset + layout.size();
        Some(result)
    }
}

static MMIO_ALLOCATOR: Once<ostd::sync::SpinLock<MmioAllocator>> = Once::new();

/// Initializes the MMIO allocator from the PCIe node's `ranges` property.
fn init_mmio_allocator_from_fdt(node: &fdt::node::FdtNode) {
    let Some(ranges) = node.property("ranges") else {
        warn!("PCIe node has no 'ranges' property; PCI BARs cannot be allocated");
        return;
    };
    let data = ranges.value;

    let entry_size = 7 * 4; // Each entry is 7 x u32 = 28 bytes
    let mut i = 0;
    while i + entry_size <= data.len() {
        let pci_space = u32::from_be_bytes(data[i..i + 4].try_into().unwrap());
        let _pci_addr = u64::from_be_bytes(data[i + 4..i + 12].try_into().unwrap());
        let cpu_addr = u64::from_be_bytes(data[i + 12..i + 20].try_into().unwrap());
        let size = u64::from_be_bytes(data[i + 20..i + 28].try_into().unwrap());

        // Only initialize with memory-type region.
        if (pci_space >> 24) == 0x2 {
            MMIO_ALLOCATOR.call_once(|| {
                ostd::sync::SpinLock::new(MmioAllocator::new(cpu_addr as usize, size as usize))
            });
            break;
        }
        i += entry_size;
    }
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

/// Resolves the DMA window and INTx route for a PCI function on RISC-V.
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
            arguments: [route.interrupt],
        },
    })
}
