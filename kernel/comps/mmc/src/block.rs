// SPDX-License-Identifier: MPL-2.0

use alloc::{
    format,
    sync::{Arc, Weak},
    vec::Vec,
};
use core::fmt;

use aster_block::{
    BlockDeviceMeta, EXTENDED_DEVICE_ID_ALLOCATOR, PartitionInfo, PartitionNode,
    bio::{BioEnqueueError, BioStatus, BioType, SubmittedBio, bio_segment_pool_init},
};
use device_id::{DeviceId, MinorId};
use ostd::{mm::VmIo, sync::SpinLock};

use crate::{MMC_BLOCK_MAJOR_ID, arch::MmioHost, card::Card};

const DEVICE_MINORS: u32 = 16;

pub(super) fn register(host: MmioHost, card: Card) -> Result<(), ()> {
    let id = DeviceId::new(MMC_BLOCK_MAJOR_ID.get().unwrap().get(), MinorId::new(0));
    let device = Arc::new_cyclic(|weak_self| ReadOnlyMmcBlock {
        state: SpinLock::new((host, card)),
        id,
        partitions: SpinLock::new(None),
        weak_self: weak_self.clone(),
    });
    aster_block::register(device).map_err(|_| ())?;
    bio_segment_pool_init();
    ostd::info!("[mmc] mmcblk0 registered read-only");
    Ok(())
}

struct ReadOnlyMmcBlock {
    state: SpinLock<(MmioHost, Card)>,
    id: DeviceId,
    partitions: SpinLock<Option<Vec<Arc<PartitionNode>>>>,
    weak_self: Weak<Self>,
}

impl fmt::Debug for ReadOnlyMmcBlock {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ReadOnlyMmcBlock")
            .field("name", &"mmcblk0")
            .field("id", &self.id)
            .finish_non_exhaustive()
    }
}

impl aster_block::BlockDevice for ReadOnlyMmcBlock {
    fn enqueue(&self, bio: SubmittedBio) -> Result<(), BioEnqueueError> {
        let status = match bio.type_() {
            BioType::Write => BioStatus::NotSupported,
            BioType::Flush => BioStatus::Complete,
            BioType::Read => self.read_bio(&bio),
        };
        bio.complete(status);
        Ok(())
    }

    fn metadata(&self) -> BlockDeviceMeta {
        BlockDeviceMeta {
            max_nr_segments_per_bio: usize::MAX,
            nr_sectors: self.state.lock().1.nr_sectors() as usize,
        }
    }

    fn name(&self) -> &str {
        "mmcblk0"
    }

    fn id(&self) -> DeviceId {
        self.id
    }

    fn set_partitions(&self, infos: Vec<Option<PartitionInfo>>) {
        let mut partitions = self.partitions.lock();
        if let Some(old) = partitions.take() {
            for partition in old {
                let _ = aster_block::unregister(partition.id());
            }
        }
        let mut new = Vec::new();
        for (position, info) in infos.into_iter().enumerate() {
            let Some(info) = info else { continue };
            let number = position as u32 + 1;
            let id = if number < DEVICE_MINORS {
                DeviceId::new(
                    self.id.major(),
                    MinorId::new(self.id.minor().get() + number),
                )
            } else {
                EXTENDED_DEVICE_ID_ALLOCATOR.get().unwrap().allocate()
            };
            let name = format!("mmcblk0p{}", number);
            let whole = self.weak_self.upgrade().unwrap();
            new.push(Arc::new(PartitionNode::new(id, name, whole, info)));
        }
        for partition in &new {
            let _ = aster_block::register(partition.clone());
        }
        *partitions = Some(new);
    }

    fn partitions(&self) -> Option<Vec<Arc<dyn aster_block::BlockDevice>>> {
        self.partitions.lock().as_ref().map(|partitions| {
            partitions
                .iter()
                .map(|partition| partition.clone() as Arc<dyn aster_block::BlockDevice>)
                .collect()
        })
    }
}

impl ReadOnlyMmcBlock {
    fn read_bio(&self, bio: &SubmittedBio) -> BioStatus {
        let mut lba = bio.sid_range().start.to_raw();
        let mut state = self.state.lock();
        let (host, card) = &mut *state;
        let mut sector = [0u8; 512];
        for segment in bio.segments() {
            if !segment.nbytes().is_multiple_of(512) {
                return BioStatus::IoError;
            }
            for offset in (0..segment.nbytes()).step_by(512) {
                if card.read_sector(host, lba, &mut sector).is_err()
                    || segment.write_bytes(offset, &sector).is_err()
                {
                    return BioStatus::IoError;
                }
                lba += 1;
            }
        }
        BioStatus::Complete
    }
}
