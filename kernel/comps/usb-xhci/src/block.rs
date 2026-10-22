// SPDX-License-Identifier: MPL-2.0

//! The block-device face of a USB mass-storage stick.
//!
//! Once `storage::probe` has read INQUIRY and READ CAPACITY, the endpoint
//! rings move here and every bio becomes one or more SCSI READ(10)/WRITE(10)
//! commands. The device is registered into the block layer and published to
//! devtmpfs as `/dev/sdX`.

use alloc::{string::String, sync::Arc, vec::Vec};
use core::sync::atomic::{AtomicU32, Ordering};

use aster_block::{
    BlockDevice, BlockDeviceMeta, DEVICE_MINORS, MajorIdOwner, acquire_major,
    bio::{BioEnqueueError, BioStatus, BioType, SubmittedBio},
};
use aster_core::prelude::*;
use device_id::{DeviceId, MajorId, MinorId};
use ostd::{
    info,
    mm::HasDaddr,
    sync::{Mutex, SpinLock},
    warn,
};

use super::XhciController;
use super::storage::{self, StorageInfo};
use super::usb::{EndpointState, SlotState};

/// Linux's SCSI disk major.
const SCSI_DISK_MAJOR: u16 = 8;
/// One READ/WRITE(10) carries at most 64 KiB: a single TRB's length field is
/// 17 bits wide.
const MAX_CHUNK: usize = 64 * 1024;

static NEXT_INDEX: AtomicU32 = AtomicU32::new(0);
static MAJOR: SpinLock<Option<MajorIdOwner>> = SpinLock::new(None);

/// Turns a probed stick into `/dev/sdX`.
pub(super) fn register(
    controller: Arc<XhciController>,
    slot_id: u8,
    slot: &mut SlotState,
) -> Result<()> {
    let Some(info) = slot.storage.take() else {
        return Ok(());
    };
    let (Some(bulk_in), Some(bulk_out)) = (slot.bulk_in, slot.bulk_out) else {
        return Ok(());
    };

    // The endpoint rings now belong to the block device.
    let mut endpoints: Vec<Option<Mutex<EndpointState>>> =
        core::mem::take(&mut slot.endpoints).into_iter().map(Some).collect();
    let (Some(bulk_in), Some(bulk_out)) = (endpoints[bulk_in].take(), endpoints[bulk_out].take())
    else {
        return_errno_with_message!(Errno::EIO, "the bulk endpoints vanished");
    };

    let index = NEXT_INDEX.fetch_add(1, Ordering::Relaxed);
    let name = disk_name(index);
    let major = {
        let mut major = MAJOR.lock();
        if major.is_none() {
            let owner = acquire_major(MajorId::new(SCSI_DISK_MAJOR))
                .map_err(|_| Error::with_message(Errno::EBUSY, "major 8 is already taken"))?;
            *major = Some(owner);
        }
        // Every owner in the option is a live major ID.
        let owner: &MajorIdOwner = major.as_ref().unwrap();
        owner.get()
    };
    let id = DeviceId::new(major, MinorId::new(index * DEVICE_MINORS));

    let device = Arc::new(UsbStorageDevice {
        controller,
        slot_id,
        bulk_in,
        bulk_out,
        id,
        name: name.clone(),
        info,
    });

    aster_block::register(device.clone() as Arc<dyn BlockDevice>)
        .map_err(|_| Error::with_message(Errno::EIO, "the block device is already registered"))?;
    aster_core::device::register_hotplug_device(device.clone() as Arc<dyn BlockDevice>)?;

    info!(
        "registered /dev/{}: {} sectors of {} bytes",
        name,
        device.info.blocks,
        device.info.block_size
    );
    Ok(())
}

/// `sda`, `sdb`, … `sdz`, `sdaa`.
fn disk_name(mut index: u32) -> String {
    let mut suffix = Vec::new();
    loop {
        suffix.push((b'a' + (index % 26) as u8) as char);
        index /= 26;
        if index == 0 {
            break;
        }
        index -= 1;
    }
    suffix.reverse();
    let mut name = String::from("sd");
    name.extend(suffix);
    name
}

#[derive(Debug)]
struct UsbStorageDevice {
    controller: Arc<XhciController>,
    slot_id: u8,
    bulk_in: Mutex<EndpointState>,
    bulk_out: Mutex<EndpointState>,
    id: DeviceId,
    name: String,
    info: StorageInfo,
}

impl BlockDevice for UsbStorageDevice {
    fn enqueue(&self, bio: SubmittedBio) -> Result<(), BioEnqueueError> {
        let status = match self.handle(&bio) {
            Ok(()) => BioStatus::Complete,
            Err(error) => {
                warn!("{}: {:?}", self.name, error);
                BioStatus::IoError
            }
        };
        bio.complete(status);
        Ok(())
    }

    fn metadata(&self) -> BlockDeviceMeta {
        BlockDeviceMeta {
            max_nr_segments_per_bio: 64,
            nr_sectors: self.info.blocks as usize,
        }
    }

    fn name(&self) -> &str {
        &self.name
    }

    fn id(&self) -> DeviceId {
        self.id
    }
}

impl UsbStorageDevice {
    fn handle(&self, bio: &SubmittedBio) -> Result<()> {
        match bio.type_() {
            BioType::Flush => Ok(()),
            BioType::Read => self.transfer(bio, true),
            BioType::Write => self.transfer(bio, false),
        }
    }

    fn transfer(&self, bio: &SubmittedBio, in_: bool) -> Result<()> {
        let mut sector = bio.sid_range().start.to_raw();
        for segment in bio.segments() {
            let daddr = segment.dma_slice().daddr() as u64;
            let length = segment.nbytes();
            let mut offset = 0usize;
            while offset < length {
                let chunk = (length - offset).min(MAX_CHUNK);
                let sectors = (chunk / 512) as u16;
                if sectors == 0 {
                    break;
                }
                storage::read_write(
                    &self.controller,
                    self.slot_id,
                    &self.bulk_in,
                    &self.bulk_out,
                    sector,
                    sectors,
                    daddr + offset as u64,
                    in_,
                )?;
                sector += sectors as u64;
                offset += sectors as usize * 512;
            }
        }
        Ok(())
    }
}
