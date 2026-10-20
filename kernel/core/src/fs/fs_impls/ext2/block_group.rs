// SPDX-License-Identifier: MPL-2.0

//! An independent allocation domain within an ext2 filesystem.
//!
//! An ext2 volume is partitioned into equally sized block groups so that
//! related blocks and inodes can be allocated close together on disk,
//! reducing seek distance and improving locality.
//!
//! # Invariants
//!
//! - All group-local bitmap indices fit in `u16` because the superblock
//!   enforces `nr_blocks_per_group <= block_size * 8` (max 32768 for 4 KiB
//!   blocks), and `nr_inodes_per_group` is similarly bounded.
//! - The group descriptor and both bitmaps are protected by a single
//!   `RwMutex`. Block and inode allocation/free update bitmaps and group
//!   counters in one critical section.
//! - Metadata block pointers (block bitmap, inode bitmap, inode table)
//!   are validated at mount time and are guaranteed to lie within the
//!   group's block range and be marked as allocated.
//!
//! # Locking
//!
//! `BlockGroup` uses two independent locks:
//!
//! - `metadata` — protects the group descriptor and both allocation
//!   bitmaps. Held briefly during alloc/free operations.
//! - `inode_cache` — protects the per-group live inode map. Uses
//!   double-checked locking (read then promote to write on miss).

use core::fmt;

use aster_block::bio::BioCompleteFn;
use ostd::const_assert;

use super::{
    checksum,
    fs::Ext2,
    inode::{
        Inode, InodeDesc, InodeTimeExtras, RawInode, EXTRA_ATIME_OFFSET, EXTRA_CRTIME_END,
        EXTRA_CRTIME_EXTRA_OFFSET, EXTRA_CRTIME_OFFSET, EXTRA_CTIME_OFFSET, EXTRA_ISIZE_OFFSET,
        EXTRA_MTIME_OFFSET, EXTRA_TIMES_END,
    },
    prelude::*,
    super_block::{FeatureInCompatSet, SuperBlock},
};
use crate::{fs::utils::IdBitmap, vm::page_cache::PageRun};

/// Represents one block group in an ext2 filesystem.
///
/// A block group is a filesystem-local allocation domain with its own block
/// bitmap, inode bitmap, and inode table. Keeping related blocks and inodes in
/// the same group improves locality on disk.
pub(super) struct BlockGroup {
    /// Block group index (0-based).
    group_idx: usize,
    /// Weak filesystem context for journal-aware metadata writeback.
    fs: Weak<Ext2>,
    /// Group descriptor and bitmaps, protected by a single lock.
    metadata: RwMutex<BlockGroupMetadata>,
    /// Cached geometry: first filesystem-wide block number of this group.
    first_block: u32,
    /// Cached geometry: last filesystem-wide block number of this group.
    last_block: u32,
    /// Cached geometry: inode table blocks per group.
    nr_inode_table_blocks_per_group: u32,
    /// Cached geometry: inodes per group.
    nr_inodes_per_group: u32,
    /// Cached geometry: inode size in bytes.
    inode_size: usize,
    /// Cached geometry: on-disk size of one group descriptor.
    desc_size: usize,
    /// Whether metadata blocks carry CRC32C checksums.
    metadata_csum: bool,
    /// Seed for the metadata checksums.
    csum_seed: u32,
    /// Bytes covered by the block bitmap checksum.
    block_bitmap_csum_len: usize,
    /// Bytes covered by the inode bitmap checksum.
    inode_bitmap_csum_len: usize,
    /// Inode table page cache backend.
    _inode_table_backend: Arc<InodeTableBackend>,
    /// Inode table page cache.
    inode_table_cache: PageCache,
    /// Per-group inode cache keyed by group-local inode index.
    ///
    /// Ext2 keeps this cache locally because the VFS layer does not provide
    /// a shared inode cache for filesystem implementations.
    inode_cache: RwMutex<BTreeMap<u16, Arc<Inode>>>,
}

impl Debug for BlockGroup {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("BlockGroup")
            .field("group_idx", &self.group_idx)
            .finish()
    }
}

impl BlockGroup {
    /// Loads a block group from the descriptor table.
    ///
    /// Caches per-group geometry from `SuperBlock` at load time.
    pub(super) fn load(
        group_descs: &USegment,
        group_idx: usize,
        sb: &SuperBlock,
        block_device: Arc<dyn BlockDevice>,
        fs: Weak<Ext2>,
    ) -> Result<Self> {
        let desc_size = sb.desc_size() as usize;
        let group_desc = read_group_descriptor(group_descs, group_idx, desc_size)?;

        // Cache geometry from `SuperBlock` at load time.
        let first_block_no = sb.group_first_block_no(group_idx);
        let last_block_no = sb.group_last_block_no(group_idx);
        let nr_inode_table_blocks_per_group = sb.nr_inode_table_blocks_per_group();
        let nr_inodes_per_group = sb.nr_inodes_per_group();
        let nr_block_groups = sb.nr_block_groups() as usize;
        let nr_inodes_in_group = if group_idx == nr_block_groups - 1 {
            sb.total_inodes() - (group_idx as u32) * nr_inodes_per_group
        } else {
            nr_inodes_per_group
        };
        let inode_size = sb.inode_size();

        group_desc.validate_free_counts(last_block_no - first_block_no + 1, nr_inodes_in_group)?;

        // Load and validate bitmaps once during mount, keep them cached in memory.
        let block_bitmap = Self::load_block_bitmap(
            block_device.as_ref(),
            first_block_no,
            last_block_no,
            &group_desc,
        )?;
        let inode_bitmap =
            Self::load_inode_bitmap(block_device.as_ref(), nr_inodes_per_group, &group_desc)?;

        group_desc.validate_metadata_blocks(
            &block_bitmap,
            first_block_no,
            last_block_no,
            nr_inode_table_blocks_per_group,
            sb.feature_incompat()
                .contains(FeatureInCompatSet::FLEX_BG),
        )?;

        // Create `PageCache` for inode table backed by `InodeTableBackend`.
        let raw_inodes_size = (nr_inodes_per_group as usize) * inode_size;
        let backend = Arc::new(InodeTableBackend {
            inode_table_bid: group_desc.inode_table_bid,
            raw_inodes_size,
            block_device: block_device.clone(),
            fs: fs.clone(),
        });
        let inode_table_cache =
            PageCache::new_with_backend(raw_inodes_size, Arc::downgrade(&backend) as _)?;

        Ok(Self {
            group_idx,
            fs,
            metadata: RwMutex::new(BlockGroupMetadata {
                desc: Dirty::new(group_desc),
                block_bitmap: Dirty::new(block_bitmap),
                inode_bitmap: Dirty::new(inode_bitmap),
            }),
            first_block: first_block_no,
            last_block: last_block_no,
            nr_inode_table_blocks_per_group,
            nr_inodes_per_group,
            inode_size,
            desc_size,
            metadata_csum: sb.has_metadata_csum(),
            csum_seed: sb.checksum_seed(),
            block_bitmap_csum_len: (sb.nr_blocks_per_group() / 8) as usize,
            inode_bitmap_csum_len: (sb.nr_inodes_per_group() / 8) as usize,
            _inode_table_backend: backend,
            inode_table_cache,
            inode_cache: RwMutex::new(BTreeMap::new()),
        })
    }

    /// Re-reads the metadata cached during mount after a journal replay.
    ///
    /// `replay_journal` writes replayed home blocks directly to the device,
    /// bypassing this group's bitmap, descriptor and inode-table caches. The
    /// caches must be reloaded before the next commit, otherwise a stale
    /// bitmap would be staged and undo the recovery.
    pub(super) fn reload_after_replay(&self, group_descs: &USegment) -> Result<()> {
        let fs = self
            .fs
            .upgrade()
            .ok_or_else(|| Error::with_message(Errno::EIO, "filesystem already dropped"))?;

        let group_desc = read_group_descriptor(group_descs, self.group_idx, self.desc_size)?;
        let block_bitmap = Self::load_block_bitmap(
            fs.block_device(),
            self.first_block,
            self.last_block,
            &group_desc,
        )?;
        let inode_bitmap =
            Self::load_inode_bitmap(fs.block_device(), self.nr_inodes_per_group, &group_desc)?;

        *self.metadata.write() = BlockGroupMetadata {
            desc: Dirty::new(group_desc),
            block_bitmap: Dirty::new(block_bitmap),
            inode_bitmap: Dirty::new(inode_bitmap),
        };

        // Inode-table pages loaded before the replay are stale too. At mount
        // time they are clean (no metadata has been written yet), so evicting
        // them is enough; the next read pulls the replayed content.
        let inode_table_size = self.nr_inodes_per_group as usize * self.inode_size;
        self.inode_table_cache.evict_range(0..inode_table_size)?;
        self.inode_cache.write().clear();

        Ok(())
    }

    /// Returns the block group index.
    pub(super) fn group_idx(&self) -> usize {
        self.group_idx
    }

    /// Returns whether an inode is marked allocated in this group.
    pub(super) fn is_inode_allocated(&self, ino: Ext2Ino) -> bool {
        let inode_idx = self.inode_idx_in_group(ino);
        self.metadata.read().inode_bitmap.is_allocated(inode_idx)
    }

    /// Looks up an allocated inode by inode number.
    pub(super) fn lookup_inode(&self, ino: Ext2Ino, fs: Weak<Ext2>) -> Result<Arc<Inode>> {
        let inode_idx = self.inode_idx_in_group(ino);

        // Fast path: cache hit under read lock.
        if let Some(inode) = self.inode_cache.read().get(&inode_idx) {
            return Ok(inode.clone());
        }

        // Slow path: check allocation bitmap, then load from disk.
        if !self.is_inode_allocated(ino) {
            return_errno!(Errno::ENOENT);
        }

        // Revalidate under write lock since another thread may have inserted
        // the inode between the read and write lock acquisition.
        let mut inode_cache = self.inode_cache.write();
        if let Some(inode) = inode_cache.get(&inode_idx) {
            return Ok(inode.clone());
        }

        let inode_desc = self.read_inode_desc(inode_idx)?;
        let inode_desc = Dirty::new(inode_desc);
        let inode = Inode::new(ino, inode_desc.type_(), inode_desc, self.group_idx, fs);
        inode_cache.insert(inode_idx, inode.clone());
        Ok(inode)
    }

    /// Inserts an inode into this group's in-memory cache.
    pub(super) fn insert_inode(&self, inode: Arc<Inode>) {
        let inode_idx = self.inode_idx_in_group(inode.ino());
        self.inode_cache.write().insert(inode_idx, inode);
    }

    /// Removes an inode from this group's in-memory cache.
    pub(super) fn remove_inode(&self, ino: Ext2Ino) -> Option<Arc<Inode>> {
        let inode_idx = self.inode_idx_in_group(ino);
        self.inode_cache.write().remove(&inode_idx)
    }

    /// Flushes the data and indirect metadata of every cached inode.
    ///
    /// This must run before the transaction that stages the inode descriptors
    /// is committed: ext4 orders data before metadata (`data=ordered`), and a
    /// committed descriptor pointing at a data block that never reached the
    /// device is exactly the corruption the checker reports as a bad extent.
    pub(super) fn flush_inode_data(&self) -> Result<()> {
        // Clone the `Arc` handles under the read lock, then drop the lock before
        // calling `sync_all`. Otherwise `sync_all` acquires `inner.write()`
        // while we still hold `inode_cache.read()`, creating a lock-order
        // inversion with `create`/`unlink`/`rmdir`/`rename` which take
        // `inner.write()` first, then `inode_cache.write()`.
        let inodes: Vec<Arc<Inode>> = self.inode_cache.read().values().cloned().collect();
        for inode in inodes {
            inode.sync_all()?;
        }
        Ok(())
    }

    /// Stages the group's allocation metadata for the next commit.
    ///
    /// Nothing reaches the device here. The caller commits the staged set as
    /// one journal transaction, so the bitmaps, the group descriptor and the
    /// inode table blocks become durable together instead of on four
    /// independent schedules.
    pub(super) fn stage_metadata(&self, group_descs: &USegment) -> Result<StagedGroupMetadata> {
        let fs = self
            .fs
            .upgrade()
            .ok_or_else(|| Error::with_message(Errno::EIO, "filesystem already dropped"))?;

        if self.metadata_csum {
            // The bitmap checksums stored in the descriptor must describe
            // exactly the bytes that are about to be written.
            let mut metadata = self.metadata.write();
            if metadata.block_bitmap.is_dirty() {
                let crc = checksum::crc32c(
                    self.csum_seed,
                    &metadata.block_bitmap.as_bytes()[..self.block_bitmap_csum_len],
                );
                metadata.desc.set_block_bitmap_csum(crc, self.desc_size);
            }
            if metadata.inode_bitmap.is_dirty() {
                let crc = checksum::crc32c(
                    self.csum_seed,
                    &metadata.inode_bitmap.as_bytes()[..self.inode_bitmap_csum_len],
                );
                metadata.desc.set_inode_bitmap_csum(crc, self.desc_size);
            }
        }

        let block_bitmap = self.stage_bitmap(&fs, true)?;
        let inode_bitmap = self.stage_bitmap(&fs, false)?;

        let descriptor = {
            let metadata = self.metadata.read();
            if metadata.desc.is_dirty() {
                let bytes = metadata.desc.encode(
                    self.group_idx,
                    self.desc_size,
                    self.csum_seed,
                    self.metadata_csum,
                );
                let offset = self.group_idx * self.desc_size;
                group_descs.write_bytes(offset, &bytes[..self.desc_size])?;
                Some(*metadata.desc)
            } else {
                None
            }
        };

        Ok(StagedGroupMetadata {
            block_bitmap,
            inode_bitmap,
            descriptor,
        })
    }

    /// Clears the dirty flags of exactly the state that was committed.
    ///
    /// A bitmap or descriptor that changed between staging and the successful
    /// commit is left dirty so the next commit writes the newer contents.
    pub(super) fn clear_staged(&self, staged: &StagedGroupMetadata) {
        let mut metadata = self.metadata.write();

        if let Some(payload) = &staged.block_bitmap
            && metadata.block_bitmap.as_bytes() == payload.as_slice()
        {
            metadata.block_bitmap.clear_dirty();
        }
        if let Some(payload) = &staged.inode_bitmap
            && metadata.inode_bitmap.as_bytes() == payload.as_slice()
        {
            metadata.inode_bitmap.clear_dirty();
        }
        if let Some(snapshot) = &staged.descriptor
            && *metadata.desc == *snapshot
        {
            metadata.desc.clear_dirty();
        }
    }

    /// Syncs the inode table back to the device.
    pub(super) fn sync_inode_table(&self) -> Result<()> {
        // TODO: support sync specific inode with inode number.
        let size = self.nr_inodes_per_group as usize * self.inode_size;
        let range = 0..size;
        self.inode_table_cache.flush_range(range)
    }

    /// Returns a read guard over the combined group metadata.
    #[cfg(ktest)]
    pub(super) fn metadata(&self) -> RwMutexReadGuard<'_, BlockGroupMetadata> {
        self.metadata.read()
    }

    /// Returns the first filesystem-wide block number of this group.
    pub(super) fn first_block(&self) -> u32 {
        self.first_block
    }

    /// Returns the last filesystem-wide block number of this group.
    pub(super) fn last_block(&self) -> u32 {
        self.last_block
    }

    /// Returns the number of free blocks in this group.
    pub(super) fn free_blocks_count(&self) -> u16 {
        self.metadata.read().desc.free_blocks_count
    }

    /// Returns the number of free inodes in this group.
    pub(super) fn free_inodes_count(&self) -> u16 {
        self.metadata.read().desc.free_inodes_count
    }

    /// Returns whether the group descriptor has been modified since the last writeback.
    pub(super) fn is_desc_dirty(&self) -> bool {
        self.metadata.read().desc.is_dirty()
    }

    /// Attempts to allocate up to `count` contiguous blocks within this group.
    ///
    /// Returns `Ok(range)` with filesystem-wide block numbers on success, or an empty range
    /// if no allocatable blocks are available. Returns `Err(EIO)` on bitmap corruption.
    pub(super) fn alloc_blocks(&self, count: u32, sb_free_blocks: u32) -> Result<Range<Ext2Bid>> {
        let group_size = self.last_block - self.first_block + 1;
        debug_assert!(group_size <= IdBitmap::capacity() as u32);

        let mut metadata = self.metadata.write();

        let mut requested_count = count
            .min(group_size)
            .min(metadata.desc.free_blocks_count as u32)
            .min(sb_free_blocks) as u16;

        // TODO: Improve bitmap allocation to reduce fragmentation (e.g., find the
        // first free block directly instead of retrying with smaller counts).
        let mut allocated_range = None;
        while requested_count > 0 {
            let candidate_range = metadata.block_bitmap.alloc_consecutive(requested_count);
            if candidate_range.is_some() {
                allocated_range = candidate_range;
                break;
            }
            requested_count /= 2;
        }

        let Some(range) = allocated_range else {
            if metadata.desc.free_blocks_count > 0 {
                return_errno_with_message!(Errno::EIO, "block bitmap corruption detected");
            }
            return Ok(0..0);
        };

        let range_start = range.start as u32;
        let alloc_count = range.len() as u32;

        let abs_range =
            (self.first_block + range_start)..(self.first_block + range_start + alloc_count);
        if self.overlaps_system_zone_with(&metadata.desc, abs_range)
            || (metadata.desc.free_blocks_count as u32) < alloc_count
            || sb_free_blocks < alloc_count
        {
            metadata.block_bitmap.free_consecutive(range);
            return_errno_with_message!(Errno::EIO, "block bitmap corruption detected");
        }

        let alloc_count = alloc_count as u16;
        debug_assert!(metadata.desc.free_blocks_count >= alloc_count);
        metadata.desc.free_blocks_count -= alloc_count;

        let range_start_block = self.first_block + range.start as u32;
        let range_end_block = self.first_block + range.end as u32;
        Ok(range_start_block..range_end_block)
    }

    /// Frees a range of blocks within this group.
    ///
    /// Frees a contiguous range of group-relative block bits.
    /// Returns the number of blocks actually freed.
    pub(super) fn free_blocks(&self, bit_range: Range<u32>) -> Result<u32> {
        let start_bit = bit_range.start;
        let group_count = bit_range.len() as u32;
        // Validate system zone overlap using filesystem-wide coordinates.
        let abs_range = (self.first_block + start_bit)..(self.first_block + bit_range.end);

        let mut metadata = self.metadata.write();

        if self.overlaps_system_zone_with(&metadata.desc, abs_range) {
            return_errno_with_message!(Errno::EIO, "freeing blocks in system zone");
        }

        // Clear bits one by one and count only allocated-to-free transitions.
        let range_start = start_bit as u16;
        let range_end = (start_bit + group_count) as u16;
        let mut actually_freed: u32 = 0;
        for block_bit in range_start..range_end {
            if !metadata.block_bitmap.is_allocated(block_bit) {
                warn!(
                    "free_blocks: bit already cleared for block {}",
                    self.first_block + start_bit + (block_bit - range_start) as u32
                );
            } else {
                metadata.block_bitmap.free(block_bit);
                actually_freed += 1;
            }
        }

        // Persistent in-memory bitmap cache; writeback is deferred to sync_metadata.

        let freed_count = actually_freed as u16;
        metadata.desc.free_blocks_count = metadata
            .desc
            .free_blocks_count
            .checked_add(freed_count)
            .ok_or_else(|| Error::with_message(Errno::EIO, "free block count overflow in group"))?;

        Ok(actually_freed)
    }

    /// Attempts to allocate one inode within this group.
    ///
    /// Returns `Some(inode_idx)` with the 0-based group-relative inode index,
    /// or `None` if no free inode is available in this group.
    pub(super) fn alloc_ino(&self, inode_type: InodeType) -> Result<Option<Ext2Ino>> {
        let mut metadata = self.metadata.write();

        if metadata.desc.free_inodes_count == 0 {
            return Ok(None);
        }
        if inode_type.is_directory() && metadata.desc.used_dirs_count == u16::MAX {
            return_errno_with_message!(Errno::EIO, "group used directory counter overflow");
        }

        let Some(inode_idx) = metadata.inode_bitmap.alloc() else {
            return Ok(None);
        };

        metadata.desc.free_inodes_count = metadata
            .desc
            .free_inodes_count
            .checked_sub(1)
            .ok_or_else(|| Error::with_message(Errno::EIO, "group free inode counter underflow"))?;
        if inode_type.is_directory() {
            metadata.desc.used_dirs_count += 1;
        }
        // Keep `bg_itable_unused` consistent with the allocation. Linux uses
        // it as a hint for the unused tail of the inode table; e2fsck rejects
        // an allocated inode that lies inside the declared unused area.
        let unused_tail = (self.nr_inodes_per_group as u32)
            .saturating_sub(inode_idx as u32 + 1)
            .min(u16::MAX as u32) as u16;
        if metadata.desc.itable_unused > unused_tail {
            metadata.desc.itable_unused = unused_tail;
        }

        Ok(Some(inode_idx as Ext2Ino))
    }

    /// Frees one inode within this group.
    ///
    /// Returns `true` if the allocation state transitioned allocated-to-free,
    /// `false` if it was already free (logs warning).
    pub(super) fn free_inode(&self, ino: Ext2Ino, inode_type: InodeType) -> Result<bool> {
        let inode_idx = self.inode_idx_in_group(ino);
        let mut metadata = self.metadata.write();

        if !metadata.inode_bitmap.is_allocated(inode_idx) {
            warn!("free_inode: inode idx {} already freed", inode_idx);
            return Ok(false);
        }

        let free_inodes_count = metadata
            .desc
            .free_inodes_count
            .checked_add(1)
            .ok_or_else(|| Error::with_message(Errno::EIO, "group free inode counter overflow"))?;
        let used_dirs_count = if inode_type.is_directory() {
            Some(
                metadata
                    .desc
                    .used_dirs_count
                    .checked_sub(1)
                    .ok_or_else(|| {
                        Error::with_message(Errno::EIO, "group used directory counter underflow")
                    })?,
            )
        } else {
            None
        };

        metadata.inode_bitmap.free(inode_idx);
        metadata.desc.free_inodes_count = free_inodes_count;
        if let Some(used_dirs_count) = used_dirs_count {
            metadata.desc.used_dirs_count = used_dirs_count;
        }

        Ok(true)
    }

    /// Reads an inode descriptor from the group's inode table `PageCache`.
    ///
    /// `inode_idx` is the 0-based inode index within this group.
    pub(super) fn read_inode_desc(&self, inode_idx: u16) -> Result<InodeDesc> {
        let offset_bytes = (inode_idx as usize) * self.inode_size;
        let raw_inode: RawInode = self.inode_table_cache.read_val(offset_bytes)?;
        let extras = self.read_time_extras(offset_bytes)?;
        InodeDesc::try_from_raw_with_extras(&raw_inode, extras)
    }

    /// Writes an inode descriptor to the group's inode table `PageCache`.
    ///
    /// The sub-second timestamp words are written too when the slot
    /// advertises room for them; the rest of the large-inode area
    /// (`i_crtime`, `i_version_hi`, `i_projid`, checksums) is left untouched.
    pub(super) fn write_back_inode_desc(
        &self,
        ino: Ext2Ino,
        raw: &RawInode,
        extras: Option<InodeTimeExtras>,
    ) -> Result<()> {
        let inode_idx = self.inode_idx_in_group(ino);
        let offset_bytes = (inode_idx as usize) * self.inode_size;
        self.inode_table_cache.write_val(offset_bytes, raw)?;
        if let Some(extras) = extras
            && self.time_extras_available(offset_bytes)?
        {
            self.inode_table_cache
                .write_val(offset_bytes + EXTRA_CTIME_OFFSET, &extras.ctime)?;
            self.inode_table_cache
                .write_val(offset_bytes + EXTRA_MTIME_OFFSET, &extras.mtime)?;
            self.inode_table_cache
                .write_val(offset_bytes + EXTRA_ATIME_OFFSET, &extras.atime)?;
            if let Some((secs, extra)) = extras.crtime
                && self.crtime_available(offset_bytes)?
            {
                self.inode_table_cache
                    .write_val(offset_bytes + EXTRA_CRTIME_OFFSET, &secs)?;
                self.inode_table_cache
                    .write_val(offset_bytes + EXTRA_CRTIME_EXTRA_OFFSET, &extra)?;
            }
        }
        if self.metadata_csum {
            self.write_inode_checksum(ino, offset_bytes, raw)?;
        }
        Ok(())
    }

    /// Recomputes `i_checksum_lo`/`i_checksum_hi` for the inode slot.
    ///
    /// The checksum covers the whole inode slot with both checksum fields
    /// zeroed, seeded by the inode number and generation. It is written only
    /// when the slot advertises the large-inode area; an inode that does not
    /// (old 128-byte inodes, or `i_extra_isize == 0`) keeps no checksum, which
    /// is also how Linux treats the latter.
    fn write_inode_checksum(
        &self,
        ino: Ext2Ino,
        offset_bytes: usize,
        raw: &RawInode,
    ) -> Result<()> {
        const CHECKSUM_LO: usize = 0x7C;
        const EXTRA_ISIZE: usize = 0x80;
        const CHECKSUM_HI: usize = 0x82;
        const CHECKSUM_HI_FITS_EXTRA: usize = CHECKSUM_HI + 2 - 128;

        let mut slot = vec![0u8; self.inode_size];
        self.inode_table_cache
            .read_bytes(offset_bytes, &mut slot)?;
        let crc = checksum::inode_seed(self.csum_seed, ino, raw.generation);

        if self.inode_size <= 128 {
            slot[CHECKSUM_LO..CHECKSUM_LO + 2].copy_from_slice(&[0, 0]);
            let crc = checksum::crc32c(crc, &slot[..128]);
            self.inode_table_cache
                .write_val(offset_bytes + CHECKSUM_LO, &(crc as u16))?;
            return Ok(());
        }

        let extra_isize =
            u16::from_le_bytes([slot[EXTRA_ISIZE], slot[EXTRA_ISIZE + 1]]) as usize;
        if extra_isize == 0 {
            return Ok(());
        }

        slot[CHECKSUM_LO..CHECKSUM_LO + 2].copy_from_slice(&[0, 0]);
        let hi_fits = extra_isize >= CHECKSUM_HI_FITS_EXTRA;
        if hi_fits {
            slot[CHECKSUM_HI..CHECKSUM_HI + 2].copy_from_slice(&[0, 0]);
        }
        let crc = checksum::crc32c(crc, &slot[..self.inode_size]);
        self.inode_table_cache
            .write_val(offset_bytes + CHECKSUM_LO, &(crc as u16))?;
        if hi_fits {
            self.inode_table_cache
                .write_val(offset_bytes + CHECKSUM_HI, &((crc >> 16) as u16))?;
        }
        Ok(())
    }

    /// Prepares a freshly allocated inode slot for use.
    ///
    /// The bytes beyond the 128-byte GOOD_OLD inode are zeroed and
    /// `i_extra_isize` is set, as Linux does when it creates an inode.
    /// Without this a reused slot would inherit the previous file's
    /// `i_crtime`, project id and version, and would not advertise the
    /// timestamp extras at all.
    pub(super) fn initialize_inode_slot(&self, ino: Ext2Ino) -> Result<()> {
        if self.inode_size <= EXTRA_ISIZE_OFFSET + size_of::<u16>() {
            return Ok(());
        }
        let inode_idx = self.inode_idx_in_group(ino);
        let offset_bytes = (inode_idx as usize) * self.inode_size;
        self.inode_table_cache.fill_zeros(
            offset_bytes + EXTRA_ISIZE_OFFSET..offset_bytes + self.inode_size,
        )?;
        // Linux's `s_want_extra_isize`: 32 bytes beyond the old inode.
        let extra_isize = (self.inode_size - EXTRA_ISIZE_OFFSET).min(32) as u16;
        self.inode_table_cache
            .write_val(offset_bytes + EXTRA_ISIZE_OFFSET, &extra_isize)?;
        Ok(())
    }

    /// Reads the timestamp extras of the inode slot at `offset_bytes`, if the
    /// slot's `i_extra_isize` covers all three words.
    fn read_time_extras(&self, offset_bytes: usize) -> Result<Option<InodeTimeExtras>> {
        if !self.time_extras_available(offset_bytes)? {
            return Ok(None);
        }
        let crtime = if self.crtime_available(offset_bytes)? {
            Some((
                self.inode_table_cache
                    .read_val(offset_bytes + EXTRA_CRTIME_OFFSET)?,
                self.inode_table_cache
                    .read_val(offset_bytes + EXTRA_CRTIME_EXTRA_OFFSET)?,
            ))
        } else {
            None
        };
        Ok(Some(InodeTimeExtras {
            ctime: self
                .inode_table_cache
                .read_val(offset_bytes + EXTRA_CTIME_OFFSET)?,
            mtime: self
                .inode_table_cache
                .read_val(offset_bytes + EXTRA_MTIME_OFFSET)?,
            atime: self
                .inode_table_cache
                .read_val(offset_bytes + EXTRA_ATIME_OFFSET)?,
            crtime,
        }))
    }

    /// Returns whether the slot is large enough and its `i_extra_isize`
    /// advertises room through `i_atime_extra`.
    fn time_extras_available(&self, offset_bytes: usize) -> Result<bool> {
        let Some(extra_isize) = self.valid_extra_isize(offset_bytes)? else {
            return Ok(false);
        };
        Ok(EXTRA_ISIZE_OFFSET + extra_isize >= EXTRA_TIMES_END)
    }

    /// Returns whether the slot advertises room through `i_crtime_extra`.
    fn crtime_available(&self, offset_bytes: usize) -> Result<bool> {
        let Some(extra_isize) = self.valid_extra_isize(offset_bytes)? else {
            return Ok(false);
        };
        Ok(EXTRA_ISIZE_OFFSET + extra_isize >= EXTRA_CRTIME_END)
    }

    /// Returns the slot's `i_extra_isize` when it is well formed.
    fn valid_extra_isize(&self, offset_bytes: usize) -> Result<Option<usize>> {
        if self.inode_size < EXTRA_TIMES_END {
            return Ok(None);
        }
        let extra_isize: u16 = self
            .inode_table_cache
            .read_val(offset_bytes + EXTRA_ISIZE_OFFSET)?;
        let extra_isize = extra_isize as usize;
        Ok((extra_isize % size_of::<u32>() == 0
            && EXTRA_ISIZE_OFFSET + extra_isize <= self.inode_size)
            .then_some(extra_isize))
    }

    /// Stages one dirty bitmap for the next commit.
    ///
    /// Returns the staged bytes so the caller can clear the dirty flag only
    /// after the transaction is durable.
    fn stage_bitmap(&self, fs: &Ext2, block_bitmap: bool) -> Result<Option<Vec<u8>>> {
        let (bid, payload) = {
            let metadata = self.metadata.read();
            if block_bitmap {
                if !metadata.block_bitmap.is_dirty() {
                    return Ok(None);
                }
                (
                    metadata.desc.block_bitmap_bid,
                    metadata.block_bitmap.as_bytes().to_vec(),
                )
            } else {
                if !metadata.inode_bitmap.is_dirty() {
                    return Ok(None);
                }
                (
                    metadata.desc.inode_bitmap_bid,
                    metadata.inode_bitmap.as_bytes().to_vec(),
                )
            }
        };

        fs.stage_metadata_block(bid, &payload)?;
        debug!(
            "ext2: group {} staged {} bitmap of block {}",
            self.group_idx,
            if block_bitmap { "block" } else { "inode" },
            bid
        );
        Ok(Some(payload))
    }

    /// Returns the 0-based group-local inode index.
    fn inode_idx_in_group(&self, ino: Ext2Ino) -> u16 {
        debug_assert!(ino > 0);
        debug_assert_eq!(
            ((ino - 1) / self.nr_inodes_per_group) as usize,
            self.group_idx
        );
        ((ino - 1) % self.nr_inodes_per_group) as u16
    }

    /// Loads and validates the block bitmap for this group.
    fn load_block_bitmap(
        block_device: &dyn BlockDevice,
        first_block: u32,
        last_block: u32,
        desc: &BlockGroupDesc,
    ) -> Result<IdBitmap> {
        let bitmap_bid = desc.block_bitmap_bid;

        let mut buf = vec![0u8; BLOCK_SIZE];
        if block_device
            .read_bytes(Bid::new(bitmap_bid as u64).to_offset(), &mut buf)
            .is_err()
        {
            return_errno_with_message!(Errno::EIO, "failed to read block bitmap");
        }

        let max_bit = last_block - first_block;
        let capacity = (max_bit + 1) as usize;
        debug_assert!(capacity <= IdBitmap::capacity() as usize);
        Ok(IdBitmap::from_buf(buf.into_boxed_slice(), capacity as u16))
    }

    /// Loads the inode bitmap for this group.
    fn load_inode_bitmap(
        block_device: &dyn BlockDevice,
        nr_inodes_per_group: u32,
        desc: &BlockGroupDesc,
    ) -> Result<IdBitmap> {
        let bitmap_bid = desc.inode_bitmap_bid;

        let mut buf = vec![0u8; BLOCK_SIZE];
        if block_device
            .read_bytes(Bid::new(bitmap_bid as u64).to_offset(), &mut buf)
            .is_err()
        {
            return_errno_with_message!(Errno::EIO, "failed to read inode bitmap");
        }

        let capacity = nr_inodes_per_group as usize;
        debug_assert!(capacity <= IdBitmap::capacity() as usize);

        Ok(IdBitmap::from_buf(buf.into_boxed_slice(), capacity as u16))
    }

    /// Checks whether [start, start+count-1] overlaps any system metadata block
    /// (block bitmap, inode bitmap, inode table) of this group.
    ///
    /// This variant accepts a `BlockGroupDesc` reference directly, allowing callers
    /// that already hold the metadata lock to avoid re-acquiring it.
    fn overlaps_system_zone_with(&self, desc: &BlockGroupDesc, range: Range<u32>) -> bool {
        if range.is_empty() {
            return false;
        }

        let block_bitmap = desc.block_bitmap_bid..(desc.block_bitmap_bid + 1);
        let inode_bitmap = desc.inode_bitmap_bid..(desc.inode_bitmap_bid + 1);
        let inode_table =
            desc.inode_table_bid..(desc.inode_table_bid + self.nr_inode_table_blocks_per_group);

        Self::ranges_overlap(&range, &block_bitmap)
            || Self::ranges_overlap(&range, &inode_bitmap)
            || Self::ranges_overlap(&range, &inode_table)
    }

    fn ranges_overlap(a: &Range<u32>, b: &Range<u32>) -> bool {
        !a.is_empty() && !b.is_empty() && a.start < b.end && b.start < a.end
    }
}

/// Combined metadata for a block group: descriptor and bitmaps.
pub(super) struct BlockGroupMetadata {
    /// Group descriptor with dirty tracking.
    pub desc: Dirty<BlockGroupDesc>,
    /// Block bitmap cached in memory.
    pub block_bitmap: Dirty<IdBitmap>,
    /// Inode bitmap cached in memory.
    pub inode_bitmap: Dirty<IdBitmap>,
}

impl Debug for BlockGroupMetadata {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("BlockGroupMetadata")
            .field("desc", &self.desc)
            .field("block_bitmap_dirty", &self.block_bitmap.is_dirty())
            .field("inode_bitmap_dirty", &self.inode_bitmap.is_dirty())
            .finish()
    }
}

/// Offsets inside a block group descriptor.
const BG_BLOCK_BITMAP_CSUM_LO: usize = 0x18;
const BG_INODE_BITMAP_CSUM_LO: usize = 0x1A;
const BG_CHECKSUM: usize = 0x1E;
const BG_BLOCK_BITMAP_CSUM_HI: usize = 0x38;
const BG_INODE_BITMAP_CSUM_HI: usize = 0x3A;

/// In-memory block group descriptor.
///
/// The raw bytes are kept so fields this implementation does not interpret
/// (`bg_flags`, the exclude bitmap, `bg_itable_unused`, the reserved word)
/// survive a read-modify-write cycle unchanged.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct BlockGroupDesc {
    raw: [u8; 64],
    pub block_bitmap_bid: Ext2Bid,
    pub inode_bitmap_bid: Ext2Bid,
    pub inode_table_bid: Ext2Bid,
    pub free_blocks_count: u16,
    pub free_inodes_count: u16,
    pub used_dirs_count: u16,
    /// Inodes at the end of the inode table that have never been used.
    pub itable_unused: u16,
}

impl BlockGroupDesc {
    /// Decodes a descriptor, rejecting 64-bit block numbers we cannot address.
    fn parse(raw: [u8; 64], desc_size: usize) -> Result<Self> {
        let read_u32 = |offset: usize| {
            u32::from_le_bytes(raw[offset..offset + 4].try_into().unwrap())
        };
        let read_u16 =
            |offset: usize| u16::from_le_bytes([raw[offset], raw[offset + 1]]);

        if desc_size >= 64 {
            for offset in [32usize, 36, 40] {
                if read_u32(offset) != 0 {
                    return_errno_with_message!(
                        Errno::EINVAL,
                        "64-bit group descriptor block numbers are unsupported"
                    );
                }
            }
            for offset in [44usize, 46, 48] {
                if read_u16(offset) != 0 {
                    return_errno_with_message!(
                        Errno::EINVAL,
                        "64-bit group descriptor counters are unsupported"
                    );
                }
            }
        }

        Ok(Self {
            raw,
            block_bitmap_bid: read_u32(0),
            inode_bitmap_bid: read_u32(4),
            inode_table_bid: read_u32(8),
            free_blocks_count: read_u16(12),
            free_inodes_count: read_u16(14),
            used_dirs_count: read_u16(16),
            itable_unused: read_u16(0x1C),
        })
    }

    /// Stores a block bitmap checksum (32 bits split across the lo/hi fields).
    fn set_block_bitmap_csum(&mut self, value: u32, desc_size: usize) {
        self.raw[BG_BLOCK_BITMAP_CSUM_LO..BG_BLOCK_BITMAP_CSUM_LO + 2]
            .copy_from_slice(&(value as u16).to_le_bytes());
        if desc_size >= 64 {
            self.raw[BG_BLOCK_BITMAP_CSUM_HI..BG_BLOCK_BITMAP_CSUM_HI + 2]
                .copy_from_slice(&((value >> 16) as u16).to_le_bytes());
        }
    }

    /// Stores an inode bitmap checksum (32 bits split across the lo/hi fields).
    fn set_inode_bitmap_csum(&mut self, value: u32, desc_size: usize) {
        self.raw[BG_INODE_BITMAP_CSUM_LO..BG_INODE_BITMAP_CSUM_LO + 2]
            .copy_from_slice(&(value as u16).to_le_bytes());
        if desc_size >= 64 {
            self.raw[BG_INODE_BITMAP_CSUM_HI..BG_INODE_BITMAP_CSUM_HI + 2]
                .copy_from_slice(&((value >> 16) as u16).to_le_bytes());
        }
    }

    /// Serializes the descriptor into its first `desc_size` bytes.
    ///
    /// The mutable fields are refreshed and, with `metadata_csum`, the
    /// descriptor checksum is recomputed over the whole descriptor with the
    /// checksum field zeroed.
    fn encode(
        &self,
        group_idx: usize,
        desc_size: usize,
        seed: u32,
        metadata_csum: bool,
    ) -> [u8; 64] {
        let mut out = self.raw;
        out[0..4].copy_from_slice(&self.block_bitmap_bid.to_le_bytes());
        out[4..8].copy_from_slice(&self.inode_bitmap_bid.to_le_bytes());
        out[8..12].copy_from_slice(&self.inode_table_bid.to_le_bytes());
        out[12..14].copy_from_slice(&self.free_blocks_count.to_le_bytes());
        out[14..16].copy_from_slice(&self.free_inodes_count.to_le_bytes());
        out[16..18].copy_from_slice(&self.used_dirs_count.to_le_bytes());
        out[0x1C..0x1E].copy_from_slice(&self.itable_unused.to_le_bytes());
        if desc_size >= 64 {
            out[0x32..0x34].copy_from_slice(&0u16.to_le_bytes());
        }

        if metadata_csum {
            out[BG_CHECKSUM..BG_CHECKSUM + 2].copy_from_slice(&[0, 0]);
            let crc = checksum::crc32c(seed, &(group_idx as u32).to_le_bytes());
            let crc = checksum::crc32c(crc, &out[..desc_size]);
            out[BG_CHECKSUM..BG_CHECKSUM + 2]
                .copy_from_slice(&((crc & 0xffff) as u16).to_le_bytes());
        }
        out
    }
}

impl BlockGroupDesc {
    /// Validates that free counters fit within this group's capacity.
    fn validate_free_counts(&self, nr_blocks_in_group: u32, nr_inodes_in_group: u32) -> Result<()> {
        if u32::from(self.free_blocks_count) > nr_blocks_in_group {
            return_errno_with_message!(
                Errno::EINVAL,
                "group free blocks count exceeds group blocks count"
            );
        }
        if u32::from(self.free_inodes_count) > nr_inodes_in_group {
            return_errno_with_message!(
                Errno::EINVAL,
                "group free inodes count exceeds group inodes count"
            );
        }

        Ok(())
    }

    /// Validates that all metadata block pointers (block bitmap, inode bitmap,
    /// inode table) fall within the group range and are marked as allocated.
    fn validate_metadata_blocks(
        &self,
        block_bitmap: &IdBitmap,
        first_block: u32,
        last_block: u32,
        nr_inode_table_blocks_per_group: u32,
        flex_bg: bool,
    ) -> Result<()> {
        if flex_bg {
            // With `FLEX_BG` the bitmaps and inode table of this group may be
            // stored anywhere inside the same flex group, so this group's
            // block bitmap does not describe them; the bitmap of the group
            // that physically contains them does. Only the presence of the
            // pointers can be checked here, and `e2fsck` verifies the layout.
            if self.block_bitmap_bid == 0
                || self.inode_bitmap_bid == 0
                || self.inode_table_bid == 0
            {
                return_errno_with_message!(Errno::EINVAL, "missing group metadata block");
            }
            return Ok(());
        }

        let max_bit = last_block - first_block;

        let is_valid_fn = |bid: u32| -> bool {
            let Some(offset) = bid.checked_sub(first_block) else {
                return false;
            };
            if offset > max_bit {
                return false;
            }
            block_bitmap.is_allocated(offset as u16)
        };

        if !is_valid_fn(self.block_bitmap_bid) {
            return_errno_with_message!(Errno::EINVAL, "block bitmap block invalid or not marked");
        }
        if !is_valid_fn(self.inode_bitmap_bid) {
            return_errno_with_message!(Errno::EINVAL, "inode bitmap block invalid or not marked");
        }

        if self.inode_table_bid < first_block || self.inode_table_bid - first_block > max_bit {
            return_errno_with_message!(Errno::EINVAL, "inode table start out of group range");
        }
        let table_start_bit = self.inode_table_bid - first_block;
        let table_end_bit = table_start_bit + nr_inode_table_blocks_per_group;
        if table_end_bit - 1 > max_bit {
            return_errno_with_message!(Errno::EINVAL, "inode table extends beyond group");
        }
        for table_bit in table_start_bit..table_end_bit {
            if !block_bitmap.is_allocated(table_bit as u16) {
                return_errno_with_message!(Errno::EINVAL, "inode table block not marked in bitmap");
            }
        }

        Ok(())
    }
}

/// On-disk block group descriptor (the classic 32-byte body).
///
/// Used by the test fixtures and by callers that need the byte layout; the
/// live code reads and writes descriptors through [`BlockGroupDesc`], which
/// preserves the optional 32-byte extension of `64BIT` images.
#[repr(C)]
#[derive(Clone, Copy, Debug, Eq, PartialEq, Pod)]
pub(super) struct RawBlockGroup {
    pub block_bitmap_bid: u32,      // bg_block_bitmap_lo
    pub inode_bitmap_bid: u32,      // bg_inode_bitmap_lo
    pub inode_table_bid: u32,       // bg_inode_table_lo
    pub free_blocks_count: u16,     // bg_free_blocks_count_lo
    pub free_inodes_count: u16,     // bg_free_inodes_count_lo
    pub used_dirs_count: u16,       // bg_used_dirs_count_lo
    pub flags: u16,                 // bg_flags
    pub exclude_bitmap: u32,        // bg_exclude_bitmap_lo
    pub block_bitmap_csum: u16,     // bg_block_bitmap_csum_lo
    pub inode_bitmap_csum: u16,     // bg_inode_bitmap_csum_lo
    pub itable_unused: u16,         // bg_itable_unused_lo
    pub checksum: u16,              // bg_checksum
}

const_assert!(size_of::<RawBlockGroup>() == 32);

/// Reads the descriptor of `group_idx` from the in-memory descriptor table.
///
/// The first 32 bytes have the same layout in both the classic and the
/// `64BIT` descriptor formats. A 64-byte descriptor extends it with high
/// block-number words; Asterinas cannot address those blocks, so a non-zero
/// value means the image exceeds the supported 32-bit block space and is
/// rejected instead of silently pointing at the wrong block.
pub(super) fn read_group_descriptor(
    group_descs: &USegment,
    group_idx: usize,
    desc_size: usize,
) -> Result<BlockGroupDesc> {
    let offset = group_idx * desc_size;
    let mut raw = [0u8; 64];
    group_descs
        .read_bytes(offset, &mut raw[..desc_size])
        .map_err(|_| Error::with_message(Errno::EIO, "failed to read group descriptor"))?;
    BlockGroupDesc::parse(raw, desc_size)
}

/// The group metadata committed by one transaction.
///
/// The bitmap payloads are the exact bytes that were staged; they are kept so
/// the dirty flags can be cleared only if the in-memory state did not change
/// while the transaction was being written.
pub(super) struct StagedGroupMetadata {
    block_bitmap: Option<Vec<u8>>,
    inode_bitmap: Option<Vec<u8>>,
    descriptor: Option<BlockGroupDesc>,
}

/// Backend of the inode table page cache in one block group.
struct InodeTableBackend {
    /// Physical block ID of `bg_inode_table`.
    inode_table_bid: Ext2Bid,
    /// Total inode table size in bytes (`nr_inodes_per_group * inode_size`).
    raw_inodes_size: usize,
    /// Block device handle for I/O.
    block_device: Arc<dyn BlockDevice>,
    /// Filesystem context reserved for journal-aware writeback.
    fs: Weak<Ext2>,
}

impl BlockAsPageCacheBackend for InodeTableBackend {
    fn submit_read_bio(
        &self,
        idx: usize,
        bio_segment: BioSegment,
        complete_fn: BioCompleteFn,
        io_batch: &mut IoBatch,
    ) -> Result<()> {
        if self.raw_inodes_size <= idx * BLOCK_SIZE {
            return_errno_with_message!(Errno::EINVAL, "invalid read size");
        }
        let bid = Bid::new(self.inode_table_bid as u64) + idx as u64;
        self.block_device
            .read_blocks_async(bid, vec![bio_segment], Some(complete_fn), io_batch)?;
        Ok(())
    }

    fn submit_read_page_run(&self, mut pages: PageRun<'_>, io_batch: &mut IoBatch) -> Result<()> {
        if self.raw_inodes_size < (pages.start_idx() + pages.len()) * BLOCK_SIZE {
            return_errno_with_message!(Errno::EINVAL, "invalid read size");
        }

        let start_idx = pages.start_idx();
        let bid = Bid::new(self.inode_table_bid as u64) + start_idx as u64;
        let len = pages.len();

        pages.submit_contiguous_read_pages(self.block_device.as_ref(), bid, len, io_batch)
    }

    fn submit_write_bio(
        &self,
        idx: usize,
        bio_segment: BioSegment,
        complete_fn: BioCompleteFn,
        io_batch: &mut IoBatch,
    ) -> Result<()> {
        if self.raw_inodes_size <= idx * BLOCK_SIZE {
            return_errno_with_message!(Errno::EINVAL, "invalid write size");
        }
        let bid = Bid::new(self.inode_table_bid as u64) + idx as u64;

        // Inode descriptors are allocation metadata: an entry in the inode
        // table is only meaningful together with the inode bitmap bit and the
        // group counter that authorise it. Staging the block here (instead of
        // writing it to its home location) lets the next commit put both in one
        // journal transaction. The page cache is told the write completed; the
        // staged copy owns the bytes from this point on.
        let fs = self
            .fs
            .upgrade()
            .ok_or_else(|| Error::with_message(Errno::EIO, "filesystem already dropped"))?;
        let mut bytes = vec![0u8; bio_segment.nbytes()];
        bio_segment.copy_out(&mut bytes)?;
        fs.stage_metadata_block(bid.to_raw() as Ext2Bid, &bytes)?;
        complete_fn(BioStatus::Complete);

        let _ = io_batch;
        Ok(())
    }

    // `submit_write_page_run` is deliberately not overridden: the default
    // implementation submits each page through `submit_write_bio`, which is
    // where journal staging lives. A batched staging entry point can replace it
    // once the transaction layer can accept page runs directly.
}

impl InodeTableBackend {
    #[expect(dead_code)]
    fn ensure_filesystem_alive(&self) -> Result<()> {
        self.fs
            .upgrade()
            .map(|_| ())
            .ok_or_else(|| Error::with_message(Errno::EIO, "filesystem already dropped"))
    }
}
