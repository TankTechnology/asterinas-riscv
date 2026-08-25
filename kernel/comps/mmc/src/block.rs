// SPDX-License-Identifier: MPL-2.0

use alloc::{
    format,
    sync::{Arc, Weak},
    vec::Vec,
};
use core::{
    fmt,
    sync::atomic::{AtomicBool, Ordering},
};

use aster_block::{
    BlockDeviceMeta, EXTENDED_DEVICE_ID_ALLOCATOR, PartitionInfo, PartitionNode,
    bio::{BioEnqueueError, BioStatus, BioType, SubmittedBio, bio_segment_pool_init},
};
use device_id::{DeviceId, MinorId};
use ostd::sync::{Mutex, SpinLock};

use crate::{MMC_BLOCK_MAJOR_ID, MMC_WRITE_PARTITION2, arch::MmioHost, card::Card};

const DEVICE_MINORS: u32 = 16;
const P2_START_LBA: u64 = 0x000f_a022;
const P2_NR_SECTORS: u64 = 0x0080_0000;
const P2_END_LBA: u64 = P2_START_LBA + P2_NR_SECTORS;

fn physical_lba(logical_lba: u64, sid_offset: u64, upper_bound: u64) -> Option<u64> {
    let physical = sid_offset.checked_add(logical_lba)?;
    (physical < upper_bound).then_some(physical)
}

fn partition2_geometry_matches(start_lba: u64, nr_sectors: u64) -> bool {
    start_lba == P2_START_LBA && nr_sectors == P2_NR_SECTORS
}

fn partition_write_allowed(
    armed: bool,
    geometry_matches: bool,
    sid_offset: u64,
    first_lba: u64,
    nr_sectors: u64,
) -> bool {
    armed
        && geometry_matches
        && sid_offset == P2_START_LBA
        && nr_sectors != 0
        && sid_offset
            .checked_add(first_lba)
            .and_then(|start| start.checked_add(nr_sectors))
            .is_some_and(|end| end <= P2_END_LBA)
}

pub(super) fn register(host: MmioHost, card: Card) -> Result<(), ()> {
    let id = DeviceId::new(MMC_BLOCK_MAJOR_ID.get().unwrap().get(), MinorId::new(0));
    let partition2_write_armed = MMC_WRITE_PARTITION2.load(Ordering::Relaxed);
    let device = Arc::new_cyclic(|weak_self| MegrezMmcBlock {
        state: Mutex::new((host, card)),
        id,
        partitions: SpinLock::new(None),
        weak_self: weak_self.clone(),
        partition2_write_armed,
        partition2_geometry_matches: AtomicBool::new(false),
    });
    aster_block::register(device).map_err(|_| ())?;
    bio_segment_pool_init();
    if partition2_write_armed {
        ostd::warn!("[mmc] mmcblk0 registered with partition-2 writes armed");
    } else {
        ostd::info!("[mmc] mmcblk0 registered read-only");
    }
    Ok(())
}

struct MegrezMmcBlock {
    state: Mutex<(MmioHost, Card)>,
    id: DeviceId,
    partitions: SpinLock<Option<Vec<Arc<PartitionNode>>>>,
    weak_self: Weak<Self>,
    partition2_write_armed: bool,
    partition2_geometry_matches: AtomicBool,
}

impl fmt::Debug for MegrezMmcBlock {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("MegrezMmcBlock")
            .field("name", &"mmcblk0")
            .field("id", &self.id)
            .finish_non_exhaustive()
    }
}

impl aster_block::BlockDevice for MegrezMmcBlock {
    fn enqueue(&self, bio: SubmittedBio) -> Result<(), BioEnqueueError> {
        let status = match bio.type_() {
            BioType::Write => self.write_bio(&bio),
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
        let geometry_matches = infos.get(1).and_then(Option::as_ref).is_some_and(|info| {
            partition2_geometry_matches(info.start_sector(), info.total_sectors())
        });
        self.partition2_geometry_matches
            .store(geometry_matches, Ordering::Relaxed);
        if self.partition2_write_armed {
            if geometry_matches {
                ostd::warn!("[mmc] exact partition-2 write gate enabled");
            } else {
                ostd::error!("[mmc] partition-2 geometry mismatch; writes remain disabled");
            }
        }
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

impl MegrezMmcBlock {
    fn read_bio(&self, bio: &SubmittedBio) -> BioStatus {
        let logical_lba = bio.sid_range().start.to_raw();
        let mut state = self.state.lock();
        let (host, card) = &mut *state;
        let Some(mut lba) = physical_lba(logical_lba, bio.sid_offset(), card.nr_sectors()) else {
            return BioStatus::IoError;
        };
        let mut sector = [0u8; 512];
        for segment in bio.segments() {
            if !segment.nbytes().is_multiple_of(512) {
                return BioStatus::IoError;
            }
            for offset in (0..segment.nbytes()).step_by(512) {
                if card.read_sector(host, lba, &mut sector).is_err()
                    || segment.write_from_device(offset, &sector).is_err()
                {
                    return BioStatus::IoError;
                }
                lba += 1;
            }
        }
        BioStatus::Complete
    }

    fn write_bio(&self, bio: &SubmittedBio) -> BioStatus {
        let first_lba = bio.sid_range().start.to_raw();
        let nr_sectors = bio.sid_range().end.to_raw().saturating_sub(first_lba);
        if !partition_write_allowed(
            self.partition2_write_armed,
            self.partition2_geometry_matches.load(Ordering::Relaxed),
            bio.sid_offset(),
            first_lba,
            nr_sectors,
        ) {
            return BioStatus::NotSupported;
        }

        let mut state = self.state.lock();
        let (host, card) = &mut *state;
        let Some(mut lba) = physical_lba(first_lba, bio.sid_offset(), card.nr_sectors()) else {
            return BioStatus::IoError;
        };
        let mut sector = [0u8; 512];
        for segment in bio.segments() {
            if !segment.nbytes().is_multiple_of(512) {
                return BioStatus::IoError;
            }
            for offset in (0..segment.nbytes()).step_by(512) {
                if segment.read_for_device(offset, &mut sector).is_err()
                    || card.write_sector(host, lba, &sector).is_err()
                {
                    return BioStatus::IoError;
                }
                lba += 1;
            }
        }
        BioStatus::Complete
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn translates_partition_relative_sectors_once() {
        assert_eq!(
            physical_lba(0, P2_START_LBA, P2_END_LBA),
            Some(P2_START_LBA)
        );
        assert_eq!(
            physical_lba(P2_NR_SECTORS - 1, P2_START_LBA, P2_END_LBA),
            Some(P2_END_LBA - 1)
        );
        assert_eq!(physical_lba(P2_NR_SECTORS, P2_START_LBA, P2_END_LBA), None);
        assert_eq!(physical_lba(u64::MAX, 1, u64::MAX), None);
    }

    #[ktest]
    fn arms_writes_only_for_the_frozen_partition_two_range() {
        assert!(!partition_write_allowed(false, true, P2_START_LBA, 0, 1));
        assert!(!partition_write_allowed(true, false, P2_START_LBA, 0, 1));
        assert!(partition_write_allowed(true, true, P2_START_LBA, 0, 1));
        assert!(partition_write_allowed(
            true,
            true,
            P2_START_LBA,
            P2_NR_SECTORS - 1,
            1
        ));

        for (offset, first, count) in [
            (0, P2_START_LBA, 1),
            (P2_START_LBA - 1, 0, 1),
            (P2_START_LBA + 1, 0, 1),
            (P2_START_LBA, P2_NR_SECTORS, 1),
            (P2_START_LBA, P2_NR_SECTORS - 1, 2),
            (P2_START_LBA, u64::MAX, 2),
        ] {
            assert!(!partition_write_allowed(true, true, offset, first, count));
        }
    }

    #[ktest]
    fn requires_the_exact_partition_two_geometry() {
        assert!(partition2_geometry_matches(P2_START_LBA, P2_NR_SECTORS));
        assert!(!partition2_geometry_matches(
            P2_START_LBA - 1,
            P2_NR_SECTORS
        ));
        assert!(!partition2_geometry_matches(
            P2_START_LBA,
            P2_NR_SECTORS - 1
        ));
    }
}
