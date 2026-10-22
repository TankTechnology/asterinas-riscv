// SPDX-License-Identifier: MPL-2.0

//! A minimal xHCI host-controller driver.
//!
//! This first increment brings the controller up, starts it with empty
//! command/event rings, and watches the root-hub port registers so a device
//! plugged into QEMU (`device_add usb-storage`) is reported on the console.
//! Device enumeration, the USB core and the mass-storage class are the next
//! increment; the rings and registers they need are already programmed here.

#![no_std]
#![deny(unsafe_code)]

extern crate alloc;

// Set this crate's log prefix for `ostd::log`.
macro_rules! __log_prefix {
    () => {
        "xhci: "
    };
}

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

/// Red Hat's `qemu-xhci`.
const QEMU_XHCI: (u16, u16) = (0x1b36, 0x000d);
/// NEC/Renesas xHCI controllers.
const NEC_VENDOR_ID: u16 = 0x1033;
const NEC_XHCI_DEVICE_ID: u16 = 0x0194;

// Controller capability registers (BAR0 + 0).
const REG_CAPLENGTH: usize = 0x00;
const REG_HCSPARAMS1: usize = 0x04;
const REG_HCCPARAMS1: usize = 0x10;
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
const REG_ERDP: usize = 0x38;

const USBCMD_RUN: u32 = 1 << 0;
const USBCMD_HCRST: u32 = 1 << 1;
const USBSTS_HCH: u32 = 1 << 0;
const USBSTS_CNR: u32 = 1 << 11;
/// Maximum slots enabled by `CONFIG`.
const CONFIG_MAX_SLOTS: u32 = 0xFF;
/// `PORTSC`: current connect status.
const PORTSC_CCS: u32 = 1 << 0;
/// `PORTSC` write-1-to-clear change bits.
const PORTSC_CHANGE_BITS: u32 = 0xFE_00_00;
/// Event ring segment size (TRBs).
const EVENT_RING_TRBS: u32 = 256;

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
    port_count: u8,
    /// The command ring must stay allocated while the controller runs.
    #[expect(dead_code)]
    command_ring: DmaCoherent,
    #[expect(dead_code)]
    device_contexts: DmaCoherent,
    #[expect(dead_code)]
    erst: DmaCoherent,
    #[expect(dead_code)]
    event_ring: DmaCoherent,
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
        let command_ring = allocate()?;
        let device_contexts = allocate()?;
        let erst = allocate()?;
        let event_ring = allocate()?;

        let mut controller = Self {
            registers,
            device_id,
            cap_length: 0,
            runtime_offset: 0,
            port_count: 0,
            command_ring,
            device_contexts,
            erst,
            event_ring,
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

    /// Resets the controller, programs empty rings and starts it.
    fn initialize(&mut self) -> Result<()> {
        self.cap_length = self.registers.read_once::<u8>(REG_CAPLENGTH).unwrap_or(0);
        let hciversion = self.registers.read_once::<u16>(0x02).unwrap_or(0);
        let hcsparams1 = self.read32(REG_HCSPARAMS1);
        let hccparams1 = self.read32(REG_HCCPARAMS1);
        self.runtime_offset = self.read32(REG_RTSOFF) & !0x1F;
        let slots = (hcsparams1 & 0xFF) as u8;
        self.port_count = ((hcsparams1 >> 24) & 0xFF) as u8;

        info!(
            "controller {:04x}:{:04x} version={}.{} caplength={:#x} slots={} ports={} hccparams1={:#x}",
            self.device_id.vendor_id,
            self.device_id.device_id,
            hciversion >> 8,
            hciversion & 0xFF,
            self.cap_length,
            slots,
            self.port_count,
            hccparams1
        );

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

        // Enable every slot and point the controller at empty rings.
        self.write32(self.op(REG_CONFIG), CONFIG_MAX_SLOTS);
        self.write32(self.op(REG_DCBAAP), self.device_contexts.daddr() as u32);
        self.write32(
            self.op(REG_DCBAAP) + 4,
            (self.device_contexts.daddr() >> 32) as u32,
        );
        // RCS (ring cycle state) starts set.
        self.write32(self.op(REG_CRCR), self.command_ring.daddr() as u32 | 1);
        self.write32(
            self.op(REG_CRCR) + 4,
            (self.command_ring.daddr() >> 32) as u32,
        );

        // One event-ring segment; the erst entry is {ring address, size}.
        let to_error = |_| Error::with_message(Errno::EIO, "the event ring table is unusable");
        self.erst
            .write_val(0, &(self.event_ring.daddr() as u64))
            .map_err(to_error)?;
        self.erst.write_val(8, &EVENT_RING_TRBS).map_err(to_error)?;
        self.erst.write_val(12, &0u32).map_err(to_error)?;
        self.write32(self.rt(REG_ERSTSZ), 1);
        self.write32(self.rt(REG_ERSTBA), self.erst.daddr() as u32);
        self.write32(
            self.rt(REG_ERSTBA) + 4,
            (self.erst.daddr() >> 32) as u32,
        );
        self.write32(self.rt(REG_ERDP), self.event_ring.daddr() as u32);
        self.write32(
            self.rt(REG_ERDP) + 4,
            (self.event_ring.daddr() >> 32) as u32,
        );

        self.write32(self.op(REG_USBCMD), USBCMD_RUN);
        let mut waited = 0u32;
        while self.read32(self.op(REG_USBSTS)) & USBSTS_HCH != 0 {
            core::hint::spin_loop();
            waited += 1;
            if waited > 10_000_000 {
                return_errno_with_message!(Errno::ETIMEDOUT, "the controller did not start");
            }
        }

        let mut connected = vec![false; self.port_count as usize];
        for port in 1..=self.port_count {
            let portsc = self.read32(self.portsc(port));
            connected[port as usize - 1] = portsc & PORTSC_CCS != 0;
            info!("port {}: {}", port, if portsc & PORTSC_CCS != 0 { "connected" } else { "empty" });
        }
        *self.ports_connected.lock() = connected;

        Ok(())
    }

    /// Polls every port and logs connection changes.
    fn poll_ports(&self) {
        let mut connected = self.ports_connected.lock();
        for port in 1..=self.port_count {
            let portsc = self.read32(self.portsc(port));
            let plugged = portsc & PORTSC_CCS != 0;
            let index = port as usize - 1;
            if plugged != connected[index] {
                connected[index] = plugged;
                if plugged {
                    info!("port {} connected", port);
                } else {
                    info!("port {} disconnected", port);
                }
            }
            // Clear the change bits so the next transition is reported.
            if portsc & PORTSC_CHANGE_BITS != 0 {
                self.write32(self.portsc(port), portsc & PORTSC_CHANGE_BITS);
            }
        }
    }
}

/// Watches the root-hub ports from a kernel thread.
fn start_port_watch(controller: Arc<XhciController>) {
    spawn_kernel_thread("xhci-ports", move || {
        loop {
            let _ = wait_until_or_timeout(
                || false,
                |_| {},
                Some(Duration::from_millis(500)),
            );
            controller.poll_ports();
        }
    });
}

#[init_component(process)]
fn init() -> Result<(), ComponentInitError> {
    PCI_BUS.lock().register_driver(Arc::new(XhciDriver));
    Ok(())
}
