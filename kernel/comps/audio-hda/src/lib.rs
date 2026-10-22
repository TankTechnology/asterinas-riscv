// SPDX-License-Identifier: MPL-2.0

//! A minimal Intel HDA (High Definition Audio) controller driver.
//!
//! QEMU's `intel-hda` is the only audio device available to both target
//! architectures in this tree: it presents a memory-mapped register file
//! (BAR0), while AC97's registers are I/O ports (which this kernel cannot
//! touch) and this QEMU build ships no `virtio-snd` device.
//!
//! This first increment brings the controller up and enumerates its codec
//! through the immediate-command interface: reset, codec presence, the codec's
//! vendor identity, and the widget tree a playback stream needs.

#![no_std]
#![deny(unsafe_code)]

extern crate alloc;

// Set this crate's log prefix for `ostd::log`.
macro_rules! __log_prefix {
    () => {
        ""
    };
}

use alloc::sync::Arc;
use core::sync::atomic::{AtomicBool, Ordering};

use aster_core::prelude::*;
use aster_pci::{
    PCI_BUS,
    bus::{PciDevice, PciDriver},
    cfg_space::BarAccess,
    common_device::PciCommonDevice,
};
use component::{ComponentInitError, init_component};
use ostd::{bus::BusProbeError, info, warn};

mod bus;
mod codec;
mod pcm;
mod regs;
mod stream;
mod uapi;

use self::{bus::CorbRirb, codec::HdaCodec, regs::*};
use ostd::sync::Mutex;

/// Intel ICH6/ICH7/ICH9 HDA controllers, the IDs QEMU's `intel-hda` and
/// `ich9-intel-hda` expose.
const INTEL_VENDOR_ID: u16 = 0x8086;
const INTEL_HDA_DEVICE_IDS: [u16; 3] = [0x2668, 0x27d8, 0x293e];

/// Set by `aster_audio.selftest`: play the bring-up tone through the first
/// playback stream. Off by default so a desktop boot is silent.
static SELFTEST: AtomicBool = AtomicBool::new(false);
aster_cmdline::define_flag_param!("aster_audio.selftest", SELFTEST);

#[derive(Debug)]
struct HdaDriver;

impl PciDriver for HdaDriver {
    fn probe(
        &self,
        mut device: PciCommonDevice,
    ) -> Result<Arc<dyn PciDevice>, (BusProbeError, PciCommonDevice)> {
        let id = *device.device_id();
        info!("probing {:04x}:{:04x}", id.vendor_id, id.device_id);
        if id.vendor_id != INTEL_VENDOR_ID || !INTEL_HDA_DEVICE_IDS.contains(&id.device_id) {
            return Err((BusProbeError::DeviceNotMatch, device));
        }

        let bar = match device.bar_manager_mut().bar_mut(0).map(|bar| bar.acquire()) {
            Some(Ok(bar)) => bar,
            _ => return Err((BusProbeError::ConfigurationSpaceError, device)),
        };

        match HdaController::new(bar, id) {
            Ok(controller) => {
                let controller = Arc::new(controller);
                controller.bring_up();
                Ok(controller)
            }
            Err(error) => {
                warn!("controller bring-up failed: {:?}", error);
                Err((BusProbeError::ConfigurationSpaceError, device))
            }
        }
    }
}

impl PciDevice for HdaController {
    fn device_id(&self) -> aster_pci::PciDeviceId {
        self.device_id
    }
}

/// The converter and pin a playback stream drives.
#[derive(Clone, Copy, Debug)]
pub(crate) struct PlaybackCodec {
    pub address: u8,
    pub dac: u8,
    pub pin: u8,
}

/// Register access and codec state of one HDA controller.
#[derive(Debug)]
struct HdaController {
    registers: BarAccess,
    device_id: aster_pci::PciDeviceId,
    /// The command rings, created once the controller is reset.
    bus: Mutex<Option<CorbRirb>>,
    /// The converter and pin the PCM node plays through.
    playback: Mutex<Option<PlaybackCodec>>,
}

impl HdaController {
    fn new(registers: BarAccess, device_id: aster_pci::PciDeviceId) -> ostd::Result<Self> {
        Ok(Self {
            registers,
            device_id,
            bus: Mutex::new(None),
            playback: Mutex::new(None),
        })
    }

    fn read32(&self, offset: usize) -> u32 {
        self.registers.read_once::<u32>(offset).unwrap_or(0)
    }

    fn write32(&self, offset: usize, value: u32) {
        let _ = self.registers.write_once::<u32>(offset, value);
    }

    fn read16(&self, offset: usize) -> u16 {
        self.registers.read_once::<u16>(offset).unwrap_or(0)
    }

    fn write16(&self, offset: usize, value: u16) {
        let _ = self.registers.write_once::<u16>(offset, value);
    }

    fn read8(&self, offset: usize) -> u8 {
        self.registers.read_once::<u8>(offset).unwrap_or(0)
    }

    fn write8(&self, offset: usize, value: u8) {
        let _ = self.registers.write_once::<u8>(offset, value);
    }

    /// Resets the controller and enumerates the codecs behind it.
    fn bring_up(self: &Arc<Self>) {
        let gcap = self.read16(REG_GCAP);
        let vmin = self.read8(REG_VMIN);
        let vmaj = self.read8(REG_VMAJ);
        info!(
            "controller {:04x}:{:04x} gcap={:#06x} v{}.{} out_streams={} in_streams={}",
            INTEL_VENDOR_ID,
            self.device_id.device_id,
            gcap,
            vmaj,
            vmin,
            (gcap >> 12) & 0xF,
            (gcap >> 8) & 0xF
        );

        // Reset the controller: set CRST and wait for the hardware to clear it.
        let gctl = self.read32(REG_GCTL);
        self.write32(REG_GCTL, gctl & !GCTL_CRST);
        let mut waited = 0;
        while self.read32(REG_GCTL) & GCTL_CRST != 0 && waited < 1000 {
            core::hint::spin_loop();
            waited += 1;
        }
        self.write32(REG_GCTL, gctl | GCTL_CRST);
        let mut waited = 0;
        while self.read32(REG_GCTL) & GCTL_CRST == 0 && waited < 1_000_000 {
            core::hint::spin_loop();
            waited += 1;
        }
        if self.read32(REG_GCTL) & GCTL_CRST == 0 {
            warn!("controller reset did not complete");
            return;
        }

        // STATESTS has one bit per codec slot; writing the value back clears it.
        let statests = self.read16(REG_STATESTS);
        self.write16(REG_STATESTS, statests);
        info!("reset complete, STATESTS={:#06x}", statests);

        self.write32(REG_INTCTL, 0);
        self.write32(REG_INTSTS, 0xFFFF_FFFF);

        let bus = match CorbRirb::new(self) {
            Ok(bus) => bus,
            Err(error) => {
                warn!("failed to start the command rings: {:?}", error);
                return;
            }
        };
        let mut guard = self.bus.lock();
        *guard = Some(bus);
        let Some(bus) = guard.as_mut() else {
            unreachable!();
        };

        for slot in 0..MAX_CODECS {
            if statests & (1 << slot) == 0 {
                continue;
            }
            match HdaCodec::enumerate(self, bus, slot as u8) {
                Ok(codec) => {
                    codec.log_summary();
                    match codec.output_paths(self, bus) {
                        Ok(paths) => info!("output paths (pin -> dac): {:?}", paths),
                        Err(error) => warn!("output path query failed: {:?}", error),
                    }
                    if let (Some(dac), Some(pin)) = (codec.dac(), codec.output_pin()) {
                        *self.playback.lock() = Some(PlaybackCodec {
                            address: codec.address(),
                            dac,
                            pin,
                        });
                        // The PCM node exists as soon as a codec can play.
                        PCM_REGISTERED.call_once(|| pcm::register(self));
                    }
                    if SELFTEST.load(Ordering::Relaxed)
                        && let Some(playback) = *self.playback.lock()
                    {
                        match stream::play_test_tone(self, playback, bus) {
                            Ok(bytes) => info!("self-test tone finished: {} bytes", bytes),
                            Err(error) => warn!("self-test tone failed: {:?}", error),
                        }
                    }
                }
                Err(error) => warn!("codec {} enumeration failed: {:?}", slot, error),
            }
        }
    }
}

/// Keeps the PCM nodes from being registered twice.
static PCM_REGISTERED: spin::Once<()> = spin::Once::new();

impl HdaController {
    /// Plays one buffer through the PCM node's codec and stream.
    pub(crate) fn play(&self, data: &[u8]) -> Result<()> {
        let playback = self
            .playback
            .lock()
            .ok_or_else(|| Error::with_message(Errno::ENODEV, "no playback codec"))?;
        let mut bus = self.bus.lock();
        let Some(bus) = bus.as_mut() else {
            return_errno_with_message!(Errno::ENODEV, "the command rings are not running");
        };
        stream::play_bytes(self, playback, bus, data)
    }
}

#[init_component(process)]
fn init() -> Result<(), ComponentInitError> {
    PCI_BUS.lock().register_driver(Arc::new(HdaDriver));
    Ok(())
}
