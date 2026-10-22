// SPDX-License-Identifier: MPL-2.0

//! A minimal xHCI host-controller driver.
//!
//! The driver brings the controller up, watches the root-hub ports and
//! enumerates what is plugged in: a port reset, `Enable Slot`, `Address
//! Device`, and a `GET_DESCRIPTOR` control transfer over EP0. The result is
//! visible twice — `device 1-1: idVendor=… idProduct=…` in the log and a
//! `/sys/bus/usb/devices/1-1/` directory with `idVendor`/`idProduct`.
//!
//! Configuration parsing, interfaces and the mass-storage class are the next
//! increment; nothing here drives a device past the addressed state.

#![no_std]
#![deny(unsafe_code)]

extern crate alloc;

// Set this crate's log prefix for `ostd::log`.
macro_rules! __log_prefix {
    () => {
        "xhci: "
    };
}

mod sysfs;
mod usb;

use alloc::{sync::Arc, vec, vec::Vec};
use core::time::Duration;

use aster_core::{prelude::*, spawn_kernel_thread, wait_until_or_timeout};
use aster_pci::{
    PCI_BUS,
    bus::{PciDevice, PciDriver},
    cfg_space::BarAccess,
    common_device::PciCommonDevice,
};
use component::{ComponentInitError, init_component};
use ostd::{
    bus::BusProbeError,
    info,
    mm::{HasDaddr, VmIo, dma::DmaCoherent},
    sync::SpinLock,
    warn,
};
use usb::{Rings, SlotState, new_slot_table};

/// Red Hat's `qemu-xhci`.
const QEMU_XHCI: (u16, u16) = (0x1b36, 0x000d);
/// NEC/Renesas xHCI controllers.
const NEC_VENDOR_ID: u16 = 0x1033;
const NEC_XHCI_DEVICE_ID: u16 = 0x0194;

// Controller capability registers (BAR0 + 0).
const REG_CAPLENGTH: usize = 0x00;
const REG_HCSPARAMS1: usize = 0x04;
const REG_HCSPARAMS2: usize = 0x08;
const REG_HCCPARAMS1: usize = 0x10;
const REG_DBOFF: usize = 0x14;
const REG_RTSOFF: usize = 0x18;

// Operational registers (BAR0 + CAPLENGTH).
const REG_USBCMD: usize = 0x00;
const REG_USBSTS: usize = 0x04;
const REG_CRCR: usize = 0x18;
const REG_DCBAAP: usize = 0x30;
const REG_CONFIG: usize = 0x38;
const REG_PORTSC_BASE: usize = 0x400;
const REG_PORTSC_STRIDE: usize = 0x10;

// Interrupter 0 registers (BAR0 + RTSOFF + 0x20).
const REG_ERSTSZ: usize = 0x28;
const REG_ERSTBA: usize = 0x30;
pub(crate) const REG_ERDP: usize = 0x38;

const USBCMD_RUN: u32 = 1 << 0;
const USBCMD_HCRST: u32 = 1 << 1;
const USBSTS_HCH: u32 = 1 << 0;
const USBSTS_CNR: u32 = 1 << 11;
/// Maximum slots enabled by `CONFIG`.
const CONFIG_MAX_SLOTS: u32 = 0xFF;
/// `PORTSC`: current connect status.
pub(crate) const PORTSC_CCS: u32 = 1 << 0;
/// `PORTSC`: port enabled/disabled. Write-one-to-disable.
pub(crate) const PORTSC_PED: u32 = 1 << 1;
/// `PORTSC`: port reset.
pub(crate) const PORTSC_PR: u32 = 1 << 4;
/// `PORTSC`: port reset change.
pub(crate) const PORTSC_PRC: u32 = 1 << 21;
/// `PORTSC` write-one-to-clear change bits.
pub(crate) const PORTSC_CHANGE_BITS: u32 = 0xFE_00_00;
/// Event ring segment size (TRBs).
const EVENT_RING_TRBS: u32 = 256;
/// Device-context slots in the DCBAA page.
pub(crate) const SLOT_COUNT: usize = 64;

#[derive(Debug)]
struct XhciDriver;

impl PciDriver for XhciDriver {
    fn probe(
        &self,
        mut device: PciCommonDevice,
    ) -> Result<Arc<dyn PciDevice>, (BusProbeError, PciCommonDevice)> {
        let id = *device.device_id();
        let matches = (id.vendor_id, id.device_id) == QEMU_XHCI
            || (id.vendor_id == NEC_VENDOR_ID && id.device_id == NEC_XHCI_DEVICE_ID);
        if !matches {
            return Err((BusProbeError::DeviceNotMatch, device));
        }

        let bar = match device.bar_manager_mut().bar_mut(0).map(|bar| bar.acquire()) {
            Some(Ok(bar)) => bar,
            _ => return Err((BusProbeError::ConfigurationSpaceError, device)),
        };

        match XhciController::new(bar, id) {
            Ok(controller) => {
                let controller = Arc::new(controller);
                start_port_watch(controller.clone());
                Ok(controller)
            }
            Err(error) => {
                warn!("controller bring-up failed: {:?}", error);
                Err((BusProbeError::ConfigurationSpaceError, device))
            }
        }
    }
}

impl PciDevice for XhciController {
    fn device_id(&self) -> aster_pci::PciDeviceId {
        self.device_id
    }
}

/// One xHCI controller.
struct XhciController {
    registers: BarAccess,
    device_id: aster_pci::PciDeviceId,
    cap_length: u8,
    runtime_offset: u32,
    /// Doorbell array offset from BAR0.
    doorbell_offset: u32,
    port_count: u8,
    /// Device Context Base Address Array; must stay allocated.
    dcbaa: DmaCoherent,
    /// The event ring segment table; must stay allocated.
    erst: DmaCoherent,
    /// Command and event rings, consumed by the poll worker.
    rings: SpinLock<Rings>,
    /// Per-slot input/device contexts and EP0 rings.
    slots: SpinLock<Vec<Option<SlotState>>>,
    /// Connection state from the previous poll, per port.
    ports_connected: SpinLock<Vec<bool>>,
}

impl core::fmt::Debug for XhciController {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("XhciController")
            .field("ports", &self.port_count)
            .finish_non_exhaustive()
    }
}

impl XhciController {
    fn new(registers: BarAccess, device_id: aster_pci::PciDeviceId) -> Result<Self> {
        let allocate = || {
            DmaCoherent::alloc(1, true)
                .map_err(|_| Error::with_message(Errno::ENOMEM, "no DMA memory for xHCI rings"))
        };
        let dcbaa = allocate()?;
        let erst = allocate()?;
        let rings = Rings::new()?;

        let mut controller = Self {
            registers,
            device_id,
            cap_length: 0,
            runtime_offset: 0,
            doorbell_offset: 0,
            port_count: 0,
            dcbaa,
            erst,
            rings: SpinLock::new(rings),
            slots: new_slot_table(),
            ports_connected: SpinLock::new(Vec::new()),
        };
        controller.initialize()?;
        Ok(controller)
    }

    fn read32(&self, offset: usize) -> u32 {
        self.registers.read_once::<u32>(offset).unwrap_or(0)
    }

    fn write32(&self, offset: usize, value: u32) {
        let _ = self.registers.write_once::<u32>(offset, value);
    }

    fn op(&self, offset: usize) -> usize {
        self.cap_length as usize + offset
    }

    fn rt(&self, offset: usize) -> usize {
        self.runtime_offset as usize + offset
    }

    fn portsc(&self, port: u8) -> usize {
        self.op(REG_PORTSC_BASE + (port as usize - 1) * REG_PORTSC_STRIDE)
    }

    fn read_portsc(&self, port: u8) -> u32 {
        self.read32(self.portsc(port))
    }

    /// Rings a host-controller doorbell (`target` 0 is the command ring).
    fn ring_doorbell(&self, target: u32, value: u32) {
        self.write32(self.doorbell_offset as usize + 4 * target as usize, value);
    }

    /// Points the Device Context Base Address Array at `slot`'s context page.
    fn write_dcbaa(&self, slot: u8, address: u64) -> Result<()> {
        let offset = slot as usize * 8;
        self.dcbaa
            .write_val(offset, &address)
            .map_err(|_| Error::with_message(Errno::EIO, "the DCBAA page is unusable"))
    }

    /// Resets the controller, programs the rings and starts it.
    fn initialize(&mut self) -> Result<()> {
        self.cap_length = self.registers.read_once::<u8>(REG_CAPLENGTH).unwrap_or(0);
        let hciversion = self.registers.read_once::<u16>(0x02).unwrap_or(0);
        let hcsparams1 = self.read32(REG_HCSPARAMS1);
        let hcsparams2 = self.read32(REG_HCSPARAMS2);
        let hccparams1 = self.read32(REG_HCCPARAMS1);
        self.doorbell_offset = self.read32(REG_DBOFF) & !0x3;
        self.runtime_offset = self.read32(REG_RTSOFF) & !0x1F;
        let slots = (hcsparams1 & 0xFF) as u8;
        self.port_count = ((hcsparams1 >> 24) & 0xFF) as u8;
        let scratchpads =
            ((hcsparams2 >> 21) & 0x1F) as u32 | (((hcsparams2 >> 27) & 0x1F) as u32) << 5;

        info!(
            "controller {:04x}:{:04x} version={}.{} caplength={:#x} slots={} ports={} scratchpads={} hccparams1={:#x}",
            self.device_id.vendor_id,
            self.device_id.device_id,
            hciversion >> 8,
            hciversion & 0xFF,
            self.cap_length,
            slots,
            self.port_count,
            scratchpads,
            hccparams1
        );
        if scratchpads != 0 {
            // No device on QEMU needs them; say so rather than fault later.
            warn!(
                "the controller wants {} scratchpad buffers, which this driver does not allocate",
                scratchpads
            );
        }

        // Reset: HCRST stays set until the controller finishes resetting.
        self.write32(self.op(REG_USBCMD), USBCMD_HCRST);
        let mut waited = 0u32;
        while self.read32(self.op(REG_USBCMD)) & USBCMD_HCRST != 0
            || self.read32(self.op(REG_USBSTS)) & USBSTS_CNR != 0
        {
            core::hint::spin_loop();
            waited += 1;
            if waited > 10_000_000 {
                return_errno_with_message!(Errno::ETIMEDOUT, "the controller did not reset");
            }
        }

        // Enable every slot and point the controller at the rings.
        self.write32(self.op(REG_CONFIG), CONFIG_MAX_SLOTS);
        self.write32(self.op(REG_DCBAAP), self.dcbaa.daddr() as u32);
        self.write32(
            self.op(REG_DCBAAP) + 4,
            (self.dcbaa.daddr() >> 32) as u32,
        );

        {
            let rings = self.rings.lock();
            // RCS (ring cycle state) starts set.
            let command = rings.command.daddr();
            drop(rings);
            self.write32(self.op(REG_CRCR), command as u32 | 1);
            self.write32(self.op(REG_CRCR) + 4, (command >> 32) as u32);
        }

        // One event-ring segment; the erst entry is {ring address, size}.
        let to_error = |_| Error::with_message(Errno::EIO, "the event ring table is unusable");
        let event = self.rings.lock().event.daddr();
        self.erst.write_val(0, &event).map_err(to_error)?;
        self.erst.write_val(8, &EVENT_RING_TRBS).map_err(to_error)?;
        self.erst.write_val(12, &0u32).map_err(to_error)?;
        self.write32(self.rt(REG_ERSTSZ), 1);
        self.write32(self.rt(REG_ERSTBA), self.erst.daddr() as u32);
        self.write32(
            self.rt(REG_ERSTBA) + 4,
            (self.erst.daddr() >> 32) as u32,
        );
        self.write32(self.rt(REG_ERDP), event as u32);
        self.write32(self.rt(REG_ERDP) + 4, (event >> 32) as u32);

        self.write32(self.op(REG_USBCMD), USBCMD_RUN);
        let mut waited = 0u32;
        while self.read32(self.op(REG_USBSTS)) & USBSTS_HCH != 0 {
            core::hint::spin_loop();
            waited += 1;
            if waited > 10_000_000 {
                return_errno_with_message!(Errno::ETIMEDOUT, "the controller did not start");
            }
        }

        for port in 1..=self.port_count {
            let portsc = self.read_portsc(port);
            info!(
                "port {}: {}",
                port,
                if portsc & PORTSC_CCS != 0 {
                    "connected"
                } else {
                    "empty"
                }
            );
        }
        // Everything starts "disconnected" so the first poll resets and
        // enumerates whatever was attached while the kernel booted.
        *self.ports_connected.lock() = vec![false; self.port_count as usize];

        Ok(())
    }

    /// Polls every port, resets and enumerates new devices.
    fn poll_ports(&self) {
        let mut connected = self.ports_connected.lock();
        for port in 1..=self.port_count {
            let portsc = self.read_portsc(port);
            let plugged = portsc & PORTSC_CCS != 0;
            let index = port as usize - 1;
            let changed = plugged != connected[index];
            if changed {
                connected[index] = plugged;
            }

            if changed && plugged {
                info!("port {} connected", port);
                match self
                    .reset_port(port)
                    .and_then(|()| self.enumerate_port(port))
                {
                    Ok(()) => info!("port {} enumerated", port),
                    Err(error) => warn!("port {} enumeration failed: {:?}", port, error),
                }
            } else if changed {
                info!("port {} disconnected", port);
            }

            // Clear the change bits so the next transition is reported.
            let portsc = self.read_portsc(port);
            if portsc & PORTSC_CHANGE_BITS != 0 {
                self.write32(self.portsc(port), portsc & PORTSC_CHANGE_BITS);
            }
        }
    }
}

/// Watches the root-hub ports and the event ring from a kernel thread.
fn start_port_watch(controller: Arc<XhciController>) {
    spawn_kernel_thread("xhci-ports", move || {
        loop {
            let _ = wait_until_or_timeout(
                || false,
                |_| {},
                Some(Duration::from_millis(300)),
            );
            controller.poll_events();
            controller.poll_ports();
        }
    });
}

#[init_component(process)]
fn init() -> Result<(), ComponentInitError> {
    PCI_BUS.lock().register_driver(Arc::new(XhciDriver));
    Ok(())
}
