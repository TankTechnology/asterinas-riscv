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
//!   are validated at mount time. Without `FLEX_BG` they must lie within
//!   the group's block range and be marked as allocated; with `FLEX_BG`
//!   they may live in another group's range, so only the filesystem-wide
//!   bounds are checked.
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
    fs::Ext2,
    inode::{Inode, InodeDesc, RawInode},
    prelude::*,
    super_block::{FeatureInCompatSet, SuperBlock},
};
use crate::fs::utils::IdBitmap;

/// Represents one block group in an ext2 filesystem.
///
/// A block group is a filesystem-local allocation domain with its own block
/// bitmap, inode bitmap, and inode table. Keeping related blocks and inodes in
/// the same group improves locality on disk.
pub(super) struct BlockGroup {
    /// Block group index (0-based).
    group_idx: usize,
    /// Group descriptor and bitmaps, protected by a single lock.
    metadata: RwMutex<BlockGroupMetadata>,
    /// Backing block device (shared with `Ext2` and other groups).
    block_device: Arc<dyn BlockDevice>,
    /// Cached geometry: first filesystem-wide block number of this group.
    first_block: u32,
    /// Cached geometry: last filesystem-wide block number of this group.
    last_block: u32,
    /// Cached geometry: inode table blocks per group.
    nr_inode_table_blocks_per_group: u32,
    /// On-disk group descriptor size.
    group_desc_size: usize,
    /// Whether metadata structures carry CRC32C checksums.
    metadata_csum: bool,
    /// Seed every metadata checksum starts from.
    checksum_seed: u32,
    /// Blocks in this group (the last group can be short).
    nr_blocks_in_group: u32,
    /// Cached geometry: inodes per group.
    nr_inodes_per_group: u32,
    /// Whether the volume uses `FLEX_BG`, cached for the deferred metadata
    /// validation (see `ensure_bitmaps_loaded`).
    flex_bg: bool,
    /// Filesystem-wide block count, cached for the same reason.
    total_blocks: u32,
    /// Number of valid inode IDs in this group (the last group can be short).
    nr_inodes_in_group: u32,
    /// Cached geometry: inode size in bytes.
    inode_size: usize,
    /// Inode table page cache backend.
    _inode_table_backend: Arc<InodeTableBackend>,
    /// Inode table page cache.
    inode_table_cache: PageCache,
    /// Byte range of the inode table that has been written since the last
    /// flush. `fsync` used to flush the whole table of every group, which on a
    /// 4 GiB image scans ~256 MiB of page cache per call; track the dirty range
    /// so a one-inode update costs one inode-sized flush.
    inode_table_dirty: RwMutex<Option<Range<usize>>>,
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
    ) -> Result<Self> {
        let offset = group_idx * sb.group_desc_size();
        let raw_group = group_descs
            .read_val::<RawBlockGroup>(offset)
            .map_err(|_| Error::with_message(Errno::EIO, "failed to read group descriptor"))?;
        validate_64bit_descriptor(group_descs, offset, sb)?;
        let group_desc = BlockGroupDesc::from(raw_group);

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

        // The bitmaps are *not* read here. A 115 GiB ext2 volume has 917 block
        // groups, and reading every block and inode bitmap took 2.1 s on the
        // Megrez board (1834 separate 4 KiB reads at ~1.1 ms each) -- paid on
        // every mount, for groups the caller may never touch. Linux loads them
        // per group on first use; so does `ensure_bitmaps_loaded`. The
        // descriptor checks above still run at mount, and the bitmap-backed
        // validation moves to the first load of that group.
        let block_bitmap = Self::empty_block_bitmap(first_block_no, last_block_no);
        let inode_bitmap = Self::empty_inode_bitmap(nr_inodes_in_group);

        // Create `PageCache` for inode table backed by `InodeTableBackend`.
        let raw_inodes_size = (nr_inodes_per_group as usize) * inode_size;
        let backend = Arc::new(InodeTableBackend {
            inode_table_bid: group_desc.inode_table_bid,
            raw_inodes_size,
            block_device: block_device.clone(),
        });
        let inode_table_cache =
            PageCache::new_with_backend(raw_inodes_size, Arc::downgrade(&backend) as _)?;

        Ok(Self {
            group_idx,
            metadata: RwMutex::new(BlockGroupMetadata {
                desc: Dirty::new(group_desc),
                block_bitmap: Dirty::new(block_bitmap),
                inode_bitmap: Dirty::new(inode_bitmap),
                bitmaps_loaded: false,
                pending_frees: Vec::new(),
                pending_frees_count: 0,
                pending_inode_frees: Vec::new(),
                pending_inode_frees_count: 0,
            }),
            block_device,
            first_block: first_block_no,
            last_block: last_block_no,
            nr_inode_table_blocks_per_group,
            group_desc_size: sb.group_desc_size(),
            metadata_csum: sb.metadata_csum(),
            checksum_seed: sb.checksum_seed(),
            nr_blocks_in_group: last_block_no - first_block_no + 1,
            nr_inodes_per_group,
            nr_inodes_in_group,
            flex_bg: sb.feature_incompat().contains(FeatureInCompatSet::FLEX_BG),
            total_blocks: sb.total_blocks(),
            inode_size,
            _inode_table_backend: backend,
            inode_table_cache,
            inode_table_dirty: RwMutex::new(None),
            inode_cache: RwMutex::new(BTreeMap::new()),
        })
    }

    /// Returns the block group index.
    pub(super) fn group_idx(&self) -> usize {
        self.group_idx
    }

    /// Reloads metadata caches after an on-disk journal replay.
    pub(super) fn refresh_after_recovery(&self, sb: &SuperBlock) -> Result<()> {
        let descriptor_offset = Bid::new(sb.group_descriptors_bid(0) as u64).to_offset()
            + self.group_idx * sb.group_desc_size();
        let raw_group = self
            .block_device
            .read_val::<RawBlockGroup>(descriptor_offset)?;
        validate_64bit_descriptor_from_device(&*self.block_device, descriptor_offset, sb)?;
        let group_desc = BlockGroupDesc::from(raw_group);
        let block_bitmap = Self::load_block_bitmap(
            self.block_device.as_ref(),
            self.first_block,
            self.last_block,
            &group_desc,
        )?;
        let nr_inodes_in_group = self.nr_inodes_in_group;
        let inode_bitmap =
            Self::load_inode_bitmap(self.block_device.as_ref(), nr_inodes_in_group, &group_desc)?;
        group_desc
            .validate_free_counts(self.last_block - self.first_block + 1, nr_inodes_in_group)?;
        group_desc.validate_metadata_blocks(
            &block_bitmap,
            self.first_block,
            self.last_block,
            sb.nr_inode_table_blocks_per_group(),
            sb.feature_incompat().contains(FeatureInCompatSet::FLEX_BG),
            sb.total_blocks(),
        )?;

        {
            let mut metadata = self.metadata.write();
            metadata.desc = Dirty::new(group_desc);
            metadata.block_bitmap = Dirty::new(block_bitmap);
            metadata.inode_bitmap = Dirty::new(inode_bitmap);
            metadata.bitmaps_loaded = true;
            // The bitmaps were just reloaded from disk, so nothing is held
            // back any more.
            metadata.pending_frees.clear();
            metadata.pending_frees_count = 0;
            metadata.pending_inode_frees.clear();
            metadata.pending_inode_frees_count = 0;
        }
        self.inode_table_cache
            .invalidate_range(0..self.nr_inodes_per_group as usize * self.inode_size)?;
        self.inode_cache.write().clear();
        Ok(())
    }

    /// Returns whether an inode is marked allocated in this group.
    pub(super) fn is_inode_allocated(&self, ino: Ext2Ino) -> Result<bool> {
        self.ensure_bitmaps_loaded()?;
        let inode_idx = self.inode_idx_in_group(ino);
        Ok(self.metadata.read().inode_bitmap.is_allocated(inode_idx))
    }

    /// Looks up an allocated inode by inode number.
    pub(super) fn lookup_inode(&self, ino: Ext2Ino, fs: Weak<Ext2>) -> Result<Arc<Inode>> {
        let inode_idx = self.inode_idx_in_group(ino);

        // Fast path: cache hit under read lock.
        if let Some(inode) = self.inode_cache.read().get(&inode_idx) {
            return Ok(inode.clone());
        }

        // Slow path: check allocation bitmap, then load from disk.
        if !self.is_inode_allocated(ino)? {
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

    pub(super) fn sync_inodes_without_table(&self) -> Result<()> {
        // Inodes whose metadata is clean contribute no descriptor and no
        // allocation to the pending transaction, so writing their data back
        // here is not needed for `data=ordered`; durability of their data is
        // their own `fsync`'s job. Skipping them keeps a repeated fsync of one
        // file O(1) instead of O(cached inodes).
        let inodes: Vec<Arc<Inode>> = self.inode_cache.read().values().cloned().collect();
        for inode in inodes {
            if inode.has_pending_metadata() {
                inode.sync_all()?;
            }
        }
        Ok(())
    }

    /// Syncs the dirty part of the inode table back to disk.
    pub(super) fn sync_inode_table(&self) -> Result<()> {
        // Take the range under the lock. A writer that dirties the table after
        // this point records a fresh range for the next sync, so no update is
        // lost and no caller has to walk the whole table.
        let Some(range) = self.inode_table_dirty.write().take() else {
            return Ok(());
        };
        if let Err(error) = self.inode_table_cache.flush_range(range.clone()) {
            // Put the range back so a later sync retries it, merged with any
            // range a concurrent writer recorded in the meantime.
            let mut dirty = self.inode_table_dirty.write();
            let merged = match dirty.take() {
                Some(pending) => pending.start.min(range.start)..pending.end.max(range.end),
                None => range,
            };
            *dirty = Some(merged);
            return Err(error);
        }
        Ok(())
    }

    /// Records that `[offset, offset + len)` of the inode table has changed.
    fn mark_inode_table_dirty(&self, offset: usize, len: usize) {
        let mut dirty = self.inode_table_dirty.write();
        let new = offset..offset + len;
        let merged = match dirty.take() {
            Some(range) => range.start.min(new.start)..range.end.max(new.end),
            None => new,
        };
        *dirty = Some(merged);
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
        self.ensure_bitmaps_loaded()?;
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
                // The descriptor claims free blocks the bitmap does not have.
                // The bitmap is what allocation actually consults, so it wins:
                // correct the counter (as e2fsck would) and report this group
                // as full so the caller tries the next one instead of failing
                // the whole allocation.
                let live_free = metadata.block_bitmap.count_free();
                warn!(
                    "ext2: group {} free count {} disagrees with bitmap ({} free); correcting",
                    self.group_idx, metadata.desc.free_blocks_count, live_free
                );
                metadata.desc.free_blocks_count = live_free;
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
    /// All bits and the resulting counter are checked before any bit changes.
    pub(super) fn free_blocks(&self, bit_range: Range<u32>) -> Result<u32> {
        self.ensure_bitmaps_loaded()?;
        let mut metadata = self.metadata.write();
        let new_free_count = self.checked_free_block_count(&metadata, &bit_range)?;
        for block_bit in bit_range.start as u16..bit_range.end as u16 {
            metadata.block_bitmap.free(block_bit);
        }

        // Persistent in-memory bitmap cache; writeback is deferred to sync_metadata.
        metadata.desc.free_blocks_count = new_free_count;
        Self::record_pending_frees(&mut metadata, bit_range.start as u16, bit_range.end as u16);
        Ok(bit_range.len() as u32)
    }

    /// Remembers that `[start, end)` was freed but is not yet durable.
    fn record_pending_frees(metadata: &mut BlockGroupMetadata, start: u16, end: u16) {
        if start >= end {
            return;
        }
        metadata.pending_frees_count = metadata.pending_frees_count.saturating_add(end - start);
        // Frees usually walk a file's mapping in order, so extending the last
        // range keeps the list proportional to the number of runs, not blocks.
        if let Some(last) = metadata.pending_frees.last_mut() {
            if last.end == start {
                last.end = end;
                return;
            }
            if last.start == end {
                last.start = start;
                return;
            }
        }
        metadata.pending_frees.push(start..end);
    }

    /// Publishes the block bitmap with everything freed since the last
    /// writeback still marked in use, and records the matching descriptor.
    ///
    /// A volume without a journal writes the inode table and the bitmaps
    /// straight to the device, with no ordering guaranteed between the two.
    /// Both directions can be interrupted badly: an inode that has just been
    /// given a block may become durable before the bit that reserves that
    /// block, and a freed bit may become durable before the inode that still
    /// points at it stops doing so. The caller therefore runs this before
    /// writing the inode table and again after it, so that the window between
    /// the two never advertises a block as free while some inode may still
    /// reference it. A crash inside the window leaves blocks leaked, which
    /// `e2fsck` reclaims; it cannot leave a dangling reference.
    ///
    /// Returns whether the group descriptor needs to be written as well.
    pub(super) fn write_pre_inode_bitmap(&self, group_descs: &USegment) -> Result<bool> {
        self.ensure_bitmaps_loaded()?;
        let metadata = self.metadata.read();
        // Every allocation has to reach the device before the inode that uses
        // it does, whether or not anything was freed this time, so a dirty
        // bitmap always gets this early write.
        let block_dirty = metadata.block_bitmap.is_dirty();
        let inode_dirty = metadata.inode_bitmap.is_dirty();
        if !block_dirty && !inode_dirty {
            return Ok(false);
        }

        let rehold = |bitmap: &[u8], ranges: &[Range<u16>]| {
            let mut held = bitmap.to_vec();
            for range in ranges {
                for bit in range.clone() {
                    held[bit as usize / 8] |= 1 << (bit % 8);
                }
            }
            held
        };
        let block_bitmap = rehold(metadata.block_bitmap.as_bytes(), &metadata.pending_frees);
        let inode_bitmap = rehold(
            metadata.inode_bitmap.as_bytes(),
            &metadata.pending_inode_frees,
        );

        if block_dirty {
            self.block_device
                .write_bytes(
                    Bid::new(metadata.desc.block_bitmap_bid as u64).to_offset(),
                    &block_bitmap,
                )
                .map_err(|_| Error::with_message(Errno::EIO, "failed to write block bitmap"))?;
        }
        if inode_dirty {
            self.block_device
                .write_bytes(
                    Bid::new(metadata.desc.inode_bitmap_bid as u64).to_offset(),
                    &inode_bitmap,
                )
                .map_err(|_| Error::with_message(Errno::EIO, "failed to write inode bitmap"))?;
        }

        // The descriptor has to describe the bitmaps that are on their way to
        // the device, or a checksummed volume would reject the pair.
        let mut desc = *metadata.desc;
        desc.free_blocks_count = desc
            .free_blocks_count
            .saturating_sub(metadata.pending_frees_count);
        desc.free_inodes_count = desc
            .free_inodes_count
            .saturating_sub(metadata.pending_inode_frees_count);
        self.write_group_descriptor(group_descs, &desc, &block_bitmap, &inode_bitmap)?;
        Ok(true)
    }

    /// Checks a range before a filesystem-wide free that spans groups.
    pub(super) fn validate_free_blocks(&self, bit_range: Range<u32>) -> Result<()> {
        self.ensure_bitmaps_loaded()?;
        self.checked_free_block_count(&self.metadata.read(), &bit_range)?;
        Ok(())
    }

    fn checked_free_block_count(
        &self,
        metadata: &BlockGroupMetadata,
        bit_range: &Range<u32>,
    ) -> Result<u16> {
        let group_size = self.last_block - self.first_block + 1;
        if bit_range.start >= bit_range.end || bit_range.end > group_size {
            return_errno_with_message!(Errno::EIO, "freeing blocks outside block group");
        }
        let abs_range = (self.first_block + bit_range.start)..(self.first_block + bit_range.end);
        if self.overlaps_system_zone_with(&metadata.desc, abs_range) {
            return_errno_with_message!(Errno::EIO, "freeing blocks in system zone");
        }
        let count = bit_range.len() as u16;
        let new_free_count = metadata
            .desc
            .free_blocks_count
            .checked_add(count)
            .filter(|&count| count as u32 <= group_size)
            .ok_or_else(|| Error::with_message(Errno::EIO, "free block count overflow in group"))?;
        for bit in bit_range.start as u16..bit_range.end as u16 {
            if !metadata.block_bitmap.is_allocated(bit) {
                error!(
                    "free_blocks: refusing to free already-free block {}",
                    self.first_block + bit as u32
                );
                return_errno_with_message!(Errno::EIO, "freeing an already-free block");
            }
        }
        Ok(new_free_count)
    }

    /// Attempts to allocate one inode within this group.
    ///
    /// Returns `Some(inode_idx)` with the 0-based group-relative inode index,
    /// or `None` if no free inode is available in this group.
    pub(super) fn alloc_ino(&self, inode_type: InodeType) -> Result<Option<Ext2Ino>> {
        self.ensure_bitmaps_loaded()?;
        let mut metadata = self.metadata.write();

        if metadata.desc.free_inodes_count == 0 {
            if !metadata.inode_bitmap.is_full() {
                return_errno_with_message!(
                    Errno::EIO,
                    "inode bitmap disagrees with free inode count"
                );
            }
            return Ok(None);
        }
        if inode_type.is_directory() && metadata.desc.used_dirs_count == u16::MAX {
            return_errno_with_message!(Errno::EIO, "group used directory counter overflow");
        }

        if metadata.inode_bitmap.is_full() {
            return_errno_with_message!(Errno::EIO, "inode bitmap disagrees with free inode count");
        }
        let inode_idx = metadata
            .inode_bitmap
            .alloc()
            .ok_or_else(|| Error::with_message(Errno::EIO, "inode bitmap allocation failed"))?;

        metadata.desc.free_inodes_count = metadata
            .desc
            .free_inodes_count
            .checked_sub(1)
            .ok_or_else(|| Error::with_message(Errno::EIO, "group free inode counter underflow"))?;
        if inode_type.is_directory() {
            metadata.desc.used_dirs_count += 1;
        }

        Ok(Some(inode_idx as Ext2Ino))
    }

    /// Frees one inode within this group.
    ///
    /// Returns an error if the inode was already free.
    pub(super) fn free_inode(&self, ino: Ext2Ino, inode_type: InodeType) -> Result<()> {
        self.ensure_bitmaps_loaded()?;
        let inode_idx = self.inode_idx_in_group(ino);
        let mut metadata = self.metadata.write();

        if !metadata.inode_bitmap.is_allocated(inode_idx) {
            error!("free_inode: refusing to free already-free inode {}", ino);
            return_errno_with_message!(Errno::EIO, "freeing an already-free inode");
        }

        let free_inodes_count = metadata
            .desc
            .free_inodes_count
            .checked_add(1)
            .filter(|&count| count as u32 <= self.nr_inodes_in_group)
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
        metadata.pending_inode_frees_count = metadata.pending_inode_frees_count.saturating_add(1);
        if let Some(last) = metadata.pending_inode_frees.last_mut() {
            if last.end == inode_idx {
                last.end = inode_idx + 1;
            } else {
                metadata.pending_inode_frees.push(inode_idx..inode_idx + 1);
            }
        } else {
            metadata.pending_inode_frees.push(inode_idx..inode_idx + 1);
        }
        metadata.desc.free_inodes_count = free_inodes_count;
        if let Some(used_dirs_count) = used_dirs_count {
            metadata.desc.used_dirs_count = used_dirs_count;
        }

        Ok(())
    }

    /// Reads an inode descriptor from the group's inode table `PageCache`.
    ///
    /// `inode_idx` is the 0-based inode index within this group.
    pub(super) fn read_inode_desc(&self, inode_idx: u16) -> Result<InodeDesc> {
        let offset_bytes = (inode_idx as usize) * self.inode_size;
        let raw_inode: RawInode = self.inode_table_cache.read_val(offset_bytes)?;
        InodeDesc::try_from(&raw_inode)
    }

    /// Reads an inode-table block from the page cache, including dirty
    /// descriptors that have not reached the block device yet.
    pub(super) fn read_inode_table_block(
        &self,
        block_offset: usize,
        payload: &mut [u8],
    ) -> Result<()> {
        if payload.len() != BLOCK_SIZE {
            return_errno_with_message!(Errno::EINVAL, "invalid inode-table block size");
        }
        let offset = block_offset
            .checked_mul(BLOCK_SIZE)
            .ok_or_else(|| Error::with_message(Errno::EOVERFLOW, "inode-table offset overflow"))?;
        self.inode_table_cache
            .read_bytes(offset, payload)
            .map_err(|_| Error::with_message(Errno::EIO, "failed to read inode-table cache"))
    }

    /// Writes an inode descriptor to the group's inode table `PageCache`.
    pub(super) fn write_back_inode_desc(&self, ino: Ext2Ino, raw: &RawInode) -> Result<()> {
        let inode_idx = self.inode_idx_in_group(ino);
        let offset_bytes = (inode_idx as usize) * self.inode_size;
        let image = self.inode_slot_image(ino, raw)?;
        self.inode_table_cache.write_bytes(offset_bytes, &image)?;
        self.mark_inode_table_dirty(offset_bytes, self.inode_size);
        Ok(())
    }

    /// Builds the complete on-disk inode slot for `raw`, checksum included.
    ///
    /// The whole slot is returned because every writer that publishes this
    /// inode -- the page cache and the journal staging path alike -- must use
    /// these exact bytes; publishing `RawInode` alone would drop the ext4 tail
    /// and the checksum stored in it.
    pub(super) fn inode_slot_image(&self, ino: Ext2Ino, raw: &RawInode) -> Result<Vec<u8>> {
        let inode_idx = self.inode_idx_in_group(ino);
        let offset_bytes = (inode_idx as usize) * self.inode_size;
        // Start from the slot as it exists on disk: the ext4 tail (i_extra_isize,
        // extra timestamps, checksums) is not part of `RawInode`, and zeroing
        // it would silently drop those fields on every write.
        let mut image = vec![0u8; self.inode_size];
        self.inode_table_cache
            .read_bytes(offset_bytes, &mut image)
            .map_err(|_| Error::with_message(Errno::EIO, "failed to read inode slot"))?;
        image[..size_of::<RawInode>()].copy_from_slice(raw.as_bytes());
        if !self.metadata_csum {
            return Ok(image);
        }

        /// `i_checksum_lo` inside the 128-byte inode.
        const CHECKSUM_LO: usize = 0x7C;
        /// `i_checksum_hi` inside the ext4 inode tail.
        const CHECKSUM_HI: usize = 0x82;
        image[CHECKSUM_LO..CHECKSUM_LO + 2].fill(0);
        let want_hi = self.inode_size > CHECKSUM_HI + 1
            && u16::from_le_bytes([image[0x80], image[0x81]]) as usize >= CHECKSUM_HI + 2 - 0x80;
        if want_hi {
            image[CHECKSUM_HI..CHECKSUM_HI + 2].fill(0);
        }
        let csum = super::checksum::inode(self.checksum_seed, ino, raw.generation, &image);
        image[CHECKSUM_LO..CHECKSUM_LO + 2]
            .copy_from_slice(&((csum & 0xffff) as u16).to_le_bytes());
        if want_hi {
            image[CHECKSUM_HI..CHECKSUM_HI + 2]
                .copy_from_slice(&((csum >> 16) as u16).to_le_bytes());
        }
        Ok(image)
    }

    pub(super) fn inode_table_bid(&self) -> Ext2Bid {
        self.metadata.read().desc.inode_table_bid
    }

    /// Initializes the ext4 extension of a newly allocated inode slot.
    ///
    /// ext4 inode slots are larger than the 128-byte ext2 inode. The tail
    /// carries `i_extra_isize`, the extra timestamps and (with metadata
    /// checksums) `i_checksum_hi`. A reused slot may still hold the previous
    /// tenant's values, so zero the tail and declare the standard 32-byte
    /// extension.
    pub(super) fn init_inode_extra(&self, ino: Ext2Ino) -> Result<()> {
        if self.inode_size <= 128 {
            return Ok(());
        }

        let inode_idx = self.inode_idx_in_group(ino);
        let offset_bytes = (inode_idx as usize) * self.inode_size;
        let extra_len = self.inode_size - 128;
        let extra_isize = u16::try_from(extra_len.min(32)).unwrap_or(0);
        let mut extra = vec![0u8; extra_len];
        extra[..2].copy_from_slice(&extra_isize.to_le_bytes());
        self.inode_table_cache
            .write_bytes(offset_bytes + 128, &extra)
            .map_err(|_| {
                Error::with_message(Errno::EIO, "failed to initialize the inode extension")
            })?;
        self.mark_inode_table_dirty(offset_bytes, self.inode_size);
        Ok(())
    }

    pub(super) fn inode_size(&self) -> usize {
        self.inode_size
    }

    /// Writes dirty bitmaps and stages the group descriptor under a single lock.
    ///
    /// Dirty bitmaps are written to disk here. If the group descriptor is dirty,
    /// this method updates the caller-provided descriptor table segment; the
    /// caller is responsible for writing that segment to disk. The return value
    /// reports whether that descriptor needs writing.
    pub(super) fn sync_metadata(&self, group_descs: &USegment) -> Result<bool> {
        let mut metadata = self.metadata.write();

        // Sync block bitmap.
        if metadata.block_bitmap.is_dirty() {
            let block_bitmap_bid = metadata.desc.block_bitmap_bid;
            if self
                .block_device
                .write_bytes(
                    Bid::new(block_bitmap_bid as u64).to_offset(),
                    metadata.block_bitmap.as_bytes(),
                )
                .is_err()
            {
                // Keep dirty bit set on writeback failure for retry.
                return_errno_with_message!(Errno::EIO, "failed to write block bitmap");
            }
            metadata.block_bitmap.clear_dirty();
            // The frees are on the device now, and the inode table was
            // written before this call, so nothing has to be held back.
            metadata.pending_frees.clear();
            metadata.pending_frees_count = 0;
        }

        // Sync inode bitmap.
        if metadata.inode_bitmap.is_dirty() {
            let inode_bitmap_bid = metadata.desc.inode_bitmap_bid;
            if self
                .block_device
                .write_bytes(
                    Bid::new(inode_bitmap_bid as u64).to_offset(),
                    metadata.inode_bitmap.as_bytes(),
                )
                .is_err()
            {
                // Keep dirty bit set on writeback failure for retry.
                return_errno_with_message!(Errno::EIO, "failed to write inode bitmap");
            }
            metadata.inode_bitmap.clear_dirty();
        }
        // Both bitmaps are on the device at this point, and the inode table was
        // written before this call, so nothing has to be held back.
        metadata.pending_inode_frees.clear();
        metadata.pending_inode_frees_count = 0;

        // Sync group descriptor.
        let desc_dirty = metadata.desc.is_dirty();
        if desc_dirty {
            self.write_group_descriptor(
                group_descs,
                &metadata.desc,
                metadata.block_bitmap.as_bytes(),
                metadata.inode_bitmap.as_bytes(),
            )?;
            metadata.desc.clear_dirty();
        }

        Ok(desc_dirty)
    }

    /// Writes this group's descriptor, recording metadata checksums when the
    /// filesystem uses them.
    ///
    /// The bitmap checksums cover exactly the bytes ext4 checksums for the
    /// group (`blocks_per_group / 8` and `inodes_per_group / 8`), and
    /// `bg_checksum` covers the whole descriptor with its own field zeroed.
    fn write_group_descriptor(
        &self,
        group_descs: &USegment,
        desc: &BlockGroupDesc,
        block_bitmap: &[u8],
        inode_bitmap: &[u8],
    ) -> Result<()> {
        let offset = self.group_idx * self.group_desc_size;
        let mut raw = RawBlockGroup::from(*desc);
        if !self.metadata_csum {
            group_descs.write_val(offset, &raw)?;
            return Ok(());
        }

        let block_len = (self.nr_blocks_in_group as usize / 8).min(block_bitmap.len());
        let inode_len = (self.nr_inodes_per_group as usize / 8).min(inode_bitmap.len());
        let block_csum = super::checksum::bitmap(self.checksum_seed, &block_bitmap[..block_len]);
        let inode_csum = super::checksum::bitmap(self.checksum_seed, &inode_bitmap[..inode_len]);
        raw.reserved[1] = (block_csum & 0xffff) | ((inode_csum & 0xffff) << 16);

        let size = self.group_desc_size;
        let mut bytes = [0u8; 64];
        group_descs.read_bytes(offset, &mut bytes[..size])?;
        bytes[..size_of::<RawBlockGroup>()].copy_from_slice(raw.as_bytes());
        if size >= 64 {
            bytes[0x38..0x3a].copy_from_slice(&((block_csum >> 16) as u16).to_le_bytes());
            bytes[0x3a..0x3c].copy_from_slice(&((inode_csum >> 16) as u16).to_le_bytes());
        }
        bytes[0x1e..0x20].fill(0);
        let mut desc_bytes = [0u8; 64];
        desc_bytes[..size].copy_from_slice(&bytes[..size]);
        let group_csum =
            super::checksum::group_desc(self.checksum_seed, self.group_idx as u32, &desc_bytes);
        bytes[0x1e..0x20].copy_from_slice(&((group_csum & 0xffff) as u16).to_le_bytes());
        group_descs.write_bytes(offset, &bytes[..size])?;
        Ok(())
    }

    pub(super) fn stage_descriptor(&self, group_descs: &USegment) -> Result<bool> {
        let mut metadata = self.metadata.write();
        let desc_dirty = metadata.desc.is_dirty();
        if desc_dirty {
            self.write_group_descriptor(
                group_descs,
                &metadata.desc,
                metadata.block_bitmap.as_bytes(),
                metadata.inode_bitmap.as_bytes(),
            )?;
            metadata.desc.clear_dirty();
        }
        Ok(desc_dirty)
    }

    /// Snapshots dirty allocation bitmaps without clearing their dirty state.
    /// The filesystem layer can submit all group bitmaps in one journal
    /// transaction and clear them only after the transaction is checkpointed.
    pub(super) fn allocation_bitmap_snapshots(&self) -> Vec<(Ext2Bid, Vec<u8>)> {
        let metadata = self.metadata.read();
        let mut blocks = Vec::with_capacity(2);
        if metadata.block_bitmap.is_dirty() {
            blocks.push((
                metadata.desc.block_bitmap_bid,
                metadata.block_bitmap.as_bytes().to_vec(),
            ));
        }
        if metadata.inode_bitmap.is_dirty() {
            blocks.push((
                metadata.desc.inode_bitmap_bid,
                metadata.inode_bitmap.as_bytes().to_vec(),
            ));
        }
        blocks
    }

    pub(super) fn clear_allocation_bitmap_dirty(&self) {
        let mut metadata = self.metadata.write();
        metadata.block_bitmap.clear_dirty();
        metadata.inode_bitmap.clear_dirty();
        // The bitmaps reached the device, so the freed blocks no longer have
        // to be held back.
        metadata.pending_frees.clear();
        metadata.pending_frees_count = 0;
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

    /// A zeroed block bitmap of the right shape, used until the group is
    /// first touched.
    fn empty_block_bitmap(first_block: u32, last_block: u32) -> IdBitmap {
        let capacity = (last_block - first_block + 1) as u16;
        debug_assert!((capacity as usize) <= IdBitmap::capacity() as usize);
        IdBitmap::from_buf(vec![0u8; BLOCK_SIZE].into_boxed_slice(), capacity)
    }

    /// A zeroed inode bitmap of the right shape.
    fn empty_inode_bitmap(nr_inodes_in_group: u32) -> IdBitmap {
        let capacity = nr_inodes_in_group as u16;
        debug_assert!((capacity as usize) <= IdBitmap::capacity() as usize);
        IdBitmap::from_buf(vec![0u8; BLOCK_SIZE].into_boxed_slice(), capacity)
    }

    /// Reads this group's block and inode bitmaps, once.
    ///
    /// Everything that consults or mutates allocation state calls this first.
    /// The metadata-block validation that used to run at mount runs here
    /// instead, because it needs the block bitmap: a group that is never used
    /// never pays for it, and a group that is used is validated before the
    /// first block of it can be handed out.
    pub(super) fn ensure_bitmaps_loaded(&self) -> Result<()> {
        if self.metadata.read().bitmaps_loaded {
            return Ok(());
        }
        let mut metadata = self.metadata.write();
        if metadata.bitmaps_loaded {
            return Ok(());
        }
        let block_bitmap = Self::load_block_bitmap(
            self.block_device.as_ref(),
            self.first_block,
            self.last_block,
            &metadata.desc,
        )?;
        let inode_bitmap = Self::load_inode_bitmap(
            self.block_device.as_ref(),
            self.nr_inodes_in_group,
            &metadata.desc,
        )?;
        metadata.desc.validate_metadata_blocks(
            &block_bitmap,
            self.first_block,
            self.last_block,
            self.nr_inode_table_blocks_per_group,
            self.flex_bg,
            self.total_blocks,
        )?;
        metadata.block_bitmap = Dirty::new(block_bitmap);
        metadata.inode_bitmap = Dirty::new(inode_bitmap);
        metadata.bitmaps_loaded = true;
        Ok(())
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
        nr_inodes_in_group: u32,
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

        let capacity = nr_inodes_in_group as usize;
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

#[cfg(ktest)]
mod test {
    use ostd::prelude::*;

    use super::*;
    use crate::{
        fs::fs_impls::ext2::test_utils::{Ext2FixtureBuilder, assert_errno},
        time::clocks,
    };

    #[ktest]
    fn partial_last_group_rejects_out_of_range_inode_mutations() {
        clocks::init_for_ktest();
        let f = Ext2FixtureBuilder::new(2, 256)
            .with_total_inodes(1025)
            .with_free_inodes(1, 0)
            .build()
            .unwrap();
        let group = f.ext2.block_group(1);
        {
            let mut metadata = group.metadata.write();
            assert_eq!(metadata.inode_bitmap.alloc(), Some(0));
            // The group's only valid inode is now allocated.
            metadata.desc.free_inodes_count = 0;
        }

        assert!(group.alloc_ino(InodeType::File).unwrap().is_none());
        // Deliberately make the counter inconsistent: freeing the inode would
        // overflow the partial group's inode count.
        group.metadata.write().desc.free_inodes_count = 1;
        assert_errno!(group.free_inode(1025, InodeType::File), Errno::EIO);
        assert!(group.is_inode_allocated(1025).unwrap());
        assert_eq!(group.free_inodes_count(), 1);
        assert_errno!(f.ext2.read_inode(1026), Errno::EIO);

        // This fixture deliberately changed only cached metadata, not its
        // backing memory disk. Discard the synthetic dirty state on teardown.
        let mut metadata = group.metadata.write();
        metadata.desc.clear_dirty();
        metadata.inode_bitmap.clear_dirty();
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
    /// Whether both bitmaps above have been read from the device. The bitmaps
    /// are zeroed placeholders until then, so every path that consults them
    /// must call `BlockGroup::ensure_bitmaps_loaded` first.
    pub bitmaps_loaded: bool,
    /// Group-local block ranges freed since the block bitmap was last written
    /// back, with adjacent ranges merged.
    ///
    /// A volume without a journal has no ordering between the inode table and
    /// the bitmaps, so the blocks named here stay advertised as allocated until
    /// the inode that released them is durable (see
    /// [`BlockGroup::write_pre_inode_bitmap`]).
    pending_frees: Vec<Range<u16>>,
    /// Number of blocks covered by `pending_frees`.
    pending_frees_count: u16,
    /// Group-local inode indices freed since the inode bitmap was last written
    /// back, with adjacent indices merged. Same purpose as `pending_frees`.
    pending_inode_frees: Vec<Range<u16>>,
    /// Number of inodes covered by `pending_inode_frees`.
    pending_inode_frees_count: u16,
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

/// In-memory block group descriptor.
#[derive(Clone, Copy, Debug)]
pub(super) struct BlockGroupDesc {
    pub block_bitmap_bid: Ext2Bid,
    pub inode_bitmap_bid: Ext2Bid,
    pub inode_table_bid: Ext2Bid,
    pub free_blocks_count: u16,
    pub free_inodes_count: u16,
    pub used_dirs_count: u16,
}

impl From<RawBlockGroup> for BlockGroupDesc {
    fn from(raw: RawBlockGroup) -> Self {
        Self {
            block_bitmap_bid: raw.block_bitmap_bid,
            inode_bitmap_bid: raw.inode_bitmap_bid,
            inode_table_bid: raw.inode_table_bid,
            free_blocks_count: raw.free_blocks_count,
            free_inodes_count: raw.free_inodes_count,
            used_dirs_count: raw.used_dirs_count,
        }
    }
}

impl From<BlockGroupDesc> for RawBlockGroup {
    fn from(desc: BlockGroupDesc) -> Self {
        Self {
            block_bitmap_bid: desc.block_bitmap_bid,
            inode_bitmap_bid: desc.inode_bitmap_bid,
            inode_table_bid: desc.inode_table_bid,
            free_blocks_count: desc.free_blocks_count,
            free_inodes_count: desc.free_inodes_count,
            used_dirs_count: desc.used_dirs_count,
            pad: 0,
            reserved: [0; 3],
        }
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
    /// inode table) are usable.
    ///
    /// Without `FLEX_BG` the blocks must fall within the group's range and be
    /// marked in its own block bitmap. `FLEX_BG` lets a group keep its
    /// metadata in another group's range (mke2fs packs several groups'
    /// metadata into the leading groups), so only the filesystem-wide bounds
    /// are checked in that case; the owning group validates its bits when its
    /// own descriptor is loaded.
    fn validate_metadata_blocks(
        &self,
        block_bitmap: &IdBitmap,
        first_block: u32,
        last_block: u32,
        nr_inode_table_blocks_per_group: u32,
        flex_bg: bool,
        total_blocks: u32,
    ) -> Result<()> {
        let max_bit = last_block - first_block;

        let is_valid_fn = |bid: u32| -> bool {
            if flex_bg {
                return bid != 0 && bid < total_blocks;
            }
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

        if flex_bg {
            if self.inode_table_bid == 0 || self.inode_table_bid >= total_blocks {
                return_errno_with_message!(Errno::EINVAL, "inode table start out of range");
            }
            let table_end =
                u64::from(self.inode_table_bid) + u64::from(nr_inode_table_blocks_per_group);
            if table_end > u64::from(total_blocks) {
                return_errno_with_message!(
                    Errno::EINVAL,
                    "inode table extends beyond the filesystem"
                );
            }
            return Ok(());
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

fn validate_64bit_descriptor(group_descs: &USegment, offset: usize, sb: &SuperBlock) -> Result<()> {
    if !sb.feature_incompat().contains(FeatureInCompatSet::BIT64) {
        return Ok(());
    }
    for field in [0usize, 4, 8] {
        if group_descs
            .read_val::<u32>(offset + 32 + field)
            .map_err(|_| Error::with_message(Errno::EIO, "truncated 64-bit group descriptor"))?
            != 0
        {
            return_errno_with_message!(
                Errno::EOPNOTSUPP,
                "64-bit ext4 block number exceeds supported range"
            );
        }
    }
    Ok(())
}

fn validate_64bit_descriptor_from_device(
    device: &dyn BlockDevice,
    offset: usize,
    sb: &SuperBlock,
) -> Result<()> {
    if !sb.feature_incompat().contains(FeatureInCompatSet::BIT64) {
        return Ok(());
    }
    let mut high = [0u8; 12];
    device
        .read_bytes(offset + 32, &mut high)
        .map_err(|_| Error::with_message(Errno::EIO, "truncated 64-bit group descriptor"))?;
    for bytes in high.chunks_exact(4) {
        if u32::from_le_bytes(bytes.try_into().unwrap()) != 0 {
            return_errno_with_message!(
                Errno::EOPNOTSUPP,
                "64-bit ext4 block number exceeds supported range"
            );
        }
    }
    Ok(())
}

/// On-disk block group descriptor (32 bytes).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(super) struct RawBlockGroup {
    pub block_bitmap_bid: u32,  // bg_block_bitmap
    pub inode_bitmap_bid: u32,  // bg_inode_bitmap
    pub inode_table_bid: u32,   // bg_inode_table
    pub free_blocks_count: u16, // bg_free_blocks_count
    pub free_inodes_count: u16, // bg_free_inodes_count
    pub used_dirs_count: u16,   // bg_used_dirs_count
    pub pad: u16,               // bg_pad
    pub reserved: [u32; 3],     // bg_reserved
}

const_assert!(size_of::<RawBlockGroup>() == 32);

/// Backend of the inode table page cache in one block group.
struct InodeTableBackend {
    /// Physical block ID of `bg_inode_table`.
    inode_table_bid: Ext2Bid,
    /// Total inode table size in bytes (`nr_inodes_per_group * inode_size`).
    raw_inodes_size: usize,
    /// Block device handle for I/O.
    block_device: Arc<dyn BlockDevice>,
}

impl BlockAsPageCacheBackend for InodeTableBackend {
    fn submit_read_bio(
        &self,
        idx: usize,
        bio_segment: BioSegment,
        complete_fn: BioCompleteFn,
        io_batch: &mut IoBatch,
    ) -> Result<()> {
        if self.raw_inodes_size < idx * BLOCK_SIZE {
            return_errno_with_message!(Errno::EINVAL, "invalid read size");
        }
        let bid = Bid::new(self.inode_table_bid as u64) + idx as u64;
        self.block_device
            .read_blocks_async(bid, bio_segment, Some(complete_fn), io_batch)?;
        Ok(())
    }

    fn submit_write_bio(
        &self,
        idx: usize,
        bio_segment: BioSegment,
        complete_fn: BioCompleteFn,
        io_batch: &mut IoBatch,
    ) -> Result<()> {
        if self.raw_inodes_size < idx * BLOCK_SIZE {
            return_errno_with_message!(Errno::EINVAL, "invalid write size");
        }
        let bid = Bid::new(self.inode_table_bid as u64) + idx as u64;
        self.block_device
            .write_blocks_async(bid, bio_segment, Some(complete_fn), io_batch)?;
        Ok(())
    }
}
