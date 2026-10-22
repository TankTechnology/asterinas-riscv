// SPDX-License-Identifier: MPL-2.0

//! Enumeration over the command and event rings.
//!
//! Increment 1 covers what makes a hot-plugged device visible: port reset,
//! `Enable Slot`, `Address Device`, `GET_DESCRIPTOR` over EP0, and the
//! `/sys/bus/usb/devices/<name>` entry carrying `idVendor`/`idProduct`.
//! Configuration parsing, interfaces and usb-storage come next.

use alloc::{format, sync::Arc, vec, vec::Vec};

use aster_core::prelude::*;
use ostd::{
    info,
    mm::{HasDaddr, VmIo, dma::DmaCoherent},
    sync::SpinLock,
    warn,
};

use super::XhciController;

/// TRBs per ring: the command ring is fixed at 256 by the specification.
const RING_TRBS: usize = 256;

/// Event TRB types.
const TRB_TRANSFER_EVENT: u32 = 32;
const TRB_COMMAND_COMPLETION: u32 = 33;
/// Command TRB types.
const TRB_ENABLE_SLOT: u32 = 9;
const TRB_ADDRESS_DEVICE: u32 = 11;
const TRB_LINK: u32 = 6;
/// Transfer TRB types.
const TRB_SETUP_STAGE: u32 = 2;
const TRB_DATA_STAGE: u32 = 3;
const TRB_STATUS_STAGE: u32 = 4;
/// Completion codes.
const COMPLETION_SUCCESS: u32 = 1;
const COMPLETION_SHORT_PACKET: u32 = 13;

/// One 16-byte Transfer Request Block.
#[derive(Clone, Copy, Debug, Default)]
pub(super) struct Trb {
    pub param: u64,
    pub status: u32,
    pub control: u32,
}

impl Trb {
    pub(super) fn type_(&self) -> u32 {
        (self.control >> 10) & 0x3F
    }

    pub(super) fn cycle(&self) -> u32 {
        self.control & 1
    }

    pub(super) fn completion_code(&self) -> u32 {
        (self.status >> 24) & 0xFF
    }

    pub(super) fn slot_id(&self) -> u8 {
        (self.control & 0xFF) as u8
    }

    pub(super) fn residual(&self) -> usize {
        (self.status & 0xFF_FFFF) as usize
    }
}

/// A ring of 256 TRBs in one DMA page.
///
/// Producer rings (command, transfer) keep a Link TRB in the last slot; the
/// event ring wraps by size instead, and is consumed rather than produced.
pub(super) struct Ring {
    memory: Arc<DmaCoherent>,
    /// Next slot this side will touch.
    index: usize,
    /// Cycle state of the next TRB expected/written.
    cycle: u32,
    /// Whether the last slot is a Link TRB (producer rings).
    has_link: bool,
}

impl Ring {
    pub(super) fn new(has_link: bool) -> Result<Self> {
        let memory = Arc::new(
            DmaCoherent::alloc(1, true)
                .map_err(|_| Error::with_message(Errno::ENOMEM, "no DMA memory for a ring"))?,
        );
        let mut ring = Self {
            memory,
            index: 0,
            cycle: 1,
            has_link,
        };
        // The controller may reach the link before software writes another
        // TRB, so it has to exist from the start.
        if has_link {
            ring.write_link(1)?;
        }
        Ok(ring)
    }

    pub(super) fn daddr(&self) -> u64 {
        self.memory.daddr() as u64
    }

    fn write(&self, index: usize, trb: &Trb) -> Result<()> {
        let base = index * 16;
        self.memory.write_val(base, &trb.param)?;
        self.memory.write_val(base + 8, &trb.status)?;
        self.memory.write_val(base + 12, &trb.control)?;
        Ok(())
    }

    fn read(&self, index: usize) -> Result<Trb> {
        let base = index * 16;
        let param: u64 = self.memory.read_val(base)?;
        let status: u32 = self.memory.read_val(base + 8)?;
        let control: u32 = self.memory.read_val(base + 12)?;
        Ok(Trb {
            param,
            status,
            control,
        })
    }

    fn write_link(&mut self, cycle: u32) -> Result<()> {
        self.write(
            RING_TRBS - 1,
            &Trb {
                param: self.daddr(),
                status: 0,
                control: (TRB_LINK << 10) | (1 << 1) | cycle,
            },
        )
    }

    /// Writes one TRB and returns the address the controller will see.
    fn push(&mut self, trb: Trb) -> Result<u64> {
        if self.has_link && self.index == RING_TRBS - 1 {
            self.write_link(self.cycle)?;
            self.index = 0;
            self.cycle ^= 1;
        }
        let address = self.daddr() + (self.index * 16) as u64;
        self.write(
            self.index,
            &Trb {
                control: (trb.control & !1) | self.cycle,
                ..trb
            },
        )?;
        self.index += 1;
        Ok(address)
    }

    /// Consumes one completed TRB, if the controller has published one.
    fn pop(&mut self) -> Result<Option<Trb>> {
        let trb = self.read(self.index)?;
        if trb.cycle() != self.cycle {
            return Ok(None);
        }
        self.index += 1;
        if self.index == RING_TRBS {
            self.index = 0;
            self.cycle ^= 1;
        }
        Ok(Some(trb))
    }

    /// Address the controller must be told to resume from.
    fn current_address(&self) -> u64 {
        self.daddr() + (self.index * 16) as u64
    }
}

/// The host controller's command and event rings.
pub(super) struct Rings {
    pub(super) command: Ring,
    pub(super) event: Ring,
}

impl Rings {
    pub(super) fn new() -> Result<Self> {
        Ok(Self {
            command: Ring::new(true)?,
            event: Ring::new(false)?,
        })
    }
}

/// Per-slot state kept while a device is addressed.
pub(super) struct SlotState {
    input_context: DmaCoherent,
    device_context: DmaCoherent,
    ep0: Ring,
}

impl XhciController {
    /// Drains events no command or transfer is waiting for.
    ///
    /// Connection changes are driven from `PORTSC` polling rather than from
    /// these events, so anything left here is stale by definition.
    pub(super) fn poll_events(&self) {
        loop {
            match self.event_ring_pop() {
                Ok(Some(_)) => {}
                Ok(None) => break,
                Err(error) => {
                    warn!("event ring: {:?}", error);
                    break;
                }
            }
        }
    }

    /// Reads the next event and tells the controller that it was consumed.
    pub(super) fn event_ring_pop(&self) -> Result<Option<Trb>> {
        let mut rings = self.rings.lock();
        let event = rings.event.pop()?;
        if event.is_some() {
            let address = rings.event.current_address();
            drop(rings);
            // Writing the new dequeue pointer without EHB acknowledges the TRB.
            self.write32(self.rt(super::REG_ERDP), address as u32 & !0xF);
            self.write32(
                self.rt(super::REG_ERDP) + 4,
                (address >> 32) as u32,
            );
        }
        Ok(event)
    }

    /// Queues one command TRB and rings the host-controller doorbell.
    fn command_push(&self, trb: Trb) -> Result<u64> {
        let address = self.rings.lock().command.push(trb)?;
        self.ring_doorbell(0, 0);
        Ok(address)
    }

    /// Waits for the completion event of the command at `address`.
    fn wait_command(&self, address: u64) -> Result<Trb> {
        let mut spin = 0u64;
        loop {
            if let Some(event) = self.event_ring_pop()? {
                if event.type_() == TRB_COMMAND_COMPLETION && event.param == address {
                    return Ok(event);
                }
                // Anything else (a stale transfer or a port change) is not
                // what this command is waiting for.
            }
            core::hint::spin_loop();
            spin += 1;
            if spin > 20_000_000 {
                return_errno_with_message!(Errno::ETIMEDOUT, "a command never completed");
            }
        }
    }

    fn enable_slot(&self) -> Result<u8> {
        let address = self.command_push(Trb {
            param: 0,
            status: 0,
            control: TRB_ENABLE_SLOT << 10,
        })?;
        let event = self.wait_command(address)?;
        if event.completion_code() != COMPLETION_SUCCESS {
            warn!("Enable Slot failed with completion code {}", event.completion_code());
            return_errno_with_message!(Errno::EIO, "Enable Slot failed");
        }
        Ok(event.slot_id())
    }

    fn address_device(&self, slot_id: u8, input_context: u64) -> Result<()> {
        let address = self.command_push(Trb {
            param: input_context,
            status: 0,
            control: (TRB_ADDRESS_DEVICE << 10) | ((slot_id as u32) << 24),
        })?;
        let event = self.wait_command(address)?;
        if event.completion_code() != COMPLETION_SUCCESS {
            warn!(
                "Address Device failed with completion code {} (slot {})",
                event.completion_code(),
                slot_id
            );
            return_errno_with_message!(Errno::EIO, "Address Device failed");
        }
        Ok(())
    }

    /// Runs one `GET_DESCRIPTOR` control transfer on EP0.
    fn get_descriptor(
        &self,
        slot: &mut SlotState,
        slot_id: u8,
        value: u16,
        length: u16,
    ) -> Result<Vec<u8>> {
        let buffer = DmaCoherent::alloc(1, true)
            .map_err(|_| Error::with_message(Errno::ENOMEM, "no descriptor buffer"))?;

        // Device-to-host, standard, device: GET_DESCRIPTOR.
        let setup: u64 =
            0x80 | (6 << 8) | ((value as u64) << 16) | ((length as u64) << 48);
        slot.ep0.push(Trb {
            param: setup,
            status: 8,
            control: (TRB_SETUP_STAGE << 10) | (3 << 16) | (1 << 6),
        })?;
        let data_address = slot.ep0.push(Trb {
            param: buffer.daddr() as u64,
            status: length as u32,
            control: (TRB_DATA_STAGE << 10) | (1 << 16) | (1 << 5),
        })?;
        let status_address = slot.ep0.push(Trb {
            param: 0,
            status: 0,
            control: (TRB_STATUS_STAGE << 10) | (1 << 5),
        })?;
        self.ring_doorbell(slot_id as u32, 1);

        let mut transferred = length as usize;
        let (mut data_done, mut status_done) = (false, false);
        let mut spin = 0u64;
        while !(data_done && status_done) {
            if let Some(event) = self.event_ring_pop()? {
                if event.type_() == TRB_TRANSFER_EVENT && event.slot_id() == slot_id {
                    let code = event.completion_code();
                    if event.param == data_address {
                        if code != COMPLETION_SUCCESS && code != COMPLETION_SHORT_PACKET {
                            return_errno_with_message!(
                                Errno::EIO,
                                "the data stage of GET_DESCRIPTOR failed"
                            );
                        }
                        transferred = (length as usize).saturating_sub(event.residual());
                        data_done = true;
                    } else if event.param == status_address {
                        if code != COMPLETION_SUCCESS {
                            return_errno_with_message!(
                                Errno::EIO,
                                "the status stage of GET_DESCRIPTOR failed"
                            );
                        }
                        status_done = true;
                    }
                }
            }
            core::hint::spin_loop();
            spin += 1;
            if spin > 20_000_000 {
                return_errno_with_message!(Errno::ETIMEDOUT, "GET_DESCRIPTOR timed out");
            }
        }

        let mut descriptor = vec![0u8; transferred.min(length as usize)];
        for (index, byte) in descriptor.iter_mut().enumerate() {
            *byte = buffer.read_val::<u8>(index)?;
        }
        Ok(descriptor)
    }

    /// Resets the port so the device leaves the Default state.
    pub(super) fn reset_port(&self, port: u8) -> Result<()> {
        let register = self.portsc(port);
        let portsc = self.read32(register);
        // Change bits are write-one-to-clear and PED is write-one-to-disable,
        // so neither may be carried over into the write that starts the reset.
        let value = (portsc & !super::PORTSC_CHANGE_BITS & !super::PORTSC_PED)
            | super::PORTSC_PR;
        self.write32(register, value);

        let mut spin = 0u64;
        loop {
            let portsc = self.read32(register);
            // The reset is complete when PR has cleared and the port reports
            // either the reset change or an enabled device.
            let done = portsc & super::PORTSC_PR == 0
                && (portsc & (super::PORTSC_PED | super::PORTSC_PRC) != 0);
            if done {
                self.write32(register, portsc & super::PORTSC_CHANGE_BITS);
                if portsc & super::PORTSC_CCS == 0 {
                    return_errno_with_message!(Errno::ENODEV, "the device left during reset");
                }
                info!(
                    "port {} reset: portsc={:#x} speed={}",
                    port,
                    portsc,
                    (portsc >> 10) & 0xF
                );
                return Ok(());
            }
            core::hint::spin_loop();
            spin += 1;
            if spin > 20_000_000 {
                return_errno_with_message!(Errno::ETIMEDOUT, "the port did not reset");
            }
        }
    }

    /// Handles a newly connected port: slot, address and device descriptor.
    pub(super) fn enumerate_port(&self, port: u8) -> Result<()> {
        let portsc = self.read_portsc(port);
        let speed = (portsc >> 10) & 0xF;

        let slot_id = self.enable_slot()?;
        let input_context = DmaCoherent::alloc(1, true)
            .map_err(|_| Error::with_message(Errno::ENOMEM, "no input context"))?;
        let device_context = DmaCoherent::alloc(1, true)
            .map_err(|_| Error::with_message(Errno::ENOMEM, "no device context"))?;
        let mut slot = SlotState {
            input_context,
            device_context,
            ep0: Ring::new(true)?,
        };

        self.write_dcbaa(slot_id, slot.device_context.daddr() as u64)?;
        fill_input_context(&slot, port, speed)?;
        self.address_device(slot_id, slot.input_context.daddr() as u64)?;

        let descriptor = self.get_descriptor(&mut slot, slot_id, 0x0100, 18)?;
        if descriptor.len() < 18 {
            return_errno_with_message!(Errno::EIO, "a truncated device descriptor");
        }
        let vendor_id = u16::from_le_bytes([descriptor[8], descriptor[9]]);
        let product_id = u16::from_le_bytes([descriptor[10], descriptor[11]]);
        let class = descriptor[4];

        let bus_number = 1u8;
        let device_number = slot_id;
        let name = format!("{}-{}", bus_number, port);
        info!(
            "device {}: idVendor={:04x} idProduct={:04x} class={:#04x} speed={} slot={}",
            name, vendor_id, product_id, class, speed, slot_id
        );

        super::sysfs::publish_device(
            &name,
            vendor_id,
            product_id,
            bus_number,
            device_number,
            port,
            speed,
        );

        // The contexts and the EP0 ring must outlive this stack frame.
        self.retain_slot(slot_id, slot);
        Ok(())
    }

    fn retain_slot(&self, slot_id: u8, slot: SlotState) {
        let mut slots = self.slots.lock();
        let index = slot_id as usize;
        if index < slots.len() {
            slots[index] = Some(slot);
        }
    }
}

/// Fills the Input Context for `Address Device`.
fn fill_input_context(slot: &SlotState, port: u8, speed: u32) -> Result<()> {
    let context = &slot.input_context;

    // Input Control Context: add the slot and EP0 contexts (bits 0 and 1).
    context.write_val(4, &0x3u32)?;

    // Slot Context: one context entry, route string 0, speed, root-hub port.
    let slot_dword0: u32 = (1 << 27) | ((speed & 0xF) << 20);
    context.write_val(8 * 4, &slot_dword0)?;
    let slot_dword1: u32 = (port as u32) << 16;
    context.write_val(9 * 4, &slot_dword1)?;

    // EP0 Context: CErr 3, control type, max packet size for this speed.
    let max_packet: u32 = match speed {
        1 | 2 => 8,
        4 => 512,
        _ => 64,
    };
    let ep0_dword1: u32 = (max_packet << 16) | (4 << 3) | (3 << 1);
    context.write_val(17 * 4, &ep0_dword1)?;
    // TR dequeue pointer, with DCS (bit 0) set.
    let dequeue = slot.ep0.daddr();
    context.write_val(18 * 4, &((dequeue as u32) | 1))?;
    context.write_val(19 * 4, &((dequeue >> 32) as u32))?;
    // Average TRB length.
    context.write_val(20 * 4, &8u32)?;

    Ok(())
}

/// Allocates the slot-state table (slot 0 is unused).
pub(super) fn new_slot_table() -> SpinLock<Vec<Option<SlotState>>> {
    SpinLock::new((0..super::SLOT_COUNT).map(|_| None).collect())
}
