// SPDX-License-Identifier: MPL-2.0

//! Bulk-Only Transport with a small SCSI subset.
//!
//! Enough of usb-storage to identify a stick: build a Command Block Wrapper,
//! run one optional data stage, read the Command Status Wrapper, and speak
//! INQUIRY and READ CAPACITY(10). Reads of user data arrive with the block
//! device in the next stage.

use alloc::string::String;
use core::sync::atomic::{AtomicU32, Ordering};

use aster_core::prelude::*;
use ostd::{
    info,
    mm::{VmIo, dma::DmaCoherent},
};

use super::XhciController;
use super::usb::SlotState;

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

    let endpoints = (bulk_in, bulk_out);
    // The first command after a port reset can come back as UNIT ATTENTION
    // (sense key 6, ASC 0x29); reading the sense data clears it, so retry once.
    let (vendor, product) = match inquiry(controller, slot, slot_id, endpoints) {
        Ok(value) => value,
        Err(_) => {
            request_sense(controller, slot, slot_id, endpoints)?;
            inquiry(controller, slot, slot_id, endpoints)?
        }
    };
    let (blocks, block_size) = match read_capacity(controller, slot, slot_id, endpoints) {
        Ok(value) => value,
        Err(_) => {
            request_sense(controller, slot, slot_id, endpoints)?;
            read_capacity(controller, slot, slot_id, endpoints)?
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
    slot: &mut SlotState,
    slot_id: u8,
    endpoints: (usize, usize),
) -> Result<(String, String)> {
    let data = DmaCoherent::alloc(1, true)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "no INQUIRY buffer"))?;
    // INQUIRY, allocation length 36: standard data, vendor at 8, product at 16.
    let cb = [0x12u8, 0, 0, 0, 36, 0];
    let transferred = command(controller, slot, slot_id, endpoints, &cb, 36, true, &data)?;
    if transferred < 36 {
        return_errno_with_message!(Errno::EIO, "a short INQUIRY response");
    }

    Ok((read_string(&data, 8, 8)?, read_string(&data, 16, 16)?))
}

fn read_capacity(
    controller: &XhciController,
    slot: &mut SlotState,
    slot_id: u8,
    endpoints: (usize, usize),
) -> Result<(u32, u32)> {
    let data = DmaCoherent::alloc(1, true)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "no READ CAPACITY buffer"))?;
    let cb = [0x25u8, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    let transferred = command(controller, slot, slot_id, endpoints, &cb, 8, true, &data)?;
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
    slot: &mut SlotState,
    slot_id: u8,
    endpoints: (usize, usize),
) -> Result<()> {
    let data = DmaCoherent::alloc(1, true)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "no REQUEST SENSE buffer"))?;
    let cb = [0x03u8, 0, 0, 0, 18, 0];
    let transferred = command(controller, slot, slot_id, endpoints, &cb, 18, true, &data)?;
    if transferred >= 14 {
        let key = read_u8(&data, 2)? & 0x0F;
        let asc = read_u8(&data, 12)?;
        let ascq = read_u8(&data, 13)?;
        ostd::warn!("sense key={} asc={:#04x} ascq={:#04x}", key, asc, ascq);
    }
    Ok(())
}

/// Sends one command through the Bulk-Only Transport.
fn command(
    controller: &XhciController,
    slot: &mut SlotState,
    slot_id: u8,
    (bulk_in, bulk_out): (usize, usize),
    cb: &[u8],
    data_length: usize,
    data_in: bool,
    data: &DmaCoherent,
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

    let sent = controller.bulk_transfer(slot, slot_id, bulk_out, &cbw, CBW_LENGTH)?;
    if sent != CBW_LENGTH {
        return_errno_with_message!(Errno::EIO, "a short CBW write");
    }

    let mut transferred = 0usize;
    if data_length > 0 {
        let index = if data_in { bulk_in } else { bulk_out };
        transferred = controller.bulk_transfer(slot, slot_id, index, data, data_length)?;
    }

    let csw = DmaCoherent::alloc(1, true)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "no CSW buffer"))?;
    let got = controller.bulk_transfer(slot, slot_id, bulk_in, &csw, CSW_LENGTH)?;
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
