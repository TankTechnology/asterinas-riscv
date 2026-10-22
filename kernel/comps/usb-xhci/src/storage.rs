// SPDX-License-Identifier: MPL-2.0

//! Bulk-Only Transport with a small SCSI subset.
//!
//! Enough of usb-storage to identify and drive a stick: build a Command Block
//! Wrapper, run one optional data stage, read the Command Status Wrapper, and
//! speak INQUIRY, READ CAPACITY(10), READ(10) and WRITE(10).

use alloc::string::String;
use core::sync::atomic::{AtomicU32, Ordering};

use aster_core::prelude::*;
use ostd::{
    info,
    mm::{HasDaddr, VmIo, dma::DmaCoherent},
    sync::Mutex,
};

use super::XhciController;
use super::usb::{EndpointState, SlotState};

/// `"USBC"` in little-endian order.
const BOT_CBW_SIGNATURE: u32 = 0x4342_5355;
/// `"USBS"` in little-endian order.
const BOT_CSW_SIGNATURE: u32 = 0x5342_5355;
const CBW_LENGTH: usize = 31;
const CSW_LENGTH: usize = 13;

static NEXT_TAG: AtomicU32 = AtomicU32::new(1);

/// What the stick told us about itself.
#[derive(Debug)]
pub(super) struct StorageInfo {
    pub vendor: String,
    pub product: String,
    pub blocks: u32,
    pub block_size: u32,
}

impl StorageInfo {
    pub(super) fn capacity_bytes(&self) -> u64 {
        self.blocks as u64 * self.block_size as u64
    }
}

/// Runs INQUIRY and READ CAPACITY on a mass-storage interface.
pub(super) fn probe(
    controller: &XhciController,
    slot: &mut SlotState,
    slot_id: u8,
) -> Result<Option<StorageInfo>> {
    let is_storage = slot
        .configuration
        .as_ref()
        .and_then(|configuration| configuration.interfaces.first())
        .map(|interface| interface.class == 0x08)
        .unwrap_or(false);
    if !is_storage {
        return Ok(None);
    }

    let (Some(bulk_in), Some(bulk_out)) = (slot.bulk_in, slot.bulk_out) else {
        return_errno_with_message!(Errno::EIO, "the mass-storage interface has no bulk pair");
    };
    let bulk_in = &slot.endpoints[bulk_in];
    let bulk_out = &slot.endpoints[bulk_out];

    // The first command after a port reset can come back as UNIT ATTENTION
    // (sense key 6, ASC 0x29); reading the sense data clears it, so retry once.
    let (vendor, product) = match inquiry(controller, slot_id, bulk_in, bulk_out) {
        Ok(value) => value,
        Err(_) => {
            request_sense(controller, slot_id, bulk_in, bulk_out)?;
            inquiry(controller, slot_id, bulk_in, bulk_out)?
        }
    };
    let (blocks, block_size) = match read_capacity(controller, slot_id, bulk_in, bulk_out) {
        Ok(value) => value,
        Err(_) => {
            request_sense(controller, slot_id, bulk_in, bulk_out)?;
            read_capacity(controller, slot_id, bulk_in, bulk_out)?
        }
    };

    let info = StorageInfo {
        vendor,
        product,
        blocks,
        block_size,
    };
    info!(
        "storage on slot {}: vendor={:?} product={:?} blocks={} block_size={} capacity={} bytes",
        slot_id,
        info.vendor,
        info.product,
        info.blocks,
        info.block_size,
        info.capacity_bytes()
    );
    Ok(Some(info))
}

fn inquiry(
    controller: &XhciController,
    slot_id: u8,
    bulk_in: &Mutex<EndpointState>,
    bulk_out: &Mutex<EndpointState>,
) -> Result<(String, String)> {
    let data = DmaCoherent::alloc(1, true)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "no INQUIRY buffer"))?;
    // INQUIRY, allocation length 36: standard data, vendor at 8, product at 16.
    let cb = [0x12u8, 0, 0, 0, 36, 0];
    let transferred = command(
        controller,
        slot_id,
        bulk_in,
        bulk_out,
        &cb,
        36,
        true,
        data.daddr() as u64,
    )?;
    if transferred < 36 {
        return_errno_with_message!(Errno::EIO, "a short INQUIRY response");
    }

    Ok((read_string(&data, 8, 8)?, read_string(&data, 16, 16)?))
}

fn read_capacity(
    controller: &XhciController,
    slot_id: u8,
    bulk_in: &Mutex<EndpointState>,
    bulk_out: &Mutex<EndpointState>,
) -> Result<(u32, u32)> {
    let data = DmaCoherent::alloc(1, true)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "no READ CAPACITY buffer"))?;
    let cb = [0x25u8, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    let transferred = command(
        controller,
        slot_id,
        bulk_in,
        bulk_out,
        &cb,
        8,
        true,
        data.daddr() as u64,
    )?;
    if transferred < 8 {
        return_errno_with_message!(Errno::EIO, "a short READ CAPACITY response");
    }

    let last_lba = read_be_u32(&data, 0)?;
    let block_size = read_be_u32(&data, 4)?;
    Ok((last_lba.saturating_add(1), block_size))
}

/// Asks the device why the previous command failed and logs the sense data.
fn request_sense(
    controller: &XhciController,
    slot_id: u8,
    bulk_in: &Mutex<EndpointState>,
    bulk_out: &Mutex<EndpointState>,
) -> Result<()> {
    let data = DmaCoherent::alloc(1, true)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "no REQUEST SENSE buffer"))?;
    let cb = [0x03u8, 0, 0, 0, 18, 0];
    let transferred = command(
        controller,
        slot_id,
        bulk_in,
        bulk_out,
        &cb,
        18,
        true,
        data.daddr() as u64,
    )?;
    if transferred >= 14 {
        let key = read_u8(&data, 2)? & 0x0F;
        let asc = read_u8(&data, 12)?;
        let ascq = read_u8(&data, 13)?;
        ostd::warn!("sense key={} asc={:#04x} ascq={:#04x}", key, asc, ascq);
    }
    Ok(())
}

/// Runs one SCSI READ(10) or WRITE(10) into the DMA range at `daddr`.
pub(super) fn read_write(
    controller: &XhciController,
    slot_id: u8,
    bulk_in: &Mutex<EndpointState>,
    bulk_out: &Mutex<EndpointState>,
    lba: u64,
    sectors: u16,
    daddr: u64,
    in_: bool,
) -> Result<usize> {
    let length = sectors as usize * 512;
    let mut cb = [0u8; 10];
    cb[0] = if in_ { 0x28 } else { 0x2A };
    cb[2..6].copy_from_slice(&((lba as u32).to_be_bytes()));
    cb[7..9].copy_from_slice(&sectors.to_be_bytes());
    command(
        controller,
        slot_id,
        bulk_in,
        bulk_out,
        &cb,
        length,
        in_,
        daddr,
    )
}

/// Sends one command through the Bulk-Only Transport.
pub(super) fn command(
    controller: &XhciController,
    slot_id: u8,
    bulk_in: &Mutex<EndpointState>,
    bulk_out: &Mutex<EndpointState>,
    cb: &[u8],
    data_length: usize,
    data_in: bool,
    data_daddr: u64,
) -> Result<usize> {
    let tag = NEXT_TAG.fetch_add(1, Ordering::Relaxed);

    let cbw = DmaCoherent::alloc(1, true)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "no CBW buffer"))?;
    write_u32(&cbw, 0, BOT_CBW_SIGNATURE)?;
    write_u32(&cbw, 4, tag)?;
    write_u32(&cbw, 8, data_length as u32)?;
    write_u8(&cbw, 12, if data_in { 0x80 } else { 0x00 })?;
    write_u8(&cbw, 13, 0)?; // LUN 0
    write_u8(&cbw, 14, cb.len() as u8)?;
    for (index, byte) in cb.iter().enumerate() {
        write_u8(&cbw, 15 + index, *byte)?;
    }

    // A Bulk-Only Transport command is a single phase: CBW, data, CSW. Both
    // endpoints stay locked for the whole sequence so two bios can never
    // interleave their phases (the mount path submits reads concurrently).
    let mut out = bulk_out.lock();
    let mut input = bulk_in.lock();

    let sent = controller.bulk_transfer(slot_id, &mut out, cbw.daddr() as u64, CBW_LENGTH)?;
    if sent != CBW_LENGTH {
        return_errno_with_message!(Errno::EIO, "a short CBW write");
    }

    let mut transferred = 0usize;
    if data_length > 0 {
        let endpoint = if data_in { &mut input } else { &mut out };
        transferred = controller.bulk_transfer(slot_id, endpoint, data_daddr, data_length)?;
    }

    let csw = DmaCoherent::alloc(1, true)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "no CSW buffer"))?;
    let got = controller.bulk_transfer(slot_id, &mut input, csw.daddr() as u64, CSW_LENGTH)?;
    if got != CSW_LENGTH {
        return_errno_with_message!(Errno::EIO, "a short CSW read");
    }
    let signature = read_le_u32(&csw, 0)?;
    let csw_tag = read_le_u32(&csw, 4)?;
    let status = read_u8(&csw, 12)?;
    if signature != BOT_CSW_SIGNATURE || csw_tag != tag {
        return_errno_with_message!(Errno::EIO, "the CSW does not match the CBW");
    }
    if status != 0 {
        let residue = read_le_u32(&csw, 8)?;
        ostd::warn!(
            "BOT command {:#04x} failed: status={} residue={} transferred={}",
            cb[0],
            status,
            residue,
            transferred
        );
        return_errno_with_message!(Errno::EIO, "the device reported a failed command");
    }

    Ok(transferred)
}

fn read_string(buffer: &DmaCoherent, offset: usize, length: usize) -> Result<String> {
    let mut text = String::new();
    for index in 0..length {
        let byte = read_u8(buffer, offset + index)?;
        if byte == 0 || (byte == b' ' && text.is_empty()) {
            break;
        }
        text.push(byte as char);
    }
    // Trailing spaces are padding in the fixed-width INQUIRY fields.
    while text.ends_with(' ') {
        text.pop();
    }
    Ok(text)
}

fn write_u8(buffer: &DmaCoherent, offset: usize, value: u8) -> Result<()> {
    buffer
        .write_val(offset, &value)
        .map_err(|_| Error::with_message(Errno::EIO, "the CBW is not writable"))
}

fn read_u8(buffer: &DmaCoherent, offset: usize) -> Result<u8> {
    buffer
        .read_val(offset)
        .map_err(|_| Error::with_message(Errno::EIO, "the response is not readable"))
}

fn write_u32(buffer: &DmaCoherent, offset: usize, value: u32) -> Result<()> {
    for (index, byte) in value.to_le_bytes().iter().enumerate() {
        write_u8(buffer, offset + index, *byte)?;
    }
    Ok(())
}

fn read_le_u32(buffer: &DmaCoherent, offset: usize) -> Result<u32> {
    let bytes = [
        read_u8(buffer, offset)?,
        read_u8(buffer, offset + 1)?,
        read_u8(buffer, offset + 2)?,
        read_u8(buffer, offset + 3)?,
    ];
    Ok(u32::from_le_bytes(bytes))
}

fn read_be_u32(buffer: &DmaCoherent, offset: usize) -> Result<u32> {
    let bytes = [
        read_u8(buffer, offset)?,
        read_u8(buffer, offset + 1)?,
        read_u8(buffer, offset + 2)?,
        read_u8(buffer, offset + 3)?,
    ];
    Ok(u32::from_be_bytes(bytes))
}
