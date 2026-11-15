// SPDX-License-Identifier: MPL-2.0

use alloc::{
    sync::Arc,
    vec,
    vec::Vec,
};
use core::{fmt, sync::atomic::Ordering};

use aster_block::{
    BlockDeviceMeta, PartitionManager, SECTOR_SIZE,
    bio::{BioEnqueueError, BioSegment, BioStatus, BioType, SubmittedBio, bio_segment_pool_init},
};
use device_id::{DeviceId, MinorId};
use ostd::sync::Mutex;

use crate::{
    MMC_BLOCK_MAJOR_ID, MMC_WRITE_PARTITION2, MMC_WRITE_PARTITION3,
    arch::MmioHost,
    card::{CacheState, Card},
    sdhci::HostError,
};

// Match the standard SDHCI 512-KiB request boundary while remaining far below
// the controller's 16-bit block-count limit.
const PIO_TRANSFER_SECTORS: usize = 1024;
const PIO_TRANSFER_BYTES: usize = PIO_TRANSFER_SECTORS * SECTOR_SIZE;
const P2_START_LBA: u64 = 0x000f_a022;
const P2_NR_SECTORS: u64 = 0x0080_0000;
const P2_END_LBA: u64 = P2_START_LBA + P2_NR_SECTORS;
// The data partition added after the boot layout. Its range stays frozen the
// same way: a write is only ever permitted inside this exact window.
const P3_START_LBA: u64 = 0x008f_a022;
const P3_NR_SECTORS: u64 = 0x0e53_0fbd;
const P3_END_LBA: u64 = P3_START_LBA + P3_NR_SECTORS;

fn physical_lba(logical_lba: u64, sid_offset: u64, upper_bound: u64) -> Option<u64> {
    let physical = sid_offset.checked_add(logical_lba)?;
    (physical < upper_bound).then_some(physical)
}

fn partition_write_allowed(
    armed: bool,
    partition_start_lba: u64,
    partition_end_lba: u64,
    sid_offset: u64,
    first_lba: u64,
    nr_sectors: u64,
) -> bool {
    // `sid_offset` is the start sector of the partition the bio was addressed
    // through, so requiring it to equal the frozen window start proves that the
    // write enters the device through that exact partition.
    armed
        && sid_offset == partition_start_lba
        && nr_sectors != 0
        && sid_offset
            .checked_add(first_lba)
            .and_then(|start| start.checked_add(nr_sectors))
            .is_some_and(|end| end <= partition_end_lba)
}

fn pio_transfer_buffer_len(total_len: usize) -> usize {
    total_len.min(PIO_TRANSFER_BYTES)
}

fn pio_transfer_buffer(total_len: usize) -> Vec<u8> {
    vec![0u8; pio_transfer_buffer_len(total_len)]
}

pub(super) fn register(host: MmioHost, card: Card) -> Result<(), ()> {
    let id = DeviceId::new(MMC_BLOCK_MAJOR_ID.get().unwrap().get(), MinorId::new(0));
    let partition2_write_armed = MMC_WRITE_PARTITION2.load(Ordering::Relaxed);
    let partition3_write_armed = MMC_WRITE_PARTITION3.load(Ordering::Relaxed);
    let device = Arc::new(MegrezMmcBlock {
        state: Mutex::new((host, card, None)),
        id,
        partition_manager: PartitionManager::new(),
        partition2_write_armed,
        partition3_write_armed,
    });
    aster_block::register(device).map_err(|_| ())?;
    bio_segment_pool_init();
    if partition2_write_armed {
        ostd::warn!("[mmc] mmcblk0 registered with partition-2 writes armed");
    } else {
        ostd::info!("[mmc] mmcblk0 registered read-only");
    }
    if partition3_write_armed {
        ostd::warn!("[mmc] mmcblk0 registered with partition-3 writes armed");
    }
    Ok(())
}

struct MegrezMmcBlock {
    state: Mutex<(MmioHost, Card, Option<CacheState>)>,
    id: DeviceId,
    partition_manager: PartitionManager,
    partition2_write_armed: bool,
    partition3_write_armed: bool,
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
            BioType::Flush => self.flush_bio(),
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

    fn partition_manager(&self) -> Option<&PartitionManager> {
        Some(&self.partition_manager)
    }
}

impl MegrezMmcBlock {
    fn cache_state(
        host: &mut MmioHost,
        card: Card,
        state: &mut Option<CacheState>,
    ) -> Result<CacheState, HostError> {
        if let Some(state) = state {
            return Ok(*state);
        }
        let detected = card.detect_cache(host)?;
        ostd::info!("[mmc] SD cache state: {:?}", detected);
        *state = Some(detected);
        Ok(detected)
    }

    fn flush_bio(&self) -> BioStatus {
        let mut state = self.state.lock();
        let (host, card, cache) = &mut *state;
        let result =
            Self::cache_state(host, *card, cache).and_then(|cache| card.flush_cache(host, cache));
        match result {
            Ok(()) => BioStatus::Complete,
            Err(error) => {
                ostd::error!("[mmc] SD cache flush failed: {:?}", error);
                BioStatus::IoError
            }
        }
    }

    fn read_bio(&self, bio: &SubmittedBio) -> BioStatus {
        let logical_lba = bio.sid_range().start.to_raw();
        let mut state = self.state.lock();
        let (host, card, _) = &mut *state;
        let Some(mut lba) = physical_lba(logical_lba, bio.sid_offset(), card.nr_sectors()) else {
            return BioStatus::IoError;
        };

        let total_len: usize = bio.segments().iter().map(|segment| segment.nbytes()).sum();
        if !total_len.is_multiple_of(SECTOR_SIZE) {
            return BioStatus::IoError;
        }
        // Gather as many segments as fit into one controller request instead
        // of issuing one command per segment. A writeback batch can therefore
        // reach the card as a single multi-block read.
        let mut transfer = pio_transfer_buffer(total_len);
        let mut chunks: Vec<(usize, usize, usize)> = Vec::new();
        let mut filled = 0;
        for (index, segment) in bio.segments().iter().enumerate() {
            if !segment.nbytes().is_multiple_of(SECTOR_SIZE) {
                return BioStatus::IoError;
            }
            let mut offset = 0;
            while offset < segment.nbytes() {
                let length = (segment.nbytes() - offset).min(transfer.len() - filled);
                chunks.push((index, offset, length));
                offset += length;
                filled += length;
                if filled == transfer.len() {
                    if !Self::read_and_scatter(
                        host,
                        *card,
                        &mut lba,
                        &mut transfer,
                        &mut chunks,
                        bio.segments(),
                    ) {
                        return BioStatus::IoError;
                    }
                    filled = 0;
                }
            }
        }
        if filled > 0
            && !Self::read_and_scatter(
                host,
                *card,
                &mut lba,
                &mut transfer[..filled],
                &mut chunks,
                bio.segments(),
            )
        {
            return BioStatus::IoError;
        }
        BioStatus::Complete
    }

    /// Reads one buffered request from the card and scatters the bytes into the
    /// segments recorded in `chunks`, in order.
    fn read_and_scatter(
        host: &mut MmioHost,
        card: Card,
        lba: &mut u64,
        transfer: &mut [u8],
        chunks: &mut Vec<(usize, usize, usize)>,
        segments: &[BioSegment],
    ) -> bool {
        if card.read_sectors(host, *lba, transfer).is_err() {
            return false;
        }
        let mut position = 0;
        for (index, offset, length) in chunks.drain(..) {
            if segments[index]
                .write_from_device(offset, &transfer[position..position + length])
                .is_err()
            {
                return false;
            }
            position += length;
        }
        *lba += (transfer.len() / SECTOR_SIZE) as u64;
        true
    }

    fn write_bio(&self, bio: &SubmittedBio) -> BioStatus {
        let first_lba = bio.sid_range().start.to_raw();
        let nr_sectors = bio.sid_range().end.to_raw().saturating_sub(first_lba);
        let p2_allowed = partition_write_allowed(
            self.partition2_write_armed,
            P2_START_LBA,
            P2_END_LBA,
            bio.sid_offset(),
            first_lba,
            nr_sectors,
        );
        let p3_allowed = partition_write_allowed(
            self.partition3_write_armed,
            P3_START_LBA,
            P3_END_LBA,
            bio.sid_offset(),
            first_lba,
            nr_sectors,
        );
        if !p2_allowed && !p3_allowed {
            return BioStatus::NotSupported;
        }

        let mut state = self.state.lock();
        let (host, card, cache) = &mut *state;
        if let Err(error) = Self::cache_state(host, *card, cache) {
            ostd::error!("[mmc] refusing SD write without cache state: {:?}", error);
            return BioStatus::IoError;
        }
        let Some(mut lba) = physical_lba(first_lba, bio.sid_offset(), card.nr_sectors()) else {
            return BioStatus::IoError;
        };

        let total_len: usize = bio.segments().iter().map(|segment| segment.nbytes()).sum();
        if !total_len.is_multiple_of(SECTOR_SIZE) {
            return BioStatus::IoError;
        }
        let mut transfer = pio_transfer_buffer(total_len);
        let mut filled = 0;
        for segment in bio.segments() {
            if !segment.nbytes().is_multiple_of(SECTOR_SIZE) {
                return BioStatus::IoError;
            }
            let mut offset = 0;
            while offset < segment.nbytes() {
                let length = (segment.nbytes() - offset).min(transfer.len() - filled);
                if segment
                    .read_for_device(offset, &mut transfer[filled..filled + length])
                    .is_err()
                {
                    return BioStatus::IoError;
                }
                offset += length;
                filled += length;
                if filled == transfer.len() {
                    if card.write_sectors(host, lba, &transfer).is_err() {
                        return BioStatus::IoError;
                    }
                    lba += (filled / SECTOR_SIZE) as u64;
                    filled = 0;
                }
            }
        }
        if filled > 0 && card.write_sectors(host, lba, &transfer[..filled]).is_err() {
            return BioStatus::IoError;
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
        assert!(!partition_write_allowed(
            false,
            P2_START_LBA,
            P2_END_LBA,
            P2_START_LBA,
            0,
            1
        ));
        assert!(partition_write_allowed(
            true,
            P2_START_LBA,
            P2_END_LBA,
            P2_START_LBA,
            0,
            1
        ));
        assert!(partition_write_allowed(
            true,
            P2_START_LBA,
            P2_END_LBA,
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
            assert!(!partition_write_allowed(
                true,
                P2_START_LBA,
                P2_END_LBA,
                offset,
                first,
                count
            ));
        }
    }

    #[ktest]
    fn partition_three_has_its_own_frozen_range() {
        assert!(partition_write_allowed(
            true,
            P3_START_LBA,
            P3_END_LBA,
            P3_START_LBA,
            0,
            1
        ));
        assert!(partition_write_allowed(
            true,
            P3_START_LBA,
            P3_END_LBA,
            P3_START_LBA,
            P3_NR_SECTORS - 1,
            1
        ));
        // A window armed for p2 never covers p3 and vice versa.
        assert!(!partition_write_allowed(
            true,
            P2_START_LBA,
            P2_END_LBA,
            P3_START_LBA,
            0,
            1
        ));
        assert!(!partition_write_allowed(
            true,
            P3_START_LBA,
            P3_END_LBA,
            P2_START_LBA,
            0,
            1
        ));
        assert!(!partition_write_allowed(
            true,
            P3_START_LBA,
            P3_END_LBA,
            P3_START_LBA,
            P3_NR_SECTORS,
            1
        ));
    }

    #[ktest]
    fn rejects_writes_that_do_not_enter_through_the_frozen_partition() {
        // The whole disk addresses sectors with a zero offset; reaching the
        // window's physical range that way must not be accepted.
        assert!(!partition_write_allowed(
            true,
            P2_START_LBA,
            P2_END_LBA,
            0,
            P2_START_LBA,
            1
        ));
        // A partition table entry with a different start sector is refused.
        assert!(!partition_write_allowed(
            true,
            P2_START_LBA,
            P2_END_LBA,
            P2_START_LBA - 1,
            0,
            1
        ));
    }

    #[ktest]
    fn bounds_pio_batches_at_the_standard_sdhci_request_size() {
        assert_eq!(PIO_TRANSFER_SECTORS, 1024);
        assert_eq!(pio_transfer_buffer_len(512), 512);
        assert_eq!(pio_transfer_buffer_len(4096), 4096);
        assert_eq!(pio_transfer_buffer_len(1024 * 1024), 512 * 1024);
    }
}
