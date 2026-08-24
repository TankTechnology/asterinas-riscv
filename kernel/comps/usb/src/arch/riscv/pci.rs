// SPDX-License-Identifier: MPL-2.0

use alloc::sync::Arc;
use core::fmt;

use aster_pci::{
    PCI_BUS, PciDeviceId, PciDeviceLocation,
    bus::{PciDevice, PciDriver},
    cfg_space::{BarAccess, Command, PciCommonCfgOffset},
    common_device::PciCommonDevice,
    riscv_host_resources,
};
use ostd::{
    arch::irq::InterruptSourceInFdt, bus::BusProbeError, io::IoMem, mm::dma::DmaWindow,
    sync::SpinLock,
};

const USB_CLASS: u8 = 0x0c;
const USB_SUBCLASS: u8 = 0x03;
const XHCI_PROG_IF: u8 = 0x30;

#[derive(Debug)]
pub(super) struct PciHostConfig {
    pub(super) location: PciDeviceLocation,
    pub(super) mmio: IoMem,
    pub(super) dma_window: DmaWindow,
    pub(super) interrupt_source: InterruptSourceInFdt,
}

static PCI_HOST: SpinLock<Option<PciHostConfig>> = SpinLock::new(None);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PciAdapterError {
    AlreadyClaimed,
    UnsafeResources,
    MissingBar,
    IoBar,
}

fn is_xhci(id: PciDeviceId) -> bool {
    (id.class, id.subclass, id.prog_if) == (USB_CLASS, USB_SUBCLASS, XHCI_PROG_IF)
}

fn store_host<T>(slot: &SpinLock<Option<T>>, host: T) -> Result<(), PciAdapterError> {
    let mut slot = slot.lock();
    if slot.is_some() {
        return Err(PciAdapterError::AlreadyClaimed);
    }
    *slot = Some(host);
    Ok(())
}

fn take_host<T>(slot: &SpinLock<Option<T>>) -> Option<T> {
    slot.lock().take()
}

pub(super) fn take_selected_host() -> Option<PciHostConfig> {
    take_host(&PCI_HOST)
}

pub(super) fn set_intx_enabled(location: PciDeviceLocation, enabled: bool) {
    let command = Command::from_bits_truncate(location.read16(PciCommonCfgOffset::Command as u16));
    let command = if enabled {
        command - Command::INTERRUPT_DISABLE
    } else {
        command | Command::INTERRUPT_DISABLE
    };
    location.write16(PciCommonCfgOffset::Command as u16, command.bits());
}

struct PciXhciDevice {
    location: PciDeviceLocation,
    id: PciDeviceId,
}

impl fmt::Debug for PciXhciDevice {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PciXhciDevice")
            .field("location", &self.location)
            .field("id", &self.id)
            .finish()
    }
}

impl PciDevice for PciXhciDevice {
    fn device_id(&self) -> PciDeviceId {
        self.id
    }
}

#[derive(Debug)]
struct PciXhciDriver;

impl PciXhciDriver {
    fn reject(device: PciCommonDevice, error: PciAdapterError) -> (BusProbeError, PciCommonDevice) {
        device.write_command(Command::INTERRUPT_DISABLE);
        ostd::warn!("rejected PCI xHCI controller: {:?}", error);
        (BusProbeError::ConfigurationSpaceError, device)
    }
}

impl PciDriver for PciXhciDriver {
    fn probe(
        &self,
        mut device: PciCommonDevice,
    ) -> Result<Arc<dyn PciDevice>, (BusProbeError, PciCommonDevice)> {
        let id = *device.device_id();
        if !is_xhci(id) {
            return Err((BusProbeError::DeviceNotMatch, device));
        }

        let location = *device.location();
        device.write_command(Command::INTERRUPT_DISABLE);
        let resources = match riscv_host_resources(location) {
            Ok(resources) => resources,
            Err(_) => return Err(Self::reject(device, PciAdapterError::UnsafeResources)),
        };
        let Some(bar0) = device.bar_manager_mut().bar_mut(0) else {
            return Err(Self::reject(device, PciAdapterError::MissingBar));
        };
        let mmio = match bar0.acquire_exclusive() {
            Ok(BarAccess::Memory(mmio)) => mmio,
            Ok(BarAccess::Io) => return Err(Self::reject(device, PciAdapterError::IoBar)),
            Err(_) => return Err(Self::reject(device, PciAdapterError::UnsafeResources)),
        };
        let host = PciHostConfig {
            location,
            mmio,
            dma_window: resources.dma_window,
            interrupt_source: resources.interrupt_source,
        };
        if let Err(error) = store_host(&PCI_HOST, host) {
            return Err(Self::reject(device, error));
        }

        device.write_command(
            Command::BUS_MASTER | Command::MEMORY_SPACE | Command::INTERRUPT_DISABLE,
        );
        ostd::info!(
            "PCI xHCI selected: 0000:{:02x}:{:02x}.{} {:04x}:{:04x} irq-parent={} irq={}",
            location.bus,
            location.device,
            location.function,
            id.vendor_id,
            id.device_id,
            resources.interrupt_source.interrupt_parent,
            resources.interrupt_source.interrupt,
        );
        Ok(Arc::new(PciXhciDevice { location, id }))
    }
}

pub(super) fn init() {
    PCI_BUS.lock().register_driver(Arc::new(PciXhciDriver));
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    const XHCI_ID: PciDeviceId = PciDeviceId {
        vendor_id: 0x1b36,
        device_id: 0x000d,
        revision_id: 0,
        prog_if: XHCI_PROG_IF,
        subclass: USB_SUBCLASS,
        class: USB_CLASS,
    };

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    struct FakeHost {
        location: PciDeviceLocation,
    }

    fn bdf(device: u8) -> PciDeviceLocation {
        PciDeviceLocation {
            bus: 0,
            device,
            function: 0,
        }
    }

    #[ktest]
    fn riscv_pci_xhci_matches_only_xhci_programming_interface() {
        assert!(is_xhci(XHCI_ID));
        assert!(!is_xhci(PciDeviceId {
            prog_if: 0x20,
            ..XHCI_ID
        }));
        assert!(!is_xhci(PciDeviceId {
            subclass: 0x02,
            ..XHCI_ID
        }));
        assert!(!is_xhci(PciDeviceId {
            class: 0x0b,
            ..XHCI_ID
        }));
    }

    #[ktest]
    fn riscv_pci_xhci_first_valid_host_owns_the_slot() {
        let host = SpinLock::new(None);
        let first = FakeHost { location: bdf(1) };
        let second = FakeHost { location: bdf(2) };

        assert_eq!(store_host(&host, first), Ok(()));
        assert_eq!(
            store_host(&host, second),
            Err(PciAdapterError::AlreadyClaimed)
        );
        assert_eq!(take_host(&host), Some(first));
        assert_eq!(take_host(&host), None);
    }
}
