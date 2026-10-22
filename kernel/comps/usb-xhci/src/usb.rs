// SPDX-License-Identifier: MPL-2.0

//! Enumeration over the command and event rings.
//!
//! Increment 1 covers what makes a hot-plugged device visible: port reset,
//! `Enable Slot`, `Address Device`, `GET_DESCRIPTOR` over EP0, and the
//! `/sys/bus/usb/devices/<name>` entry carrying `idVendor`/`idProduct`.
//! Configuration parsing, interfaces and usb-storage come next.

use alloc::{format, string::String, sync::Arc, vec, vec::Vec};

use aster_core::prelude::*;
use ostd::{
    info,
    mm::{HasDaddr, VmIo, dma::DmaCoherent},
    sync::{Mutex, SpinLock},
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
const TRB_CONFIGURE_ENDPOINT: u32 = 12;
const TRB_STOP_ENDPOINT: u32 = 15;
const TRB_SET_TR_DEQUEUE: u32 = 16;
/// `Normal` transfer TRB.
const TRB_NORMAL: u32 = 1;
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
#[derive(Debug)]
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
    pub(super) input_context: DmaCoherent,
    pub(super) device_context: DmaCoherent,
    pub(super) ep0: Ring,
    /// Filled once the device is configured; the bulk endpoints it lists are
    /// what the mass-storage transport uses.
    pub(super) configuration: Option<Configuration>,
    /// Transfer rings, one per configured endpoint.
    pub(super) endpoints: Vec<Mutex<EndpointState>>,
    /// Indices into `endpoints`.
    pub(super) bulk_in: Option<usize>,
    pub(super) bulk_out: Option<usize>,
    /// Set when the device turned out to speak Bulk-Only Transport.
    pub(super) storage: Option<super::storage::StorageInfo>,
}

/// One configured endpoint and its transfer ring.
#[derive(Debug)]
pub(super) struct EndpointState {
    /// Device Context Index: 2N for OUT, 2N+1 for IN.
    pub(super) dci: u8,
    #[expect(dead_code)]
    pub(super) is_in: bool,
    pub(super) ring: Ring,
}

/// One endpoint of an interface.
#[derive(Debug)]
pub(super) struct EndpointDescriptor {
    pub address: u8,
    pub attributes: u8,
    pub max_packet: u16,
    /// SuperSpeed companion `bMaxBurst`; zero for USB 2 endpoints.
    pub max_burst: u8,
}

impl EndpointDescriptor {
    pub(super) fn number(&self) -> u8 {
        self.address & 0x0F
    }

    pub(super) fn is_in(&self) -> bool {
        self.address & 0x80 != 0
    }

    pub(super) fn is_bulk(&self) -> bool {
        self.attributes & 0x03 == 0x02
    }

    pub(super) fn describe(&self) -> String {
        format!(
            "{:02x}:{} {} maxpacket={} burst={}",
            self.address,
            if self.is_bulk() { "bulk" } else { "other" },
            if self.is_in() { "in" } else { "out" },
            self.max_packet,
            self.max_burst
        )
    }
}

/// One interface of the active configuration.
#[derive(Debug)]
pub(super) struct InterfaceDescriptor {
    pub number: u8,
    pub class: u8,
    pub subclass: u8,
    pub protocol: u8,
    pub endpoints: Vec<EndpointDescriptor>,
}

/// The device's active configuration, as parsed from its descriptors.
#[derive(Debug)]
pub(super) struct Configuration {
    pub value: u8,
    pub interfaces: Vec<InterfaceDescriptor>,
}

impl Configuration {
    /// `"81:bulk in maxpacket=1024\n02:bulk out ..."`, for the sysfs view.
    pub(super) fn endpoint_summary(&self) -> String {
        let mut summary = String::new();
        for interface in &self.interfaces {
            for endpoint in &interface.endpoints {
                summary.push_str(&endpoint.describe());
                summary.push('\n');
            }
        }
        summary
    }
}

impl XhciController {
    /// Reads the next event and tells the controller that it was consumed.
    pub(super) fn event_ring_pop(&self) -> Result<Option<Trb>> {
        let mut rings = self.rings.lock();
        let event = rings.event.pop()?;
        if event.is_some() {
            let address = rings.event.current_address();
            drop(rings);
            // Writing the new dequeue pointer without EHB acknowledges the TRB.
            // EHB is write-one-to-clear: software must write a 1 to it when
            // it publishes a new dequeue pointer, or the controller keeps
            // believing the event handler is still busy.
            const ERDP_EHB: u32 = 1 << 3;
            self.write32(self.rt(super::REG_ERDP), (address as u32 & !0xF) | ERDP_EHB);
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

    /// Runs one control transfer with a data-IN stage on EP0.
    fn control_in(
        &self,
        slot: &mut SlotState,
        slot_id: u8,
        setup: u64,
        length: u16,
    ) -> Result<Vec<u8>> {
        let buffer = DmaCoherent::alloc(1, true)
            .map_err(|_| Error::with_message(Errno::ENOMEM, "no descriptor buffer"))?;
        let length = length.min(4096);

        // `TRT = 3` selects the data-IN stage.
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
                                "the data stage of a control transfer failed"
                            );
                        }
                        transferred = (length as usize).saturating_sub(event.residual());
                        data_done = true;
                    } else if event.param == status_address {
                        if code != COMPLETION_SUCCESS {
                            return_errno_with_message!(
                                Errno::EIO,
                                "the status stage of a control transfer failed"
                            );
                        }
                        status_done = true;
                    }
                }
            }
            core::hint::spin_loop();
            spin += 1;
            if spin > 20_000_000 {
                return_errno_with_message!(Errno::ETIMEDOUT, "a control transfer timed out");
            }
        }

        let mut descriptor = vec![0u8; transferred.min(length as usize)];
        for (index, byte) in descriptor.iter_mut().enumerate() {
            *byte = buffer.read_val::<u8>(index)?;
        }
        Ok(descriptor)
    }

    /// Runs one control transfer without a data stage (`TRT = 0`).
    fn control_no_data(&self, slot: &mut SlotState, slot_id: u8, setup: u64) -> Result<()> {
        slot.ep0.push(Trb {
            param: setup,
            status: 8,
            control: (TRB_SETUP_STAGE << 10) | (1 << 6),
        })?;
        // The status stage of a no-data transfer goes IN.
        let status_address = slot.ep0.push(Trb {
            param: 0,
            status: 0,
            control: (TRB_STATUS_STAGE << 10) | (1 << 16) | (1 << 5),
        })?;
        self.ring_doorbell(slot_id as u32, 1);

        let mut spin = 0u64;
        loop {
            if let Some(event) = self.event_ring_pop()?
                && event.type_() == TRB_TRANSFER_EVENT
                && event.slot_id() == slot_id
                && event.param == status_address
            {
                if event.completion_code() != COMPLETION_SUCCESS {
                    return_errno_with_message!(
                        Errno::EIO,
                        "the status stage of a no-data control transfer failed"
                    );
                }
                return Ok(());
            }
            core::hint::spin_loop();
            spin += 1;
            if spin > 20_000_000 {
                return_errno_with_message!(Errno::ETIMEDOUT, "a control transfer timed out");
            }
        }
    }

    /// Reads and parses the active configuration descriptor.
    fn read_configuration(&self, slot: &mut SlotState, slot_id: u8) -> Result<Configuration> {
        let header = self.control_in(slot, slot_id, get_descriptor_setup(0x02, 0, 9), 9)?;
        if header.len() < 9 {
            return_errno_with_message!(Errno::EIO, "a truncated configuration descriptor");
        }
        let total = u16::from_le_bytes([header[2], header[3]]);
        let bytes = self.control_in(slot, slot_id, get_descriptor_setup(0x02, 0, total), total)?;
        parse_configuration(&bytes)
    }

    /// Selects the parsed configuration on the device.
    fn set_configuration(
        &self,
        slot: &mut SlotState,
        slot_id: u8,
        value: u8,
    ) -> Result<()> {
        self.control_no_data(slot, slot_id, control_setup(0x00, 9, value as u16, 0, 0))
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
                let change = portsc & super::PORTSC_CHANGE_BITS;
                let value = portsc & !super::PORTSC_CHANGE_BITS & !super::PORTSC_PED
                    & !super::PORTSC_PR;
                self.write32(register, value | change);
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

    /// Builds EP contexts for every endpoint and sends `Configure Endpoint`.
    fn configure_endpoints(&self, slot: &mut SlotState, slot_id: u8) -> Result<()> {
        let configuration = slot
            .configuration
            .as_ref()
            .ok_or_else(|| Error::with_message(Errno::EIO, "no configuration to apply"))?;

        let mut endpoints = Vec::new();
        let mut bulk_in = None;
        let mut bulk_out = None;
        for interface in &configuration.interfaces {
            for endpoint in &interface.endpoints {
                let dci = endpoint.number() * 2 + endpoint.is_in() as u8;
                let ring = Ring::new(true)?;
                let ep_type = if endpoint.is_bulk() {
                    if endpoint.is_in() { 6 } else { 2 }
                } else if endpoint.is_in() {
                    7
                } else {
                    3
                };

                // EP context of a DCI `k` lives at dwords `8 * (k + 1)`.
                let base = 8 * (dci as usize + 1) * 4;
                let info2 = ((endpoint.max_packet as u32) << 16)
                    | ((endpoint.max_burst as u32) << 8)
                    | ((ep_type as u32) << 3)
                    | (3 << 1);
                slot.input_context.write_val(base + 4, &info2)?;
                let dequeue = ring.daddr();
                slot.input_context
                    .write_val(base + 8, &((dequeue as u32) | 1))?;
                slot.input_context
                    .write_val(base + 12, &((dequeue >> 32) as u32))?;
                // Average TRB length.
                slot.input_context.write_val(base + 16, &8u32)?;

                if endpoint.is_bulk() {
                    if endpoint.is_in() {
                        bulk_in = Some(endpoints.len());
                    } else {
                        bulk_out = Some(endpoints.len());
                    }
                }
                endpoints.push(Mutex::new(EndpointState {
                    dci,
                    is_in: endpoint.is_in(),
                    ring,
                }));
            }
        }

        if endpoints.is_empty() {
            return Ok(());
        }

        let max_dci = endpoints
            .iter()
            .map(|endpoint| endpoint.lock().dci)
            .max()
            .unwrap();
        // Slot context: keep speed and port, raise the context-entry count.
        let slot_dword0: u32 = ((max_dci as u32) << 27)
            | (slot.input_context.read_val::<u32>(8 * 4)? & (0xF << 20));
        slot.input_context.write_val(8 * 4, &slot_dword0)?;

        let mut add_flags: u32 = 1;
        for endpoint in &endpoints {
            add_flags |= 1 << endpoint.lock().dci;
        }
        slot.input_context.write_val(4, &add_flags)?;

        let address = self.command_push(Trb {
            param: slot.input_context.daddr() as u64,
            status: 0,
            control: (TRB_CONFIGURE_ENDPOINT << 10) | ((slot_id as u32) << 24),
        })?;
        let event = self.wait_command(address)?;
        if event.completion_code() != COMPLETION_SUCCESS {
            warn!(
                "Configure Endpoint failed with completion code {}",
                event.completion_code()
            );
            return_errno_with_message!(Errno::EIO, "Configure Endpoint failed");
        }

        slot.bulk_in = bulk_in;
        slot.bulk_out = bulk_out;
        slot.endpoints = endpoints;
        info!(
            "configured {} endpoints (bulk in: {}, bulk out: {})",
            slot.endpoints.len(),
            bulk_in.is_some(),
            bulk_out.is_some()
        );
        Ok(())
    }

    /// Moves up to `length` bytes over a bulk endpoint and returns the count.
    pub(super) fn bulk_transfer(
        &self,
        slot_id: u8,
        endpoint: &mut EndpointState,
        daddr: u64,
        length: usize,
    ) -> Result<usize> {
        let dci = endpoint.dci;
        let trb_address = endpoint.ring.push(Trb {
            param: daddr,
            status: length as u32,
            control: (TRB_NORMAL << 10) | (1 << 5),
        })?;
        // The doorbell array is indexed by slot; the value names the endpoint.
        self.ring_doorbell(slot_id as u32, dci as u32);

        let mut spin = 0u64;
        loop {
            if let Some(event) = self.event_ring_pop()?
                && event.type_() == TRB_TRANSFER_EVENT
                && event.param == trb_address
            {
                let code = event.completion_code();
                if code != COMPLETION_SUCCESS && code != COMPLETION_SHORT_PACKET {
                    warn!(
                        "bulk dci {} failed with completion code {}",
                        dci, code
                    );
                    return_errno_with_message!(Errno::EIO, "a bulk transfer failed");
                }
                return Ok(length.saturating_sub(event.residual()));
            }
            core::hint::spin_loop();
            spin += 1;
            if spin > 20_000_000 {
                warn!(
                    "bulk timeout: slot {} dci {} trb {:#x} length {} usbsts {:#x} erdp {:#x}",
                    slot_id,
                    dci,
                    trb_address,
                    length,
                    self.read32(self.op(super::REG_USBSTS)),
                    self.read32(self.rt(super::REG_ERDP))
                );
                return_errno_with_message!(Errno::ETIMEDOUT, "a bulk transfer timed out");
            }
        }
    }

    /// Queues one transfer TRB and returns the address the event will carry.
    pub(super) fn push_transfer(
        &self,
        slot_id: u8,
        endpoint: &mut EndpointState,
        daddr: u64,
        length: usize,
        chain: bool,
        ioc: bool,
    ) -> Result<u64> {
        let mut control = TRB_NORMAL << 10;
        if chain {
            control |= 1 << 4;
        }
        if ioc {
            control |= 1 << 5;
        }
        let address = endpoint.ring.push(Trb {
            param: daddr,
            status: length as u32,
            control,
        })?;
        self.ring_doorbell(slot_id as u32, endpoint.dci as u32);
        Ok(address)
    }

    /// Waits for the completion of the TRB at `trb_address`.
    pub(super) fn wait_transfer(
        &self,
        slot_id: u8,
        trb_address: u64,
        length: usize,
    ) -> Result<usize> {
        let mut spin = 0u64;
        loop {
            if let Some(event) = self.event_ring_pop()?
                && event.type_() == TRB_TRANSFER_EVENT
                && event.param == trb_address
            {
                let code = event.completion_code();
                if code != COMPLETION_SUCCESS && code != COMPLETION_SHORT_PACKET {
                    warn!(
                        "transfer {:#x} failed with completion code {}",
                        trb_address, code
                    );
                    return_errno_with_message!(Errno::EIO, "a bulk transfer failed");
                }
                return Ok(length.saturating_sub(event.residual()));
            }
            core::hint::spin_loop();
            spin += 1;
            if spin > 20_000_000 {
                warn!(
                    "transfer timeout: slot {} trb {:#x} length {}",
                    slot_id, trb_address, length
                );
                return_errno_with_message!(Errno::ETIMEDOUT, "a bulk transfer timed out");
            }
        }
    }

    /// Stops, resets and re-arms one bulk endpoint after a halted transfer.
    ///
    /// The sequence is the controller side of the BOT reset recovery:
    /// `Stop Endpoint` -> `Set TR Dequeue Pointer` pointing at the next free
    /// TRB (DCS set), which leaves the endpoint running again.
    pub(super) fn recover_endpoint(
        &self,
        slot_id: u8,
        endpoint: &mut EndpointState,
    ) -> Result<()> {
        let dci = endpoint.dci;
        let stop = self.command_push(Trb {
            param: 0,
            status: 0,
            control: (TRB_STOP_ENDPOINT << 10) | ((dci as u32) << 16) | ((slot_id as u32) << 24),
        })?;
        let _ = self.wait_command(stop);

        let dequeue = endpoint.ring.current_address() | 1;
        let set = self.command_push(Trb {
            param: dequeue,
            status: 0,
            control: (TRB_SET_TR_DEQUEUE << 10) | ((dci as u32) << 16) | ((slot_id as u32) << 24),
        })?;
        let event = self.wait_command(set)?;
        if event.completion_code() != COMPLETION_SUCCESS {
            warn!(
                "Set TR Dequeue for dci {} failed with {}",
                dci,
                event.completion_code()
            );
            return_errno_with_message!(Errno::EIO, "endpoint recovery failed");
        }
        info!("recovered endpoint dci {} (dequeue {:#x})", dci, dequeue);
        Ok(())
    }

    /// Handles a newly connected port: slot, address and device descriptor.
    pub(super) fn enumerate_port(self: &Arc<Self>, port: u8) -> Result<()> {
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
            configuration: None,
            endpoints: Vec::new(),
            bulk_in: None,
            bulk_out: None,
            storage: None,
        };

        self.write_dcbaa(slot_id, slot.device_context.daddr() as u64)?;
        fill_input_context(&slot, port, speed)?;
        self.address_device(slot_id, slot.input_context.daddr() as u64)?;

        let descriptor =
            self.control_in(&mut slot, slot_id, get_descriptor_setup(0x01, 0, 18), 18)?;
        if descriptor.len() < 18 {
            return_errno_with_message!(Errno::EIO, "a truncated device descriptor");
        }
        let vendor_id = u16::from_le_bytes([descriptor[8], descriptor[9]]);
        let product_id = u16::from_le_bytes([descriptor[10], descriptor[11]]);
        let class = descriptor[4];

        let configuration = self.read_configuration(&mut slot, slot_id)?;
        for interface in &configuration.interfaces {
            info!(
                "interface {}: class={:#04x} subclass={:#04x} protocol={:#04x} endpoints={}",
                interface.number,
                interface.class,
                interface.subclass,
                interface.protocol,
                interface.endpoints.len()
            );
            for endpoint in &interface.endpoints {
                info!("endpoint {}", endpoint.describe());
            }
        }
        self.set_configuration(&mut slot, slot_id, configuration.value)?;
        info!(
            "config {}: interfaces={} selected",
            configuration.value,
            configuration.interfaces.len()
        );
        slot.configuration = Some(configuration);

        self.configure_endpoints(&mut slot, slot_id)?;
        match super::storage::probe(self, &mut slot, slot_id) {
            Ok(info) => slot.storage = info,
            Err(error) => warn!("mass-storage probe failed: {:?}", error),
        }
        if let Err(error) = super::block::register(self.clone(), slot_id, &mut slot) {
            warn!("failed to register the block device: {:?}", error);
        }

        let bus_number = 1u8;
        let device_number = slot_id;
        let name = format!("{}-{}", bus_number, port);
        info!(
            "device {}: idVendor={:04x} idProduct={:04x} class={:#04x} speed={} slot={}",
            name, vendor_id, product_id, class, speed, slot_id
        );

        let configuration_value = slot
            .configuration
            .as_ref()
            .map(|configuration| configuration.value)
            .unwrap_or(0);
        let interface_count = slot
            .configuration
            .as_ref()
            .map(|configuration| configuration.interfaces.len())
            .unwrap_or(0);
        let endpoints = slot
            .configuration
            .as_ref()
            .map(|configuration| configuration.endpoint_summary())
            .unwrap_or_default();
        super::sysfs::publish_device(
            &name,
            vendor_id,
            product_id,
            bus_number,
            device_number,
            port,
            speed,
            configuration_value,
            interface_count,
            &endpoints,
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

/// Builds an 8-byte setup packet as the low half of a Setup Stage TRB.
fn control_setup(request_type: u8, request: u8, value: u16, index: u16, length: u16) -> u64 {
    (request_type as u64)
        | ((request as u64) << 8)
        | ((value as u64) << 16)
        | ((index as u64) << 32)
        | ((length as u64) << 48)
}

/// `GET_DESCRIPTOR` for a descriptor kind and index.
fn get_descriptor_setup(kind: u8, index: u8, length: u16) -> u64 {
    control_setup(0x80, 6, ((kind as u16) << 8) | index as u16, 0, length)
}

/// Walks the configuration descriptor's interfaces and endpoints.
fn parse_configuration(bytes: &[u8]) -> Result<Configuration> {
    let mut value = None;
    let mut interfaces: Vec<InterfaceDescriptor> = Vec::new();
    let mut offset = 0usize;
    while offset + 2 <= bytes.len() {
        let length = bytes[offset] as usize;
        let kind = bytes[offset + 1];
        if length < 2 || offset + length > bytes.len() {
            break;
        }
        match kind {
            0x02 if length >= 6 => value = Some(bytes[offset + 5]),
            0x04 if length >= 9 => interfaces.push(InterfaceDescriptor {
                number: bytes[offset + 2],
                class: bytes[offset + 5],
                subclass: bytes[offset + 6],
                protocol: bytes[offset + 7],
                endpoints: Vec::new(),
            }),
            0x05 if length >= 7 => {
                if let Some(interface) = interfaces.last_mut() {
                    interface.endpoints.push(EndpointDescriptor {
                        address: bytes[offset + 2],
                        attributes: bytes[offset + 3],
                        max_packet: u16::from_le_bytes([bytes[offset + 4], bytes[offset + 5]])
                            & 0x7FF,
                        max_burst: 0,
                    });
                }
            }
            // SuperSpeed endpoint companion: bMaxBurst belongs to the endpoint
            // just described, and the EP context must advertise the same.
            0x30 if length >= 6 => {
                if let Some(interface) = interfaces.last_mut()
                    && let Some(endpoint) = interface.endpoints.last_mut()
                {
                    endpoint.max_burst = bytes[offset + 2];
                }
            }
            _ => {}
        }
        offset += length;
    }

    let value = value.ok_or_else(|| {
        Error::with_message(Errno::EIO, "the configuration descriptor is missing")
    })?;
    Ok(Configuration { value, interfaces })
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
