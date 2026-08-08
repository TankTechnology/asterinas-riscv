// SPDX-License-Identifier: MPL-2.0

//! PCI xHCI host adapter for the USB boot keyboard.
//!
//! The Megrez path selects a DWC3 MMIO host via `/chosen/asterinas,usb-host`.
//! On QEMU virt (and x86 machines) the xHCI controller is a PCI device
//! (class 0x0C, subclass 0x03, prog-if 0x30). This driver enumerates it
//! through the PCI bus, maps BAR0 as the xHCI MMIO window, reads the
//! conventional interrupt line, and drives the same
//! [`run_keyboard_interrupt_driven`] path used by the MMIO host.

use alloc::{sync::Arc, vec::Vec};

use aster_pci::{
    PCI_BUS,
    PciDeviceId,
    bus::{PciDevice, PciDriver},
    cfg_space::PciCommonCfgOffset,
    common_device::PciCommonDevice,
};
use ostd::{
    arch::{boot::DEVICE_TREE, irq::InterruptSourceInFdt},
    bus::BusProbeError,
    mm::dma::DmaWindow,
    sync::SpinLock,
};

const XHCI_DEVICE_CLASS: u8 = 0x0C;
const XHCI_DEVICE_SUBCLASS: u8 = 0x03;
const XHCI_DEVICE_PROG_IF: u8 = 0x30;
const BAR0_INDEX: u8 = 0;
const PCI_INTERRUPT_LINE: u16 = PciCommonCfgOffset::InterruptLine as u16;

#[derive(Debug)]
struct PciXhciDevice {
    device_id: PciDeviceId,
}

impl PciXhciDevice {
    fn new(device_id: PciDeviceId) -> Self {
        Self { device_id }
    }
}

impl PciDevice for PciXhciDevice {
    fn device_id(&self) -> PciDeviceId {
        self.device_id
    }
}

#[derive(Debug)]
struct PciXhciDriver {
    devices: SpinLock<Vec<Arc<dyn PciDevice>>>,
}

impl PciXhciDriver {
    fn new() -> Self {
        Self {
            devices: SpinLock::new(Vec::new()),
        }
    }
}

impl PciDriver for PciXhciDriver {
    fn probe(
        &self,
        mut device: PciCommonDevice,
    ) -> Result<Arc<dyn PciDevice>, (BusProbeError, PciCommonDevice)> {
        if device.device_id().class != XHCI_DEVICE_CLASS
            || device.device_id().subclass != XHCI_DEVICE_SUBCLASS
            || device.device_id().prog_if != XHCI_DEVICE_PROG_IF
        {
            return Err((BusProbeError::DeviceNotMatch, device));
        }

        let Some(bar) = device.bar_manager_mut().bar_mut(BAR0_INDEX) else {
            ostd::error!("xHCI BAR0 is missing");
            return Err((BusProbeError::ConfigurationSpaceError, device));
        };
        let mmio = match bar.acquire() {
            Ok(aster_pci::cfg_space::BarAccess::Memory(mmio)) => mmio,
            Ok(_) => {
                ostd::error!("xHCI BAR0 is not a memory BAR");
                return Err((BusProbeError::ConfigurationSpaceError, device));
            }
            Err(_) => {
                ostd::error!("xHCI BAR0 mapping failed");
                return Err((BusProbeError::ConfigurationSpaceError, device));
            }
        };

        // Conventional PCI INTx interrupt line. On QEMU virt the firmware
        // programs this to the PLIC line for the device's interrupt pin.
        let interrupt_line = device.location().read8(PCI_INTERRUPT_LINE);
        if interrupt_line == 0 {
            ostd::warn!("xHCI PCI interrupt line is zero; keyboard will not be interrupt-driven");
        } else {
            ostd::info!("xHCI PCI interrupt line: {}", interrupt_line);
        }

        let device_id = *device.device_id();
        let dma_window = DmaWindow::new(0, 0, usize::MAX).expect("identity DMA window");
        let interrupt_source = resolve_pci_interrupt(&device, interrupt_line);
        PCI_HOST_CONFIG.call_once(|| PciHostConfig {
            mmio,
            dma_window,
            interrupt_source,
        });
        ostd::info!("PCI xHCI host saved: device {:04x}:{:04x}", device_id.vendor_id, device_id.device_id);

        self.devices.lock().push(Arc::new(PciXhciDevice::new(device_id)));
        Ok(Arc::new(PciXhciDevice::new(device_id)))
    }
}

/// Resolves the PCI INTx interrupt through the DTB `interrupt-map`.
///
/// The map encodes `(pci-address pin phandle irq)` tuples; the xHCI device
/// pin (read from configuration space) selects the PLIC phandle and IRQ
/// number. Falls back to the conventional `interrupt_line` when the map or
/// pin is unavailable.
fn resolve_pci_interrupt(device: &PciCommonDevice, interrupt_line: u8) -> InterruptSourceInFdt {
    let interrupt_pin = device.location().read8(0x3D); // InterruptPin
    if interrupt_pin == 0 {
        ostd::warn!("xHCI has no interrupt pin; using interrupt_line");
        return InterruptSourceInFdt {
            interrupt_parent: u32::MAX,
            interrupt: interrupt_line as u32,
        };
    }

    let Some(pci_node) = DEVICE_TREE
        .get()
        .unwrap()
        .find_compatible(&["pci-host-ecam-generic"])
    else {
        ostd::warn!("no PCI host node in DTB; using interrupt_line");
        return InterruptSourceInFdt {
            interrupt_parent: u32::MAX,
            interrupt: interrupt_line as u32,
        };
    };
    let Some(map) = pci_node.property("interrupt-map") else {
        ostd::warn!("no PCI interrupt-map in DTB; using interrupt_line");
        return InterruptSourceInFdt {
            interrupt_parent: u32::MAX,
            interrupt: interrupt_line as u32,
        };
    };

    // The map is a flattened list of
    //   (3-cell pci-address, pin, interrupt-parent phandle, interrupt-cell)
    // entries. QEMU virt uses one-cell interrupt-parents; we match the
    // device's pin and take the first matching entry. Cells are big-endian
    // u32 values in the raw property bytes.
    let value = map.value;
    let mut offset = 0;
    while offset + 24 <= value.len() {
        let read_u32 = |at: usize| {
            u32::from_be_bytes(value[at..at + 4].try_into().unwrap())
        };
        let _addr0 = read_u32(offset);
        let _addr1 = read_u32(offset + 4);
        let _addr2 = read_u32(offset + 8);
        let pin = read_u32(offset + 12);
        let parent = read_u32(offset + 16);
        let irq = read_u32(offset + 20);
        if pin == interrupt_pin as u32 {
            ostd::info!("PCI xHCI interrupt-map: parent={:#x} irq={}", parent, irq);
            return InterruptSourceInFdt {
                interrupt_parent: parent,
                interrupt: irq,
            };
        }
        offset += 24;
    }

    ostd::warn!("no interrupt-map entry for pin {}; using interrupt_line", interrupt_pin);
    InterruptSourceInFdt {
        interrupt_parent: u32::MAX,
        interrupt: interrupt_line as u32,
    }
}

/// PCI-discovered xHCI host, saved by the driver and consumed by the
/// polling thread.
pub(crate) struct PciHostConfig {
    pub(crate) mmio: ostd::io::IoMem,
    pub(crate) dma_window: DmaWindow,
    pub(crate) interrupt_source: InterruptSourceInFdt,
}

static PCI_HOST_CONFIG: spin::Once<PciHostConfig> = spin::Once::new();

/// Returns the PCI-discovered xHCI host config, if any.
pub(crate) fn pci_host_config() -> Option<&'static PciHostConfig> {
    PCI_HOST_CONFIG.get()
}

/// Registers the PCI xHCI driver with the PCI bus.
pub(crate) fn init() {
    let driver = Arc::new(PciXhciDriver::new());
    PCI_BUS.lock().register_driver(driver);
    ostd::info!("PCI xHCI driver registered");
}
