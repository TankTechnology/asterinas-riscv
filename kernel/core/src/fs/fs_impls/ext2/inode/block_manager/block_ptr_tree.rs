// SPDX-License-Identifier: MPL-2.0

//! Logical-to-physical block translation via the ext2 block-pointer tree.

#![short_vis_path::add(inode)]

use device_id::{decode_device_numbers, encode_device_numbers};
use ostd::mm::io::util::HasVmReaderWriter;
use smallvec::SmallVec;

use super::indirect_block_manager::{IndirectBlock, IndirectBlockManager};
use crate::fs::ext2::{checksum, fs::Ext2, inode::RAW_BLOCK_PTRS_LEN, prelude::*};

const PTRS_PER_BLOCK: usize = BLOCK_SIZE / size_of::<u32>();
const SECTORS_PER_BLOCK: u32 = (BLOCK_SIZE / SECTOR_SIZE) as u32;
const MAX_BLOCK_POINTER_LEVELS: usize = 4;
const EXTENT_MAGIC: u16 = 0xf30a;
/// Size of an extent tree header (magic, entries, max_entries, depth, generation).
const EXTENT_HEADER_SIZE: usize = 12;
/// Size of one extent or index record.
const EXTENT_RECORD_SIZE: usize = 12;
/// Extent records that fit in one 4 KiB leaf block.
const EXTENT_LEAF_CAPACITY: usize = (BLOCK_SIZE - EXTENT_HEADER_SIZE) / EXTENT_RECORD_SIZE;
/// Deepest extent tree ext4 allows (`EXT_MAX_DEPTH`).
const MAX_EXTENT_DEPTH: u16 = 5;

/// One extent record while a hole is punched through a block.
#[derive(Clone, Copy)]
struct ExtentRecord {
    logical: u32,
    len: u32,
    physical: u32,
    unwritten: bool,
}

/// One index level on the way from the inode to an extent leaf.
struct ExtentIndexLevel {
    /// `None` for the in-line root.
    bid: Option<Ext2Bid>,
    bytes: [u8; BLOCK_SIZE],
    selected: usize,
    depth: u16,
    entries: usize,
    max_entries: usize,
    generation: u32,
}

/// An ext2 inode's block-pointer tree.
///
/// Each tree is rooted in the 15 raw block pointer slots stored in the inode.
/// The first 12 slots address data blocks directly,
/// while the remaining slots address progressively deeper indirect levels.
///
/// | Slot index | Region           | Addressable blocks (4 KiB blocks)     |
/// |------------|------------------|---------------------------------------|
/// | 0 – 11     | Direct           | 12 blocks                             |
/// | 12         | Single-indirect  | 1 024 blocks                          |
/// | 13         | Double-indirect  | 1 024² blocks                         |
/// | 14         | Triple-indirect  | 1 024³ blocks                         |
///
/// A zero pointer at any level represents an unallocated sparse range.
/// Non-zero pointers must refer either to data blocks at the leaf level
/// or to indirect metadata blocks at the level selected by the slot.
#[derive(Debug)]
pub(in inode) struct BlockPtrTree {
    raw_block_ptrs: Dirty<RawBlockPtrs>,
    indirect_blocks_manager: Mutex<IndirectBlockManager>,
    extent_tree: bool,
    /// Inode number, used as part of the extent block checksum seed.
    ino: Ext2Ino,
    /// Inode generation, used as part of the extent block checksum seed.
    generation: u32,
    /// Whether extent blocks carry CRC32C checksums.
    metadata_csum: bool,
    /// Filesystem checksum seed.
    csum_seed: u32,
}

impl BlockPtrTree {
    /// Creates a new block-pointer tree from the on-disk raw pointers.
    pub(in inode) fn new(
        raw_block_ptrs: RawBlockPtrs,
        fs: Weak<Ext2>,
        ino: Ext2Ino,
        generation: u32,
    ) -> Self {
        let (metadata_csum, csum_seed) = fs
            .upgrade()
            .map(|fs| {
                let sb = fs.super_block();
                (sb.has_metadata_csum(), sb.checksum_seed())
            })
            .unwrap_or((false, 0));
        Self {
            extent_tree: raw_block_ptrs.is_extent_tree(),
            raw_block_ptrs: Dirty::new(raw_block_ptrs),
            indirect_blocks_manager: Mutex::new(IndirectBlockManager::new(fs)),
            ino,
            generation,
            metadata_csum,
            csum_seed,
        }
    }

    /// Returns a reference to the raw on-disk block pointer state.
    pub(in inode) fn raw_block_ptrs(&self) -> &RawBlockPtrs {
        &self.raw_block_ptrs
    }

    /// Returns the inode number this tree belongs to.
    pub(in inode) const fn ino(&self) -> Ext2Ino {
        self.ino
    }

    /// Returns the inode generation.
    pub(in inode) const fn generation(&self) -> u32 {
        self.generation
    }

    /// Returns whether metadata checksums are enabled.
    pub(in inode) const fn metadata_csum(&self) -> bool {
        self.metadata_csum
    }

    /// Returns the filesystem checksum seed.
    pub(in inode) const fn csum_seed(&self) -> u32 {
        self.csum_seed
    }

    /// Returns whether the `RawBlockPtrs` is dirty.
    pub(super) fn is_dirty(&self) -> bool {
        self.raw_block_ptrs.is_dirty()
    }

    /// Adds sectors to `i_blocks` for blocks owned by external xattr values.
    pub(in inode) fn add_sector_count(&mut self, sectors: u32) -> Result<()> {
        self.raw_block_ptrs.sector_count = self
            .raw_block_ptrs
            .sector_count
            .checked_add(sectors)
            .ok_or_else(|| Error::with_message(Errno::EIO, "inode block count overflow"))?;
        Ok(())
    }

    /// Removes sectors from `i_blocks` when external xattr storage is freed.
    pub(in inode) fn sub_sector_count(&mut self, sectors: u32) {
        self.raw_block_ptrs.sector_count =
            self.raw_block_ptrs.sector_count.saturating_sub(sectors);
    }

    /// Clears the dirty flag for the raw on-disk block pointer state.
    pub(super) fn clear_dirty(&mut self) {
        self.raw_block_ptrs.clear_dirty();
    }

    /// Flushes all dirty cached indirect blocks to the device.
    pub(in inode) fn sync_indirect_blocks(&self) -> Result<()> {
        self.indirect_blocks_manager.lock().sync()
    }

    /// Resolves a logical block to a contiguous physical block range.
    pub(in inode) fn lookup_block_range(
        &self,
        iblock: Iblock,
        max_blocks: u32,
    ) -> Result<Range<Ext2Bid>> {
        if max_blocks == 0 {
            return_errno_with_message!(Errno::EINVAL, "zero block range requested");
        }

        if self.extent_tree {
            return self.lookup_extent_range(iblock, max_blocks);
        }

        let walk = self.walk_at(iblock)?;
        self.existing_contiguous_data_blocks(&walk, max_blocks)
    }

    /// Resolves a logical block to physical block (read-only).
    pub(in inode) fn lookup_block(&self, iblock: Iblock) -> Result<Option<Ext2Bid>> {
        let range = self.lookup_block_range(iblock, 1)?;
        Ok(if range.is_empty() {
            None
        } else {
            Some(range.start)
        })
    }

    /// Resolves a logical block to a contiguous physical block range,
    /// allocating new blocks if the mapping does not yet exist.
    ///
    /// The returned range starts at the physical block backing `iblock` and
    /// extends for at most `max_blocks` contiguous physical blocks, but may
    /// be shorter when the contiguous run ends or a leaf-pointer boundary is
    /// reached.
    ///
    /// - `Existing`: `iblock` was already allocated; the range covers the
    ///   contiguous prefix of physical blocks starting from that position.
    /// - `NewlyAllocated`: `iblock` was a hole; new data blocks (and any
    ///   required indirect metadata blocks) were allocated and linked into
    ///   the tree. The range covers the freshly allocated data blocks.
    //
    // TODO: When a leaf block range is partially allocated, we walk the indirect tree
    // twice: once to discover the existing prefix, then again to find the hole
    // and allocate. A cursor over the leaf block could eliminate the second walk.
    pub(in inode) fn resolve_block_range(
        &mut self,
        fs: &Ext2,
        iblock: Iblock,
        max_blocks: u32,
    ) -> Result<ResolvedBlockRange> {
        if max_blocks == 0 {
            return_errno_with_message!(Errno::EINVAL, "zero block allocation requested");
        }
        if self.extent_tree {
            return self.resolve_extent_range(fs, iblock, max_blocks);
        }

        let walk = self.walk_at(iblock)?;
        if walk.is_complete() {
            // The starting block already exists, so report its contiguous range.
            let existing_range = self.existing_contiguous_data_blocks(&walk, max_blocks)?;
            return Ok(ResolvedBlockRange::Existing(existing_range));
        }

        // Allocate the missing indirect metadata blocks, plus as many contiguous
        // data blocks as we can place before the current leaf boundary.
        let (indirect_blks, data_blks) = self.planned_allocation(&walk, max_blocks)?;
        let mut guard = self.allocate_blocks(fs, indirect_blks, data_blks, &walk)?;
        self.link_allocated_blocks(&guard, &walk)?;
        guard.commit();
        Ok(ResolvedBlockRange::NewlyAllocated(
            guard.data_blocks.clone(),
        ))
    }

    fn lookup_extent_range(&self, iblock: Iblock, max_blocks: u32) -> Result<Range<Ext2Bid>> {
        let root = self.raw_block_ptrs.extent_bytes();
        let (entries, max_entries, depth, _) = Self::parse_extent_header(&root, 4)?;
        if depth == 0 {
            return self.lookup_extent_leaf_range(&root, entries, max_entries, iblock, max_blocks);
        }

        // Walk the index levels until the leaf that owns `iblock`.
        let mut node = [0u8; BLOCK_SIZE];
        node[..RAW_BLOCK_PTRS_LEN * size_of::<u32>()].copy_from_slice(&root);
        let mut node_entries = entries;
        let mut node_depth = depth;
        loop {
            let Some((index, _)) =
                Self::select_extent_index(&node, node_entries, iblock, false)?
            else {
                // The block precedes every index key, so it is a hole.
                return Ok(iblock..iblock);
            };
            let child =
                Self::extent_index_leaf(&node, EXTENT_HEADER_SIZE + index * EXTENT_RECORD_SIZE)?;
            let child_bytes = self.read_extent_leaf(child)?;
            let (child_entries, child_max_entries, child_depth, _) =
                Self::parse_extent_header(&child_bytes, EXTENT_LEAF_CAPACITY)?;
            if child_depth.checked_add(1) != Some(node_depth) {
                return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent depth");
            }
            if child_depth == 0 {
                return self.lookup_extent_leaf_range(
                    &child_bytes,
                    child_entries,
                    child_max_entries,
                    iblock,
                    max_blocks,
                );
            }
            node = child_bytes;
            node_entries = child_entries;
            node_depth = child_depth;
        }
    }

    fn lookup_extent_leaf_range(
        &self,
        bytes: &[u8],
        entries: usize,
        max_entries: usize,
        iblock: Iblock,
        max_blocks: u32,
    ) -> Result<Range<Ext2Bid>> {
        if bytes.len() < 12
            || u16::from_le_bytes([bytes[0], bytes[1]]) != EXTENT_MAGIC
            || entries > max_entries
            || max_entries > (BLOCK_SIZE - 12) / 12
        {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent leaf");
        }
        let mut previous_end = 0u32;
        for index in 0..entries {
            let offset = 12 + index * 12;
            let logical = u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap());
            let raw_length = u16::from_le_bytes(bytes[offset + 4..offset + 6].try_into().unwrap());
            let length = u32::from(raw_length & 0x7fff);
            if length == 0 || logical < previous_end {
                return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent range");
            }
            let physical_hi = u16::from_le_bytes(bytes[offset + 6..offset + 8].try_into().unwrap());
            let physical_lo =
                u32::from_le_bytes(bytes[offset + 8..offset + 12].try_into().unwrap());
            let physical = (u64::from(physical_hi) << 32) | u64::from(physical_lo);
            let physical_end = physical
                .checked_add(u64::from(length))
                .ok_or_else(|| Error::with_message(Errno::EUCLEAN, "ext4 extent block overflow"))?;
            let physical_end = u32::try_from(physical_end)
                .map_err(|_| Error::with_message(Errno::EOPNOTSUPP, "64-bit ext4 extent block"))?;
            let logical_end = logical.checked_add(length).ok_or_else(|| {
                Error::with_message(Errno::EUCLEAN, "ext4 extent logical overflow")
            })?;
            if iblock >= logical && iblock < logical_end {
                let start = physical
                    .checked_add(u64::from(iblock - logical))
                    .and_then(|value| u32::try_from(value).ok())
                    .ok_or_else(|| {
                        Error::with_message(Errno::EOPNOTSUPP, "64-bit ext4 extent block")
                    })?;
                let available = length - (iblock - logical);
                return Ok(start..start + available.min(max_blocks));
            }
            previous_end = logical.checked_add(length).ok_or_else(|| {
                Error::with_message(Errno::EUCLEAN, "ext4 extent logical overflow")
            })?;
            let _ = physical_end;
        }
        Ok(iblock..iblock)
    }

    /// Allocates data for an inline, depth-zero extent tree.
    ///
    /// This covers the common small-file case used during Debian boot (for
    /// example `/etc/machine-id`). Extent index blocks remain unsupported.
    fn resolve_extent_range(
        &mut self,
        fs: &Ext2,
        iblock: Iblock,
        max_blocks: u32,
    ) -> Result<ResolvedBlockRange> {
        let bytes = self.raw_block_ptrs.extent_bytes();
        let entries = u16::from_le_bytes([bytes[2], bytes[3]]) as usize;
        let max_entries = u16::from_le_bytes([bytes[4], bytes[5]]) as usize;
        let depth = u16::from_le_bytes([bytes[6], bytes[7]]);
        if entries > max_entries
            || entries > 4
            || max_entries == 0
            || depth > MAX_EXTENT_DEPTH
        {
            return_errno_with_message!(Errno::EOPNOTSUPP, "unsupported ext4 extent tree");
        }
        if depth > 0 {
            // Page-cache writeback resolves already-mapped ranges before it
            // writes them, and directories such as `/var/lib/dpkg/info` carry
            // a depth-one tree. An unmapped block is either an append or a
            // hole left by a punch; both insert a record into the right leaf,
            // splitting it (and its index parents) when they are full.
            let range = self.lookup_extent_range(iblock, max_blocks)?;
            if range.is_empty() {
                return self.allocate_in_extent_tree(fs, iblock, max_blocks);
            }
            return Ok(ResolvedBlockRange::Existing(range));
        }

        let mut next_logical = 0u32;
        let mut goal = self.raw_block_ptrs.block_ptrs[0];
        for index in 0..entries {
            let offset = 12 + index * 12;
            let logical = u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap());
            let raw_length = u16::from_le_bytes(bytes[offset + 4..offset + 6].try_into().unwrap());
            let length = u32::from(raw_length & 0x7fff);
            if length == 0 {
                return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent range");
            }
            // Records must be ordered but need not be contiguous: a punched
            // hole leaves a logical gap.
            if index != 0 && logical < next_logical {
                return_errno_with_message!(Errno::EUCLEAN, "out-of-order ext4 extent range");
            }
            let physical_hi = u16::from_le_bytes(bytes[offset + 6..offset + 8].try_into().unwrap());
            let physical_lo =
                u32::from_le_bytes(bytes[offset + 8..offset + 12].try_into().unwrap());
            let physical = (u64::from(physical_hi) << 32) | u64::from(physical_lo);
            let physical = u32::try_from(physical)
                .map_err(|_| Error::with_message(Errno::EOPNOTSUPP, "64-bit ext4 extent block"))?;
            let logical_end = logical.checked_add(length).ok_or_else(|| {
                Error::with_message(Errno::EUCLEAN, "ext4 extent logical overflow")
            })?;
            if iblock >= logical && iblock < logical_end {
                let start = physical.checked_add(iblock - logical).ok_or_else(|| {
                    Error::with_message(Errno::EUCLEAN, "ext4 extent block overflow")
                })?;
                return Ok(ResolvedBlockRange::Existing(
                    start..start + (length - (iblock - logical)).min(max_blocks),
                ));
            }
            if iblock < logical {
                // A hole before this extent: allocate it in place, bounded by
                // the next extent's first logical block.
                let gap_goal = if index == 0 { physical } else { goal };
                return self.allocate_inline_hole(
                    fs,
                    iblock,
                    max_blocks,
                    Some(logical),
                    index,
                    gap_goal,
                );
            }
            next_logical = logical.checked_add(length).ok_or_else(|| {
                Error::with_message(Errno::EUCLEAN, "ext4 extent logical overflow")
            })?;
            goal = physical.saturating_add(length);
        }

        if iblock > next_logical {
            // A hole after the last extent (for example a punched tail that
            // the file size still covers).
            return self.allocate_inline_hole(fs, iblock, max_blocks, None, entries, goal);
        }
        if iblock != next_logical {
            return_errno_with_message!(Errno::EOPNOTSUPP, "non-append ext4 extent allocation");
        }
        if entries == max_entries {
            // The inline root is full. Promote it to a depth-one tree and
            // append there, which is what ext4 does when a file outgrows the
            // in-inode extent array.
            self.promote_inline_extents(fs)?;
            return self.allocate_in_extent_tree(fs, iblock, max_blocks);
        }

        let count = max_blocks.min(0x7fff).min((max_entries - entries) as u32);
        let data_blocks = fs.alloc_blocks(count, goal)?;
        Self::zero_new_blocks(fs, &data_blocks)?;
        let mut guard = BlockAllocGuard::new(fs, 0);
        guard.track_data_blocks(data_blocks.clone());
        self.append_extent(next_logical, &data_blocks)?;
        self.raw_block_ptrs.sector_count = self
            .raw_block_ptrs
            .sector_count
            .checked_add(data_blocks.len() as u32 * SECTORS_PER_BLOCK)
            .ok_or_else(|| Error::with_message(Errno::EIO, "inode block count overflow"))?;
        guard.commit();
        Ok(ResolvedBlockRange::NewlyAllocated(data_blocks))
    }

    fn append_extent(&mut self, logical: u32, data_blocks: &Range<Ext2Bid>) -> Result<()> {
        let mut bytes = self.raw_block_ptrs.extent_bytes();
        let entries = u16::from_le_bytes([bytes[2], bytes[3]]);
        let offset = 12 + usize::from(entries) * 12;
        let length = u16::try_from(data_blocks.len())
            .map_err(|_| Error::with_message(Errno::EOVERFLOW, "extent length overflow"))?;
        bytes[offset..offset + 4].copy_from_slice(&logical.to_le_bytes());
        bytes[offset + 4..offset + 6].copy_from_slice(&length.to_le_bytes());
        bytes[offset + 6..offset + 8].copy_from_slice(&0u16.to_le_bytes());
        bytes[offset + 8..offset + 12].copy_from_slice(&data_blocks.start.to_le_bytes());
        let new_entries = entries
            .checked_add(1)
            .ok_or_else(|| Error::with_message(Errno::EOVERFLOW, "extent entry overflow"))?;
        bytes[2..4].copy_from_slice(&new_entries.to_le_bytes());
        for (index, value) in self.raw_block_ptrs.block_ptrs.iter_mut().enumerate() {
            *value = u32::from_le_bytes(bytes[index * 4..index * 4 + 4].try_into().unwrap());
        }
        Ok(())
    }

    /// Allocates data for a hole inside an in-line extent array.
    ///
    /// The new record is inserted at `insert_at`, so the records above it
    /// shift right. `upper_logical` bounds the allocation to the next
    /// extent (when there is one); the caller re-resolves the rest of the
    /// range afterwards.
    fn allocate_inline_hole(
        &mut self,
        fs: &Ext2,
        iblock: Iblock,
        max_blocks: u32,
        upper_logical: Option<u32>,
        insert_at: usize,
        goal: u32,
    ) -> Result<ResolvedBlockRange> {
        let bytes = self.raw_block_ptrs.extent_bytes();
        let entries = u16::from_le_bytes([bytes[2], bytes[3]]) as usize;
        let max_entries = u16::from_le_bytes([bytes[4], bytes[5]]) as usize;
        if entries >= max_entries {
            return_errno_with_message!(Errno::EOPNOTSUPP, "inline extent array is full");
        }

        let mut count = max_blocks.min(0x7fff).min((max_entries - entries) as u32);
        if let Some(upper) = upper_logical {
            count = count.min(upper.saturating_sub(iblock));
        }
        if count == 0 {
            return_errno_with_message!(Errno::EOPNOTSUPP, "no room for the hole");
        }

        let data_blocks = fs.alloc_blocks(count, goal)?;
        Self::zero_new_blocks(fs, &data_blocks)?;
        let mut guard = BlockAllocGuard::new(fs, 0);
        guard.track_data_blocks(data_blocks.clone());

        let mut out = [0u8; RAW_BLOCK_PTRS_LEN * size_of::<u32>()];
        out.copy_from_slice(&bytes);
        Self::insert_extent_record(&mut out, entries, insert_at, iblock, data_blocks.start, count)?;
        for (index, value) in self.raw_block_ptrs.block_ptrs.iter_mut().enumerate() {
            *value = u32::from_le_bytes(out[index * 4..index * 4 + 4].try_into().unwrap());
        }
        self.raw_block_ptrs.sector_count = self
            .raw_block_ptrs
            .sector_count
            .checked_add(count * SECTORS_PER_BLOCK)
            .ok_or_else(|| Error::with_message(Errno::EIO, "inode block count overflow"))?;
        guard.commit();
        Ok(ResolvedBlockRange::NewlyAllocated(data_blocks))
    }

    /// Inserts one extent record at `insert_at`, shifting later records.
    fn insert_extent_record(
        bytes: &mut [u8],
        entries: usize,
        insert_at: usize,
        logical: u32,
        physical: Ext2Bid,
        len: u32,
    ) -> Result<()> {
        if insert_at > entries {
            return_errno_with_message!(Errno::EINVAL, "extent insert position out of range");
        }
        for index in (insert_at..entries).rev() {
            let src = EXTENT_HEADER_SIZE + index * EXTENT_RECORD_SIZE;
            let dst = src + EXTENT_RECORD_SIZE;
            bytes.copy_within(src..src + EXTENT_RECORD_SIZE, dst);
        }
        let offset = EXTENT_HEADER_SIZE + insert_at * EXTENT_RECORD_SIZE;
        bytes[offset..offset + 4].copy_from_slice(&logical.to_le_bytes());
        let raw_len = u16::try_from(len & 0x7fff)
            .map_err(|_| Error::with_message(Errno::EOVERFLOW, "extent length overflow"))?;
        bytes[offset + 4..offset + 6].copy_from_slice(&raw_len.to_le_bytes());
        bytes[offset + 6..offset + 8].copy_from_slice(&0u16.to_le_bytes());
        bytes[offset + 8..offset + 12].copy_from_slice(&physical.to_le_bytes());
        let new_entries = u16::try_from(entries + 1)
            .map_err(|_| Error::with_message(Errno::EOVERFLOW, "extent entry overflow"))?;
        bytes[2..4].copy_from_slice(&new_entries.to_le_bytes());
        Ok(())
    }

    /// Allocates data for a hole or an append in an extent tree.
    ///
    /// The leaf that owns `iblock` is selected by descending the index levels
    /// (the last key not above the block, or the first child when the block
    /// precedes every key). The new record is inserted in logical order,
    /// bounded by the next record or the next sibling key, so an allocation
    /// never crosses into a following leaf.
    ///
    /// A full leaf is split in two and the new index entry is propagated
    /// upwards; full index blocks are split the same way. When the in-line
    /// root is full, its records move into a fresh index block and the root
    /// grows one level, which is how ext4 builds depth-two and deeper trees.
    fn allocate_in_extent_tree(
        &mut self,
        fs: &Ext2,
        iblock: Iblock,
        max_blocks: u32,
    ) -> Result<ResolvedBlockRange> {
        let root = self.raw_block_ptrs.extent_bytes();
        let (root_entries, root_max_entries, root_depth, root_generation) =
            Self::parse_extent_header(&root, 4)?;
        if root_depth == 0 || root_entries == 0 {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent index");
        }

        let mut levels: Vec<ExtentIndexLevel> = Vec::new();
        let mut node_bytes = [0u8; BLOCK_SIZE];
        node_bytes[..RAW_BLOCK_PTRS_LEN * size_of::<u32>()].copy_from_slice(&root);
        let mut node_bid = None;
        let mut node_entries = root_entries;
        let mut node_max_entries = root_max_entries;
        let mut node_depth = root_depth;
        let mut node_generation = root_generation;
        let mut upper = None;

        let (leaf, leaf_bytes, leaf_entries, leaf_max_entries, leaf_generation) = loop {
            let (selected, next_key) =
                Self::select_extent_index(&node_bytes, node_entries, iblock, true)?
                    .ok_or_else(|| {
                        Error::with_message(Errno::EUCLEAN, "empty ext4 extent index")
                    })?;
            if let Some(key) = next_key {
                upper = Some(upper.map_or(key, |bound: u32| bound.min(key)));
            }
            let child = Self::extent_index_leaf(
                &node_bytes,
                EXTENT_HEADER_SIZE + selected * EXTENT_RECORD_SIZE,
            )?;
            let child_bytes = self.read_extent_leaf(child)?;
            let (child_entries, child_max_entries, child_depth, child_generation) =
                Self::parse_extent_header(&child_bytes, EXTENT_LEAF_CAPACITY)?;
            if child_depth.checked_add(1) != Some(node_depth) {
                return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent depth");
            }
            levels.push(ExtentIndexLevel {
                bid: node_bid,
                bytes: node_bytes,
                selected,
                depth: node_depth,
                entries: node_entries,
                max_entries: node_max_entries,
                generation: node_generation,
            });
            if child_depth == 0 {
                break (
                    child,
                    child_bytes,
                    child_entries,
                    child_max_entries,
                    child_generation,
                );
            }
            node_bid = Some(child);
            node_bytes = child_bytes;
            node_entries = child_entries;
            node_max_entries = child_max_entries;
            node_depth = child_depth;
            node_generation = child_generation;
        };

        let mut records: Vec<ExtentRecord> = Vec::with_capacity(leaf_entries + 1);
        let mut insert_at = leaf_entries;
        let mut goal = 0u32;
        let mut insert_goal = 0u32;
        let mut previous_end = 0u32;
        for position in 0..leaf_entries {
            let offset = EXTENT_HEADER_SIZE + position * EXTENT_RECORD_SIZE;
            let logical = u32::from_le_bytes(leaf_bytes[offset..offset + 4].try_into().unwrap());
            let raw_len = u16::from_le_bytes([leaf_bytes[offset + 4], leaf_bytes[offset + 5]]);
            let len = u32::from(raw_len & 0x7fff);
            let physical = Self::extent_record_physical(&leaf_bytes, offset)?;
            let logical_end = logical.checked_add(len).ok_or_else(|| {
                Error::with_message(Errno::EUCLEAN, "ext4 extent logical overflow")
            })?;
            if len == 0 || (position != 0 && logical < previous_end) {
                return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent range");
            }
            if iblock >= logical && iblock < logical_end {
                let start = physical.checked_add(iblock - logical).ok_or_else(|| {
                    Error::with_message(Errno::EUCLEAN, "ext4 extent block overflow")
                })?;
                return Ok(ResolvedBlockRange::Existing(
                    start..start + (len - (iblock - logical)).min(max_blocks),
                ));
            }
            if insert_at == leaf_entries && iblock < logical {
                insert_at = position;
                upper = Some(upper.map_or(logical, |bound: u32| bound.min(logical)));
                insert_goal = if position == 0 { physical } else { goal };
            }
            previous_end = logical_end;
            goal = physical.saturating_add(len);
            records.push(ExtentRecord {
                logical,
                len,
                physical,
                unwritten: raw_len & 0x8000 != 0,
            });
        }
        if insert_at == leaf_entries {
            insert_goal = goal;
        }

        let bound = upper.unwrap_or(u32::MAX);
        let count = max_blocks.min(0x7fff).min(bound.saturating_sub(iblock));
        if count == 0 {
            return_errno_with_message!(Errno::EOPNOTSUPP, "no room for the hole");
        }

        let data_blocks = fs.alloc_blocks(count, insert_goal)?;
        Self::zero_new_blocks(fs, &data_blocks)?;
        let mut guard = BlockAllocGuard::new(fs, 0);
        guard.track_data_blocks(data_blocks.clone());
        records.insert(
            insert_at,
            ExtentRecord {
                logical: iblock,
                len: count,
                physical: data_blocks.start,
                unwritten: false,
            },
        );

        let first_key = records[0].logical;

        if records.len() <= leaf_max_entries {
            let mut bytes = [0u8; BLOCK_SIZE];
            Self::encode_extent_block(&mut bytes, &records, leaf_max_entries, leaf_generation)?;
            self.write_extent_leaf(leaf, &bytes)?;
            if insert_at == 0 {
                self.lower_extent_keys(&mut levels, first_key)?;
            }
            self.add_sectors(count)?;
            guard.commit();
            return Ok(ResolvedBlockRange::NewlyAllocated(data_blocks));
        }

        // Splitting may need a new leaf and one block per index level that
        // splits, plus one when the in-line root grows. Allocate the whole
        // pool before the first write so a later allocation failure cannot
        // leave the tree half-updated.
        let pool_len = levels.len() as u32 + 2;
        let pool = fs.alloc_blocks(pool_len, insert_goal)?;
        guard.extend_indirect_blocks(pool.clone());
        let mut next_indirect = pool.start;

        // Split the overfull leaf into two balanced leaves. The second half
        // gets a new leaf block, and its first key is propagated upwards.
        let split_at = records.len() / 2;
        let new_leaf = next_indirect;
        next_indirect += 1;

        let mut first_bytes = [0u8; BLOCK_SIZE];
        Self::encode_extent_block(
            &mut first_bytes,
            &records[..split_at],
            leaf_max_entries,
            leaf_generation,
        )?;
        let mut second_bytes = [0u8; BLOCK_SIZE];
        Self::encode_extent_block(
            &mut second_bytes,
            &records[split_at..],
            EXTENT_LEAF_CAPACITY,
            leaf_generation,
        )?;
        self.write_extent_leaf(leaf, &first_bytes)?;
        self.write_extent_leaf(new_leaf, &second_bytes)?;
        if insert_at == 0 {
            self.lower_extent_keys(&mut levels, first_key)?;
        }

        let mut pending = (records[split_at].logical, new_leaf);
        let mut level_index = levels.len();
        while level_index > 0 {
            level_index -= 1;
            let is_root = level_index == 0;
            let mut entries: Vec<(u32, Ext2Bid)> = Vec::new();
            {
                let level = &levels[level_index];
                for index in 0..level.entries {
                    let offset = EXTENT_HEADER_SIZE + index * EXTENT_RECORD_SIZE;
                    let key =
                        u32::from_le_bytes(level.bytes[offset..offset + 4].try_into().unwrap());
                    let child = Self::extent_index_leaf(&level.bytes, offset)?;
                    entries.push((key, child));
                }
            }
            let insert_pos = levels[level_index].selected + 1;
            entries.insert(insert_pos, pending);
            let level = &mut levels[level_index];

            if is_root && level.entries >= level.max_entries {
                // The in-line root is full: move its records into a fresh
                // index block and make the root a one-entry index of it.
                let new_depth = level.depth + 1;
                if new_depth > MAX_EXTENT_DEPTH {
                    return_errno_with_message!(
                        Errno::EOPNOTSUPP,
                        "extent tree depth limit reached"
                    );
                }
                let root_child = next_indirect;
                next_indirect += 1;
                let mut child_bytes = [0u8; BLOCK_SIZE];
                Self::encode_extent_index_block(
                    &mut child_bytes,
                    &entries,
                    EXTENT_LEAF_CAPACITY,
                    level.depth,
                    level.generation,
                )?;
                self.write_extent_leaf(root_child, &child_bytes)?;

                let mut root_bytes = [0u8; BLOCK_SIZE];
                Self::encode_extent_index_block(
                    &mut root_bytes,
                    &[(entries[0].0, root_child)],
                    4,
                    new_depth,
                    level.generation,
                )?;
                self.store_extent_root_block(&root_bytes);
                level.depth = new_depth;
                level.entries = 1;
                self.finish_extent_allocation(fs, &mut guard, &pool, next_indirect, count)?;
                return Ok(ResolvedBlockRange::NewlyAllocated(data_blocks));
            }

            if !is_root && level.entries >= level.max_entries {
                // Split this full index block and propagate its second half.
                let split_at = entries.len() / 2;
                let new_index = next_indirect;
                next_indirect += 1;
                let mut first_bytes = [0u8; BLOCK_SIZE];
                Self::encode_extent_index_block(
                    &mut first_bytes,
                    &entries[..split_at],
                    level.max_entries,
                    level.depth,
                    level.generation,
                )?;
                let mut second_bytes = [0u8; BLOCK_SIZE];
                Self::encode_extent_index_block(
                    &mut second_bytes,
                    &entries[split_at..],
                    EXTENT_LEAF_CAPACITY,
                    level.depth,
                    level.generation,
                )?;
                let bid = level.bid.expect("non-root index level has a block");
                self.write_extent_leaf(bid, &first_bytes)?;
                self.write_extent_leaf(new_index, &second_bytes)?;
                level.entries = split_at;
                pending = (entries[split_at].0, new_index);
                continue;
            }

            let mut bytes = level.bytes;
            Self::encode_extent_index_block(
                &mut bytes,
                &entries,
                level.max_entries,
                level.depth,
                level.generation,
            )?;
            level.entries = entries.len();
            self.write_extent_level(level.bid, &bytes)?;
            self.finish_extent_allocation(fs, &mut guard, &pool, next_indirect, count)?;
            return Ok(ResolvedBlockRange::NewlyAllocated(data_blocks));
        }

        unreachable!("the in-line root always terminates the split propagation")
    }

    /// Adds newly allocated data and metadata blocks to the inode's sector
    /// count.
    fn add_sectors(&mut self, blocks: u32) -> Result<()> {
        self.raw_block_ptrs.sector_count = self
            .raw_block_ptrs
            .sector_count
            .checked_add(blocks * SECTORS_PER_BLOCK)
            .ok_or_else(|| Error::with_message(Errno::EIO, "inode block count overflow"))?;
        Ok(())
    }

    /// Frees the unused tail of a pre-allocated metadata pool, accounts the
    /// blocks that were linked into the tree and commits the allocation.
    fn finish_extent_allocation(
        &mut self,
        fs: &Ext2,
        guard: &mut BlockAllocGuard<'_>,
        pool: &Range<Ext2Bid>,
        next_indirect: Ext2Bid,
        data_blocks: u32,
    ) -> Result<()> {
        let used = next_indirect.saturating_sub(pool.start);
        if next_indirect < pool.end {
            fs.free_blocks(next_indirect, pool.end - next_indirect)?;
        }
        self.add_sectors(data_blocks + used)?;
        guard.commit();
        Ok(())
    }

    /// Rewrites an index level (the in-line root when `bid` is `None`).
    fn write_extent_level(&mut self, bid: Option<Ext2Bid>, bytes: &[u8; BLOCK_SIZE]) -> Result<()> {
        match bid {
            Some(bid) => self.write_extent_leaf(bid, bytes),
            None => {
                self.store_extent_root_block(bytes);
                Ok(())
            }
        }
    }

    /// Copies the first 60 bytes of an extent block into the inode's `i_block`.
    fn store_extent_root_block(&mut self, bytes: &[u8; BLOCK_SIZE]) {
        for (index, value) in self.raw_block_ptrs.block_ptrs.iter_mut().enumerate() {
            *value = u32::from_le_bytes(bytes[index * 4..index * 4 + 4].try_into().unwrap());
        }
    }

    /// Lowers the first key of each index level that owns the modified leaf.
    ///
    /// Inserting a record before the current first extent moves the subtree's
    /// lower boundary. The change only propagates while the child sits at
    /// index zero, because any higher child is already bounded by an earlier
    /// key.
    fn lower_extent_keys(
        &mut self,
        levels: &mut [ExtentIndexLevel],
        first_key: u32,
    ) -> Result<()> {
        for level in levels.iter_mut().rev() {
            if level.selected != 0 {
                break;
            }
            let offset = EXTENT_HEADER_SIZE;
            let old_key =
                u32::from_le_bytes(level.bytes[offset..offset + 4].try_into().unwrap());
            if old_key <= first_key {
                break;
            }
            level.bytes[offset..offset + 4].copy_from_slice(&first_key.to_le_bytes());
            self.write_extent_level(level.bid, &level.bytes)?;
        }
        Ok(())
    }

    /// Parses an extent header. `capacity` bounds `eh_max` for the node kind
    /// (four records in the inode, [`EXTENT_LEAF_CAPACITY`] in a block).
    fn parse_extent_header(
        bytes: &[u8],
        capacity: usize,
    ) -> Result<(usize, usize, u16, u32)> {
        if bytes.len() < EXTENT_HEADER_SIZE
            || u16::from_le_bytes([bytes[0], bytes[1]]) != EXTENT_MAGIC
        {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent header");
        }
        let entries = u16::from_le_bytes([bytes[2], bytes[3]]) as usize;
        let max_entries = u16::from_le_bytes([bytes[4], bytes[5]]) as usize;
        let depth = u16::from_le_bytes([bytes[6], bytes[7]]);
        if entries > max_entries
            || max_entries == 0
            || max_entries > capacity
            || depth > MAX_EXTENT_DEPTH
        {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent header");
        }
        let generation = u32::from_le_bytes(bytes[8..12].try_into().unwrap());
        Ok((entries, max_entries, depth, generation))
    }

    /// Returns the key of one index record.
    fn extent_index_key(bytes: &[u8], index: usize) -> Result<u32> {
        let offset = EXTENT_HEADER_SIZE + index * EXTENT_RECORD_SIZE;
        if offset + EXTENT_RECORD_SIZE > bytes.len() {
            return_errno_with_message!(Errno::EUCLEAN, "ext4 extent index out of range");
        }
        Ok(u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap()))
    }

    /// Selects the index entry that owns `iblock`.
    ///
    /// The result is the last key not above `iblock`, or the first entry when
    /// `default_first` is set and every key is above it. The second value is
    /// the following key, which bounds a hole that starts at `iblock`.
    fn select_extent_index(
        bytes: &[u8],
        entries: usize,
        iblock: u32,
        default_first: bool,
    ) -> Result<Option<(usize, Option<u32>)>> {
        let mut selected = None;
        let mut previous = 0u32;
        for index in 0..entries {
            let key = Self::extent_index_key(bytes, index)?;
            if index != 0 && key < previous {
                return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent index");
            }
            previous = key;
            if key <= iblock {
                selected = Some(index);
            }
        }
        let index = match selected {
            Some(index) => index,
            None if default_first && entries > 0 => 0,
            None => return Ok(None),
        };
        let next = if index + 1 < entries {
            Some(Self::extent_index_key(bytes, index + 1)?)
        } else {
            None
        };
        Ok(Some((index, next)))
    }

    /// Serializes index records into an extent index block.
    fn encode_extent_index_block(
        out: &mut [u8; BLOCK_SIZE],
        entries: &[(u32, Ext2Bid)],
        max_entries: usize,
        depth: u16,
        generation: u32,
    ) -> Result<()> {
        if entries.len() > max_entries || max_entries > EXTENT_LEAF_CAPACITY {
            return_errno_with_message!(Errno::EOPNOTSUPP, "extent index overflow");
        }
        if depth == 0 || depth > MAX_EXTENT_DEPTH {
            return_errno_with_message!(Errno::EINVAL, "invalid extent index depth");
        }
        out.fill(0);
        out[0..2].copy_from_slice(&EXTENT_MAGIC.to_le_bytes());
        out[2..4].copy_from_slice(&(entries.len() as u16).to_le_bytes());
        out[4..6].copy_from_slice(&(max_entries as u16).to_le_bytes());
        out[6..8].copy_from_slice(&depth.to_le_bytes());
        out[8..12].copy_from_slice(&generation.to_le_bytes());
        for (index, (key, child)) in entries.iter().enumerate() {
            let offset = EXTENT_HEADER_SIZE + index * EXTENT_RECORD_SIZE;
            out[offset..offset + 4].copy_from_slice(&key.to_le_bytes());
            out[offset + 4..offset + 8].copy_from_slice(&child.to_le_bytes());
            out[offset + 8..offset + 10].copy_from_slice(&0u16.to_le_bytes());
            out[offset + 10..offset + 12].copy_from_slice(&0u16.to_le_bytes());
        }
        Ok(())
    }

    /// Promotes a full inline extent tree to a depth-one tree.
    ///
    /// The inline records move into a freshly allocated leaf and the inode's
    /// `i_block` becomes a one-entry index pointing at it, with the same
    /// generation number. This is how ext4 represents files with more than
    /// four extents, and it lets later appends use the leaf's capacity.
    fn promote_inline_extents(&mut self, fs: &Ext2) -> Result<()> {
        let root = self.raw_block_ptrs.extent_bytes();
        let entries = u16::from_le_bytes([root[2], root[3]]) as usize;
        if entries == 0 || entries > 4 {
            return_errno_with_message!(Errno::EUCLEAN, "invalid inline extent tree");
        }
        let generation = u32::from_le_bytes(root[8..12].try_into().unwrap());
        let first_logical = u32::from_le_bytes(root[12..16].try_into().unwrap());
        let last_offset = EXTENT_HEADER_SIZE + (entries - 1) * EXTENT_RECORD_SIZE;
        let last_length = u32::from(
            u16::from_le_bytes([root[last_offset + 4], root[last_offset + 5]]) & 0x7fff,
        );
        let last_physical = Self::extent_record_physical(&root, last_offset)?;
        let goal = u32::try_from(u64::from(last_physical) + u64::from(last_length))
            .map_err(|_| Error::with_message(Errno::EOPNOTSUPP, "64-bit ext4 extent block"))?;
        let leaf_blocks = fs.alloc_blocks(1, goal)?;
        let leaf = leaf_blocks.start;
        let mut guard = BlockAllocGuard::new(fs, 1);
        guard.extend_indirect_blocks(leaf_blocks.clone());

        Self::zero_new_blocks(fs, &leaf_blocks)?;
        let mut leaf_bytes = [0u8; BLOCK_SIZE];
        leaf_bytes[0..2].copy_from_slice(&EXTENT_MAGIC.to_le_bytes());
        leaf_bytes[2..4].copy_from_slice(&(entries as u16).to_le_bytes());
        leaf_bytes[4..6].copy_from_slice(&(EXTENT_LEAF_CAPACITY as u16).to_le_bytes());
        leaf_bytes[6..8].copy_from_slice(&0u16.to_le_bytes());
        leaf_bytes[8..12].copy_from_slice(&generation.to_le_bytes());
        let records_end = EXTENT_HEADER_SIZE + entries * EXTENT_RECORD_SIZE;
        leaf_bytes[EXTENT_HEADER_SIZE..records_end]
            .copy_from_slice(&root[EXTENT_HEADER_SIZE..records_end]);
        self.write_extent_leaf(leaf, &leaf_bytes)?;

        let mut new_root = [0u8; RAW_BLOCK_PTRS_LEN * size_of::<u32>()];
        new_root[0..2].copy_from_slice(&EXTENT_MAGIC.to_le_bytes());
        new_root[2..4].copy_from_slice(&1u16.to_le_bytes());
        // The inode stores four index entries at most.
        new_root[4..6].copy_from_slice(&4u16.to_le_bytes());
        new_root[6..8].copy_from_slice(&1u16.to_le_bytes());
        new_root[8..12].copy_from_slice(&generation.to_le_bytes());
        new_root[12..16].copy_from_slice(&first_logical.to_le_bytes());
        new_root[16..20].copy_from_slice(&leaf.to_le_bytes());
        new_root[20..22].copy_from_slice(&0u16.to_le_bytes());
        new_root[22..24].copy_from_slice(&0u16.to_le_bytes());
        for (index, value) in self.raw_block_ptrs.block_ptrs.iter_mut().enumerate() {
            *value = u32::from_le_bytes(new_root[index * 4..index * 4 + 4].try_into().unwrap());
        }
        self.raw_block_ptrs.sector_count = self
            .raw_block_ptrs
            .sector_count
            .checked_add(SECTORS_PER_BLOCK)
            .ok_or_else(|| Error::with_message(Errno::EIO, "inode block count overflow"))?;
        guard.commit();
        Ok(())
    }

    /// Returns the physical block of an index record at `offset`.
    fn extent_index_leaf(bytes: &[u8], offset: usize) -> Result<Ext2Bid> {
        let leaf_lo = u32::from_le_bytes(bytes[offset + 4..offset + 8].try_into().unwrap());
        let leaf_hi = u16::from_le_bytes([bytes[offset + 8], bytes[offset + 9]]);
        let leaf = (u64::from(leaf_hi) << 32) | u64::from(leaf_lo);
        u32::try_from(leaf)
            .map_err(|_| Error::with_message(Errno::EOPNOTSUPP, "64-bit ext4 extent block"))
    }

    /// Returns the physical start block of an extent record at `offset`.
    fn extent_record_physical(bytes: &[u8], offset: usize) -> Result<Ext2Bid> {
        let physical_hi = u16::from_le_bytes([bytes[offset + 6], bytes[offset + 7]]);
        let physical_lo = u32::from_le_bytes(bytes[offset + 8..offset + 12].try_into().unwrap());
        let physical = (u64::from(physical_hi) << 32) | u64::from(physical_lo);
        u32::try_from(physical)
            .map_err(|_| Error::with_message(Errno::EOPNOTSUPP, "64-bit ext4 extent block"))
    }

    /// Loads an extent leaf block through the inode's indirect-block cache.
    fn read_extent_leaf(&self, bid: Ext2Bid) -> Result<[u8; BLOCK_SIZE]> {
        self.indirect_blocks_manager.lock().find(bid)?.read_bytes()
    }

    /// Stores an extent leaf block and marks it for journaled write-back.
    ///
    /// With `metadata_csum`, the checksum is written into the extent tail at
    /// `12 + eh_max * 12`; it covers everything before the tail and is seeded
    /// with the inode number and generation.
    fn write_extent_leaf(&self, bid: Ext2Bid, bytes: &[u8; BLOCK_SIZE]) -> Result<()> {
        let mut block = *bytes;
        if self.metadata_csum {
            let max_entries = u16::from_le_bytes([block[4], block[5]]) as usize;
            let tail = EXTENT_HEADER_SIZE + max_entries * EXTENT_RECORD_SIZE;
            if tail + 4 > BLOCK_SIZE {
                return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent block capacity");
            }
            let seed = checksum::inode_seed(self.csum_seed, self.ino, self.generation);
            let crc = checksum::crc32c(seed, &block[..tail]);
            block[tail..tail + 4].copy_from_slice(&crc.to_le_bytes());
        }
        self.indirect_blocks_manager
            .lock()
            .find_mut(bid)?
            .write_bytes(&block)
    }

    /// Frees the data blocks covering `[start_block, end_block)`, leaving
    /// holes, and releases indirect blocks that became empty.
    ///
    /// Only the legacy indirect mapping is supported; extent trees report
    /// `EOPNOTSUPP`. The caller keeps `i_size`, so a partially covered tail
    /// block becomes a hole, matching ext4's round-up behavior.
    pub(in inode) fn punch_hole_range(
        &mut self,
        fs: &Ext2,
        start_block: Iblock,
        end_block: Iblock,
    ) -> Result<()> {
        if start_block >= end_block {
            return Ok(());
        }
        if self.extent_tree {
            return self.punch_inline_extent_range(fs, start_block, end_block);
        }

        let mut iblock = start_block;
        while iblock < end_block {
            let walk = self.walk_at(iblock)?;
            if walk.is_complete() {
                let data_bid = if walk.is_direct_data_block() {
                    let slot = walk.root_slot() as usize;
                    let bid = self.raw_block_ptrs.block_ptrs[slot];
                    self.raw_block_ptrs.block_ptrs[slot] = 0;
                    bid
                } else {
                    let leaf = walk.leaf_level();
                    let parent = walk.parent_bid_at(leaf);
                    let slot = walk.slot_at(leaf) as usize;
                    let mut manager = self.indirect_blocks_manager.lock();
                    let block = manager.find_mut(parent)?;
                    let bid = block.read_bid(slot)?;
                    block.write_bid(slot, 0)?;
                    bid
                };
                if data_bid != 0 {
                    fs.free_blocks(data_bid, 1)?;
                    self.raw_block_ptrs.sector_count = self
                        .raw_block_ptrs
                        .sector_count
                        .saturating_sub(SECTORS_PER_BLOCK);
                }
            }
            iblock += 1;
        }

        self.prune_empty_indirect_blocks(fs)
    }

    /// Frees an extent range in an extent tree.
    ///
    /// Depth-zero (in-line) trees are rewritten in place. Deeper trees are
    /// walked recursively: every leaf that overlaps the range is split, the
    /// freed physical runs are released, an emptied leaf or index block is
    /// returned to the allocator and its index entry is dropped, and an
    /// emptied root becomes an empty depth-zero header again.
    fn punch_inline_extent_range(
        &mut self,
        fs: &Ext2,
        start_block: Iblock,
        end_block: Iblock,
    ) -> Result<()> {
        let bytes = self.raw_block_ptrs.extent_bytes();
        let (_, max_entries, depth, generation) = Self::parse_extent_header(&bytes, 4)?;
        if depth == 0 {
            let (kept, freed) = Self::split_extent_block(&bytes, start_block, end_block)?;
            if kept.len() > max_entries {
                return_errno_with_message!(
                    Errno::EOPNOTSUPP,
                    "punch-hole would overflow the inline extent array"
                );
            }

            for (physical, len) in &freed {
                fs.free_blocks(*physical, *len)?;
                self.raw_block_ptrs.sector_count = self
                    .raw_block_ptrs
                    .sector_count
                    .saturating_sub(*len * SECTORS_PER_BLOCK);
            }

            let mut out = [0u8; RAW_BLOCK_PTRS_LEN * size_of::<u32>()];
            Self::encode_extent_block(&mut out, &kept, max_entries, generation)?;
            for (index, value) in self.raw_block_ptrs.block_ptrs.iter_mut().enumerate() {
                *value = u32::from_le_bytes(out[index * 4..index * 4 + 4].try_into().unwrap());
            }
            return Ok(());
        }

        let mut node = [0u8; BLOCK_SIZE];
        node[..RAW_BLOCK_PTRS_LEN * size_of::<u32>()].copy_from_slice(&bytes);
        match self.punch_extent_node(fs, &mut node, start_block, end_block)? {
            Some(_) => {
                self.collapse_extent_root(fs, &mut node)?;
                self.store_extent_root_block(&node);
            }
            None => {
                let mut empty = [0u8; BLOCK_SIZE];
                empty[0..2].copy_from_slice(&EXTENT_MAGIC.to_le_bytes());
                empty[2..4].copy_from_slice(&0u16.to_le_bytes());
                empty[4..6].copy_from_slice(&4u16.to_le_bytes());
                empty[6..8].copy_from_slice(&0u16.to_le_bytes());
                empty[8..12].copy_from_slice(&generation.to_le_bytes());
                self.store_extent_root_block(&empty);
            }
        }
        Ok(())
    }

    /// Collapses a root whose only child fits in the in-line array.
    ///
    /// Punching and truncation can leave a chain of single-child index nodes.
    /// Moving the child's records into `i_block` and freeing the child keeps
    /// the tree at the shallowest valid depth, which is what `e2fsck` asks
    /// for when it reports that a tree "could be narrower".
    fn collapse_extent_root(&mut self, fs: &Ext2, root: &mut [u8; BLOCK_SIZE]) -> Result<()> {
        let (_, _, mut depth, generation) = Self::parse_extent_header(root, 4)?;
        while depth > 0 {
            let entries = u16::from_le_bytes([root[2], root[3]]) as usize;
            if entries != 1 {
                break;
            }
            let child = Self::extent_index_leaf(root, EXTENT_HEADER_SIZE)?;
            let child_bytes = self.read_extent_leaf(child)?;
            let (child_entries, _, child_depth, _) =
                Self::parse_extent_header(&child_bytes, EXTENT_LEAF_CAPACITY)?;
            if child_depth.checked_add(1) != Some(depth) {
                return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent depth");
            }
            if child_entries == 0 || child_entries > 4 {
                break;
            }

            let records_end = EXTENT_HEADER_SIZE + child_entries * EXTENT_RECORD_SIZE;
            root[EXTENT_HEADER_SIZE..records_end]
                .copy_from_slice(&child_bytes[EXTENT_HEADER_SIZE..records_end]);
            root[2..4].copy_from_slice(&(child_entries as u16).to_le_bytes());
            root[4..6].copy_from_slice(&4u16.to_le_bytes());
            root[6..8].copy_from_slice(&child_depth.to_le_bytes());
            root[8..12].copy_from_slice(&generation.to_le_bytes());
            for byte in &mut root[records_end..RAW_BLOCK_PTRS_LEN * size_of::<u32>()] {
                *byte = 0;
            }

            let _ = self.indirect_blocks_manager.lock().remove(child);
            fs.free_blocks(child, 1)?;
            self.raw_block_ptrs.sector_count = self
                .raw_block_ptrs
                .sector_count
                .saturating_sub(SECTORS_PER_BLOCK);
            depth = child_depth;
        }
        Ok(())
    }

    /// Punches `[start_block, end_block)` inside one extent node and returns
    /// the node's new first key, or `None` when the node no longer holds any
    /// extent.
    fn punch_extent_node(
        &mut self,
        fs: &Ext2,
        bytes: &mut [u8; BLOCK_SIZE],
        start_block: Iblock,
        end_block: Iblock,
    ) -> Result<Option<u32>> {
        let (entries, max_entries, depth, generation) =
            Self::parse_extent_header(bytes, EXTENT_LEAF_CAPACITY)?;
        if depth == 0 {
            let (kept, freed) = Self::split_extent_block(bytes, start_block, end_block)?;
            for (physical, len) in &freed {
                fs.free_blocks(*physical, *len)?;
                self.raw_block_ptrs.sector_count = self
                    .raw_block_ptrs
                    .sector_count
                    .saturating_sub(*len * SECTORS_PER_BLOCK);
            }
            if kept.is_empty() {
                return Ok(None);
            }
            Self::encode_extent_block(bytes, &kept, max_entries, generation)?;
            return Ok(Some(kept[0].logical));
        }

        let mut survivors: Vec<(u32, Ext2Bid)> = Vec::with_capacity(entries);
        for index in 0..entries {
            let offset = EXTENT_HEADER_SIZE + index * EXTENT_RECORD_SIZE;
            let key = Self::extent_index_key(bytes, index)?;
            let child = Self::extent_index_leaf(bytes, offset)?;
            if key >= end_block {
                // The whole subtree starts at or beyond the punched range.
                survivors.push((key, child));
                continue;
            }
            let mut child_bytes = self.read_extent_leaf(child)?;
            let (_, _, child_depth, _) =
                Self::parse_extent_header(&child_bytes, EXTENT_LEAF_CAPACITY)?;
            if child_depth.checked_add(1) != Some(depth) {
                return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent depth");
            }
            match self.punch_extent_node(fs, &mut child_bytes, start_block, end_block)? {
                None => {
                    let _ = self.indirect_blocks_manager.lock().remove(child);
                    fs.free_blocks(child, 1)?;
                    self.raw_block_ptrs.sector_count = self
                        .raw_block_ptrs
                        .sector_count
                        .saturating_sub(SECTORS_PER_BLOCK);
                }
                Some(first_key) => {
                    self.write_extent_leaf(child, &child_bytes)?;
                    survivors.push((first_key, child));
                }
            }
        }

        if survivors.is_empty() {
            return Ok(None);
        }
        Self::encode_extent_index_block(bytes, &survivors, max_entries, depth, generation)?;
        Ok(Some(survivors[0].0))
    }

    /// Splits the depth-zero extent records in `bytes` around the punched
    /// block range, returning the records to keep and the physical runs to
    /// free.
    fn split_extent_block(
        bytes: &[u8],
        start_block: Iblock,
        end_block: Iblock,
    ) -> Result<(Vec<ExtentRecord>, Vec<(Ext2Bid, u32)>)> {
        let entries = u16::from_le_bytes([bytes[2], bytes[3]]) as usize;
        let max_entries = u16::from_le_bytes([bytes[4], bytes[5]]) as usize;
        let depth = u16::from_le_bytes([bytes[6], bytes[7]]);
        if u16::from_le_bytes([bytes[0], bytes[1]]) != EXTENT_MAGIC
            || entries > max_entries
            || max_entries > EXTENT_LEAF_CAPACITY
            || depth != 0
        {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent block");
        }

        let mut kept = Vec::new();
        let mut freed = Vec::new();
        for index in 0..entries {
            let offset = EXTENT_HEADER_SIZE + index * EXTENT_RECORD_SIZE;
            let logical = u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap());
            let raw_len = u16::from_le_bytes([bytes[offset + 4], bytes[offset + 5]]);
            let len = u32::from(raw_len & 0x7fff);
            let unwritten = raw_len & 0x8000 != 0;
            let physical = Self::extent_record_physical(bytes, offset)?;
            let extent_end = logical
                .checked_add(len)
                .ok_or_else(|| Error::with_message(Errno::EUCLEAN, "extent logical overflow"))?;

            if extent_end <= start_block || logical >= end_block {
                kept.push(ExtentRecord {
                    logical,
                    len,
                    physical,
                    unwritten,
                });
                continue;
            }

            let hole_start = logical.max(start_block);
            let hole_end = extent_end.min(end_block);
            if hole_start > logical {
                kept.push(ExtentRecord {
                    logical,
                    len: hole_start - logical,
                    physical,
                    unwritten,
                });
            }
            if hole_end < extent_end {
                kept.push(ExtentRecord {
                    logical: hole_end,
                    len: extent_end - hole_end,
                    physical: physical.checked_add(hole_end - logical).ok_or_else(|| {
                        Error::with_message(Errno::EUCLEAN, "extent physical overflow")
                    })?,
                    unwritten,
                });
            }
            freed.push((
                physical.checked_add(hole_start - logical).ok_or_else(|| {
                    Error::with_message(Errno::EUCLEAN, "extent physical overflow")
                })?,
                hole_end - hole_start,
            ));
        }
        Ok((kept, freed))
    }

    /// Serializes `kept` records as a depth-zero extent block.
    fn encode_extent_block(
        out: &mut [u8],
        kept: &[ExtentRecord],
        max_entries: usize,
        generation: u32,
    ) -> Result<()> {
        if kept.len() > max_entries {
            return_errno_with_message!(Errno::EOPNOTSUPP, "extent block overflow");
        }
        out.fill(0);
        out[0..2].copy_from_slice(&EXTENT_MAGIC.to_le_bytes());
        out[2..4].copy_from_slice(&(kept.len() as u16).to_le_bytes());
        out[4..6].copy_from_slice(&(max_entries as u16).to_le_bytes());
        out[6..8].copy_from_slice(&0u16.to_le_bytes());
        out[8..12].copy_from_slice(&generation.to_le_bytes());
        for (index, extent) in kept.iter().enumerate() {
            let offset = EXTENT_HEADER_SIZE + index * EXTENT_RECORD_SIZE;
            out[offset..offset + 4].copy_from_slice(&extent.logical.to_le_bytes());
            let raw_len = (extent.len as u16 & 0x7fff)
                | if extent.unwritten { 0x8000 } else { 0 };
            out[offset + 4..offset + 6].copy_from_slice(&raw_len.to_le_bytes());
            out[offset + 6..offset + 8].copy_from_slice(&0u16.to_le_bytes());
            out[offset + 8..offset + 12].copy_from_slice(&extent.physical.to_le_bytes());
        }
        Ok(())
    }

    /// Detaches indirect subtrees that no longer own any data block.
    fn prune_empty_indirect_blocks(&mut self, fs: &Ext2) -> Result<()> {
        for (slot, depth) in [(12usize, 1u32), (13, 2), (14, 3)] {
            let root = self.raw_block_ptrs.block_ptrs[slot];
            if root != 0 && self.prune_empty_subtree(fs, root, depth)? {
                self.raw_block_ptrs.block_ptrs[slot] = 0;
                self.free_block_subtree(fs, root, depth);
            }
        }
        Ok(())
    }

    /// Returns whether the indirect subtree at `bid` holds no data, freeing
    /// the empty children on the way. `depth` counts indirect levels,
    /// including `bid` itself.
    fn prune_empty_subtree(&mut self, fs: &Ext2, bid: Ext2Bid, depth: u32) -> Result<bool> {
        let children: SmallVec<[(usize, Ext2Bid); 8]> = {
            let mut manager = self.indirect_blocks_manager.lock();
            let block = manager.find(bid)?;
            let mut children = SmallVec::new();
            for index in 0..PTRS_PER_BLOCK {
                let child = block.read_bid(index)?;
                if child != 0 {
                    children.push((index, child));
                }
            }
            children
        };
        if depth == 1 {
            // The children are data blocks; the subtree is empty when none
            // remain (any that do are outside the punched range).
            return Ok(children.is_empty());
        }

        let mut kept = false;
        for (index, child) in children {
            if self.prune_empty_subtree(fs, child, depth - 1)? {
                {
                    let mut manager = self.indirect_blocks_manager.lock();
                    manager.find_mut(bid)?.write_bid(index, 0)?;
                }
                self.free_block_subtree(fs, child, depth - 1);
            } else {
                kept = true;
            }
        }
        Ok(!kept)
    }

    /// Truncates blocks to the new byte length (best-effort).
    ///
    /// This is a best-effort operation. Errors are logged but not propagated.
    /// Leaked blocks from partial failures are recoverable by e2fsck. Linux
    /// also follows this practice (see
    /// <https://elixir.bootlin.com/linux/v7.0/source/fs/ext2/inode.c#L1172>).
    pub(in inode) fn truncate_to_byte_len(&mut self, fs: &Ext2, new_size: usize) {
        if self.extent_tree {
            if let Err(err) = self.truncate_extent_tree(fs, new_size) {
                error!("truncate: extent-tree truncation failed, err: {:?}", err);
            }
            return;
        }

        // First logical block to free = ceil(new_size / block_size).
        let iblock = match Iblock::try_from(new_size.div_ceil(BLOCK_SIZE)) {
            Ok(ib) => ib,
            Err(_) => {
                error!("truncate: size exceeds ext2 limits, new_size={}", new_size);
                return;
            }
        };

        let walk = match self.walk_at(iblock) {
            Ok(w) => w,
            Err(err) => {
                error!("truncate: failed to compute block walk, err: {:?}", err);
                return;
            }
        };

        if walk.is_direct_data_block() {
            if let Err(err) = self.truncate_direct_slots(fs, walk.root_slot() as usize) {
                error!("truncate: truncate_direct_slots failed, err: {:?}", err);
            }
        } else if let Err(err) = self.truncate_indirect_path(fs, &walk) {
            error!("truncate: truncate_indirect_path failed, err: {:?}", err);
        }

        self.free_indirect_roots_after(fs, walk.root_slot() as usize);
    }

    /// Truncates an extent tree to the new byte length.
    ///
    /// Freeing everything from the cutoff onwards is a punch whose end is
    /// unbounded; the same recursive walk splits the straddling extent, frees
    /// emptied leaves and index blocks, and collapses an emptied root.
    fn truncate_extent_tree(&mut self, fs: &Ext2, new_size: usize) -> Result<()> {
        let cutoff = u32::try_from(new_size.div_ceil(BLOCK_SIZE))
            .map_err(|_| Error::with_message(Errno::EFBIG, "ext4 truncate size overflow"))?;
        self.punch_inline_extent_range(fs, cutoff, u32::MAX)
    }

    /// Returns a conservative hole run starting from `iblock`, capped at
    /// `max_blocks`.
    ///
    /// The caller must have already verified that `iblock` is unallocated.
    /// Returns `0` if the block is actually allocated. The result may stop
    /// early at a direct/indirect region boundary, but it never overestimates:
    /// every block in the returned interval is known to be unmapped.
    pub(in inode) fn approx_hole_blocks(&self, iblock: Iblock, max_blocks: u32) -> Result<u32> {
        if max_blocks == 0 {
            return Ok(0);
        }
        if self.extent_tree {
            if !self.lookup_extent_range(iblock, 1)?.is_empty() {
                return Ok(0);
            }
            let root = self.raw_block_ptrs.extent_bytes();
            let (entries, _, depth, _) = Self::parse_extent_header(&root, 4)?;
            let mut node = [0u8; BLOCK_SIZE];
            node[..RAW_BLOCK_PTRS_LEN * size_of::<u32>()].copy_from_slice(&root);
            let mut node_entries = entries;
            let mut node_depth = depth;
            let mut upper = None;
            if depth == 0 {
                for index in 0..entries {
                    let logical = Self::extent_index_key(&node, index)?;
                    if logical > iblock {
                        upper = Some(logical);
                        break;
                    }
                }
            } else {
                loop {
                    let (selected, next_key) =
                        Self::select_extent_index(&node, node_entries, iblock, true)?
                            .ok_or_else(|| {
                                Error::with_message(Errno::EUCLEAN, "empty ext4 extent index")
                            })?;
                    if let Some(key) = next_key {
                        upper = Some(upper.map_or(key, |bound: u32| bound.min(key)));
                    }
                    let child = Self::extent_index_leaf(
                        &node,
                        EXTENT_HEADER_SIZE + selected * EXTENT_RECORD_SIZE,
                    )?;
                    let child_bytes = self.read_extent_leaf(child)?;
                    let (child_entries, _, child_depth, _) =
                        Self::parse_extent_header(&child_bytes, EXTENT_LEAF_CAPACITY)?;
                    if child_depth.checked_add(1) != Some(node_depth) {
                        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent depth");
                    }
                    if child_depth == 0 {
                        for index in 0..child_entries {
                            let logical = Self::extent_index_key(&child_bytes, index)?;
                            if logical > iblock {
                                upper =
                                    Some(upper.map_or(logical, |bound: u32| bound.min(logical)));
                                break;
                            }
                        }
                        break;
                    }
                    node = child_bytes;
                    node_entries = child_entries;
                    node_depth = child_depth;
                }
            }
            let hole = upper
                .map_or(max_blocks, |bound| bound.saturating_sub(iblock).min(max_blocks))
                .max(1);
            return Ok(hole);
        }
        let walk = self.walk_at(iblock)?;

        if walk.is_complete() {
            return Ok(0);
        }

        let leaf_level = walk.leaf_level();
        let hole_level = walk.hole_level();
        let subtree_indirect_levels = leaf_level - hole_level;

        let mut hole_len = 0u32;
        let ptrs = PTRS_PER_BLOCK as u32;

        // `hole_level` is where the first zero pointer was found. A leaf-level
        // hole needs a short slot scan; a higher-level zero pointer roots a
        // fully sparse subtree that can be skipped without probing each block.
        //
        // The computed length is intentionally conservative: it may stop before
        // the full hole ends, but it never crosses a possibly allocated pointer.
        if subtree_indirect_levels == 0 {
            let slot = walk.slot_at(hole_level);
            if hole_level == 0 {
                let scan_len = max_blocks.min(12 - slot);
                let scan_end = slot + scan_len;
                for &bid in &self.raw_block_ptrs.block_ptrs[slot as usize..scan_end as usize] {
                    if bid != 0 {
                        break;
                    }
                    hole_len += 1;
                }
            } else {
                let parent_bid = walk.parent_bid_at(hole_level);
                let mut indirect_block_manager = self.indirect_blocks_manager.lock();
                let indirect_block = indirect_block_manager.find(parent_bid)?;
                let scan_len = max_blocks.min(ptrs - slot);
                let scan_end = slot + scan_len;
                for idx in slot..scan_end {
                    let bid = indirect_block.read_bid(idx as usize)?;
                    if bid != 0 {
                        break;
                    }
                    hole_len += 1;
                }
            }
        } else {
            let subtree = ptrs.pow(subtree_indirect_levels as u32);
            let mut slot_offset = 0u32;
            for level in (hole_level + 1)..=leaf_level {
                slot_offset = slot_offset * ptrs + walk.slot_at(level);
            }
            hole_len = subtree - slot_offset;
        }

        Ok(hole_len.min(max_blocks))
    }

    fn truncate_direct_slots(&mut self, fs: &Ext2, start_idx: usize) -> Result<()> {
        let start = start_idx.min(12);
        for idx in start..12 {
            let bid = self.raw_block_ptrs.block_ptrs[idx];
            if bid == 0 {
                continue;
            }
            fs.free_blocks(bid, 1)?;
            self.raw_block_ptrs.block_ptrs[idx] = 0;
            self.raw_block_ptrs.sector_count = self
                .raw_block_ptrs
                .sector_count
                .saturating_sub(SECTORS_PER_BLOCK);
        }
        Ok(())
    }

    fn truncate_indirect_path(&mut self, fs: &Ext2, walk: &BlockPointerWalk) -> Result<()> {
        // If the truncation point is exactly at the start of an indirect
        // block (innermost slot = 0), trim trailing zero slots and re-walk the
        // shorter path. This raises the shared level so the whole block can be
        // detached at the parent without reading the lower subtree.
        let trimmed = self.walk_path(walk.trim_trailing_zero_slots())?;

        // `detach_level` is the highest level at which we detach and free a whole
        // subtree. Slots to the right of the cut below `detach_level` are cleared
        // by `free_indirect_right_side`.
        let detach_level = self.find_detach_level(walk, &trimmed)?;

        if let Some(detached_bid) = self.detach_subtree_root(walk, &trimmed, detach_level)? {
            self.free_block_subtree(fs, detached_bid, walk.subtree_indirect_levels(detach_level));
        }

        self.free_indirect_right_side(fs, walk, &trimmed, detach_level)?;

        Ok(())
    }

    fn find_detach_level(
        &self,
        walk: &BlockPointerWalk,
        trimmed: &BlockPointerWalk,
    ) -> Result<usize> {
        // Start from the deepest level if the walk was complete, or from the
        // level where the walk stopped on a zero pointer.
        let mut detach_level = if trimmed.is_complete() {
            trimmed.leaf_level()
        } else {
            trimmed.hole_level()
        };

        // Walk upward: if all entries to the left of the truncation slot in
        // an indirect block are zero, the entire block can be detached at the
        // parent level. Stop as soon as a non-zero kept entry is found.
        while detach_level > 0 {
            let current_bid = trimmed.parent_bid_at(detach_level);

            let mut indirect_blocks_manager = self.indirect_blocks_manager.lock();
            let block = indirect_blocks_manager.find(current_bid)?;
            let keep_entries = walk.slot_at(detach_level) as usize;
            let mut all_zero = true;
            for idx in 0..keep_entries {
                if block.read_bid(idx)? != 0 {
                    all_zero = false;
                    break;
                }
            }
            if !all_zero {
                break;
            }

            // Left side is all zeros; move up one level.
            detach_level -= 1;
        }

        Ok(detach_level)
    }

    fn detach_subtree_root(
        &mut self,
        walk: &BlockPointerWalk,
        trimmed: &BlockPointerWalk,
        detach_level: usize,
    ) -> Result<Option<Ext2Bid>> {
        let detached_bid = if detach_level == 0 {
            // Subtree root is referenced directly from `inode.block_ptrs[]`.
            let slot = walk.root_slot() as usize;
            let bid = self.raw_block_ptrs.block_ptrs[slot];
            self.raw_block_ptrs.block_ptrs[slot] = 0;
            bid
        } else {
            // Subtree root is a slot inside a parent indirect block.
            let parent_bid = trimmed.parent_bid_at(detach_level);
            let slot = walk.slot_at(detach_level) as usize;
            let mut indirect_blocks_manager = self.indirect_blocks_manager.lock();
            let parent_block = indirect_blocks_manager.find_mut(parent_bid)?;
            let bid = parent_block.read_bid(slot)?;
            parent_block.write_bid(slot, 0)?;
            bid
        };

        if detached_bid != 0 {
            Ok(Some(detached_bid))
        } else {
            Ok(None)
        }
    }

    fn free_indirect_right_side(
        &mut self,
        fs: &Ext2,
        walk: &BlockPointerWalk,
        trimmed: &BlockPointerWalk,
        detach_level: usize,
    ) -> Result<()> {
        let ptrs_per_block = PTRS_PER_BLOCK;
        // From `detach_level` down to level 1, clear all slots to the right of the
        // truncation slot and free their subtrees.
        for level in (1..=detach_level).rev() {
            let current_bid = trimmed.parent_bid_at(level);

            let start_idx = (walk.slot_at(level) as usize) + 1;
            let child_indirect_levels = walk.subtree_indirect_levels(level);
            // Collect block numbers before releasing the lock; `free_block_subtree`
            // may re-acquire the indirect block manager recursively.
            let child_blocks = {
                let mut indirect_blocks_manager = self.indirect_blocks_manager.lock();
                let block = indirect_blocks_manager.find_mut(current_bid)?;
                let mut child_blocks = Vec::new();
                for idx in start_idx..ptrs_per_block {
                    let bid = block.read_bid(idx)?;
                    if bid == 0 {
                        continue;
                    }
                    block.write_bid(idx, 0)?;
                    child_blocks.push(bid);
                }
                child_blocks
            };
            for bid in child_blocks {
                self.free_block_subtree(fs, bid, child_indirect_levels);
            }
        }
        Ok(())
    }

    fn free_indirect_roots_after(&mut self, fs: &Ext2, root_slot: usize) {
        // The conditions are cumulative: truncating before the single-indirect
        // root frees slots 12, 13, and 14; truncating at or before the
        // single-indirect root frees slots 13 and 14; truncating at or before
        // the double-indirect root frees slot 14.
        if root_slot < 12 {
            let bid = self.raw_block_ptrs.block_ptrs[12];
            if bid != 0 {
                self.raw_block_ptrs.block_ptrs[12] = 0;
                self.free_block_subtree(fs, bid, 1);
            }
        }
        if root_slot <= 12 {
            let bid = self.raw_block_ptrs.block_ptrs[13];
            if bid != 0 {
                self.raw_block_ptrs.block_ptrs[13] = 0;
                self.free_block_subtree(fs, bid, 2);
            }
        }
        if root_slot <= 13 {
            let bid = self.raw_block_ptrs.block_ptrs[14];
            if bid != 0 {
                self.raw_block_ptrs.block_ptrs[14] = 0;
                self.free_block_subtree(fs, bid, 3);
            }
        }
    }

    /// Translates a logical block number into a block-pointer path and walks
    /// the on-disk chain to resolve physical block addresses.
    ///
    /// After skipping the 12 direct blocks, the remaining block number is
    /// converted to base-1024 digits — each digit becomes a slot at one
    /// level of the indirect block tree.
    fn walk_at(&self, iblock: Iblock) -> Result<BlockPointerWalk> {
        let path = BlockPointerPath::new(iblock)?;
        self.walk_path(path)
    }

    /// Computes a `BlockPointerWalk` by walking the on-disk pointer chain.
    fn walk_path(&self, path: BlockPointerPath) -> Result<BlockPointerWalk> {
        let top_bid = self.raw_block_ptrs.block_ptrs[path.slots[0] as usize];
        let num_levels = path.num_levels;
        let mut visited_entries = SmallVec::new();
        if top_bid == 0 {
            // Zero pointer means the path is broken at level 0.
            return Ok(BlockPointerWalk {
                path,
                is_complete: false,
                visited_entries,
            });
        }
        visited_entries.push(top_bid);

        let mut indirect_blocks_manager = self.indirect_blocks_manager.lock();
        let mut parent_bid = top_bid;
        for level in 1..num_levels {
            let next_bid = indirect_blocks_manager
                .find(parent_bid)?
                .read_bid(path.slots[level] as usize)?;
            if next_bid == 0 {
                return Ok(BlockPointerWalk {
                    path,
                    is_complete: false,
                    visited_entries,
                });
            }
            visited_entries.push(next_bid);
            parent_bid = next_bid;
        }

        Ok(BlockPointerWalk {
            path,
            is_complete: true,
            visited_entries,
        })
    }

    fn existing_contiguous_data_blocks(
        &self,
        walk: &BlockPointerWalk,
        max_blocks: u32,
    ) -> Result<Range<Ext2Bid>> {
        if max_blocks == 0 {
            return_errno_with_message!(Errno::EINVAL, "zero block range requested");
        }
        // A partial walk means the logical block lands in a hole, so there is
        // no existing physical range to report.
        if !walk.is_complete() {
            return Ok(0..0);
        }
        // The last chain entry is the first existing data block for `iblock`.
        let first_bid = walk.bid_at(walk.leaf_level());
        if first_bid == 0 {
            return Ok(0..0);
        }

        let max_count = walk.max_blocks_in_leaf(max_blocks);
        let mut count = 1u32;
        let start_slot = walk.leaf_slot() as usize;

        if walk.is_direct_data_block() {
            // Direct blocks are stored inline in the inode, so scan forward in
            // `i_block[]` while the next physical block stays contiguous.
            while count < max_count {
                let slot = start_slot + count as usize;
                let Some(&next_bid) = self.raw_block_ptrs.block_ptrs.get(slot) else {
                    break;
                };
                if next_bid == 0 || next_bid != first_bid.saturating_add(count) {
                    break;
                }
                count += 1;
            }
        } else {
            // Indirect cases share the same contiguity rule, but the leaf slots
            // live in the final indirect block reached by the walk.
            let leaf_bid = walk.parent_bid_at(walk.leaf_level());
            let mut indirect_block_manager = self.indirect_blocks_manager.lock();
            let leaf_block = indirect_block_manager.find(leaf_bid)?;
            while count < max_count {
                let slot = start_slot + count as usize;
                let next_bid = leaf_block.read_bid(slot)?;
                if next_bid == 0 || next_bid != first_bid.saturating_add(count) {
                    break;
                }
                count += 1;
            }
        }

        Ok(first_bid..first_bid.saturating_add(count))
    }

    /// Returns how many indirect metadata blocks and data blocks are needed.
    ///
    /// The returned tuple is `(indirect_blks, data_blks)`. `indirect_blks`
    /// covers the missing metadata levels below the first hole in `walk`,
    /// while `data_blks` is the contiguous data range that can be placed in the
    /// current leaf without crossing its block-pointer boundary.
    fn planned_allocation(&self, walk: &BlockPointerWalk, max_blocks: u32) -> Result<(u32, u32)> {
        if max_blocks == 0 {
            return_errno_with_message!(Errno::EINVAL, "zero block allocation requested");
        }

        // Missing child levels correspond to indirect metadata blocks that
        // must be allocated before any data block can be linked into the tree.
        // Never allocate past the current leaf boundary even if the caller asks
        // for a larger contiguous range.
        let indirect_blks = walk.subtree_indirect_levels(walk.hole_level());
        let max_data_blks = walk.max_blocks_in_leaf(max_blocks);
        if indirect_blks > 0 {
            // If the path is incomplete, the fresh leaf starts empty, so we
            // can request the whole bounded data range immediately.
            return Ok((indirect_blks, max_data_blks));
        }

        // The indirect path already exists, so only count how many consecutive
        // empty slots remain in the current leaf.
        let start_slot = walk.leaf_slot() as usize;
        let mut count = 1u32;
        if walk.is_direct_data_block() {
            // Direct blocks live inline in the inode's `i_block[]`.
            while count < max_data_blks {
                let slot = start_slot + count as usize;
                let Some(&next_bid) = self.raw_block_ptrs.block_ptrs.get(slot) else {
                    break;
                };
                if next_bid != 0 {
                    break;
                }
                count += 1;
            }
        } else {
            // Indirect cases use the final indirect block as the allocation leaf.
            let leaf_bid = walk.parent_bid_at(walk.leaf_level());
            let mut indirect_blocks_manager = self.indirect_blocks_manager.lock();
            let leaf_block = indirect_blocks_manager.find(leaf_bid)?;
            while count < max_data_blks {
                let slot = start_slot + count as usize;
                if leaf_block.read_bid(slot)? != 0 {
                    break;
                }
                count += 1;
            }
        }

        Ok((0, count))
    }

    /// Frees all blocks in a pointer subtree.
    fn free_block_subtree(&mut self, fs: &Ext2, block_bid: Ext2Bid, indirect_levels: u32) {
        if block_bid == 0 {
            return;
        }

        if indirect_levels == 0 {
            if let Err(err) = fs.free_blocks(block_bid, 1) {
                // Best-effort free path logs errors and proceeds.
                error!(
                    "free_block_subtree: failed to free data block {}: {:?}",
                    block_bid, err
                );
                return;
            }
            self.raw_block_ptrs.sector_count = self
                .raw_block_ptrs
                .sector_count
                .saturating_sub(SECTORS_PER_BLOCK);
            return;
        }

        let child_blocks = {
            let mut indirect_blocks_manager = self.indirect_blocks_manager.lock();
            match indirect_blocks_manager.read_child_bids(block_bid) {
                Ok(children) => {
                    indirect_blocks_manager.remove(block_bid);
                    children
                }
                Err(_) => {
                    // Skip the damaged subtree after logging the read failure so
                    // cleanup can continue for the remaining subtree.
                    error!(
                        "free_block_subtree: failed to read indirect block {} (indirect_levels {})",
                        block_bid, indirect_levels
                    );
                    return;
                }
            }
        };

        for bid in child_blocks {
            self.free_block_subtree(fs, bid, indirect_levels - 1);
        }

        if let Err(err) = fs.free_blocks(block_bid, 1) {
            error!(
                "free_block_subtree: failed to free indirect block {}: {:?}",
                block_bid, err
            );
            return;
        }
        self.raw_block_ptrs.sector_count = self
            .raw_block_ptrs
            .sector_count
            .saturating_sub(SECTORS_PER_BLOCK);
    }

    /// Allocates indirect metadata blocks and data blocks for a missing path.
    ///
    /// Indirect blocks are allocated first so the eventual data range can be
    /// placed near the newly allocated metadata when possible. The returned
    /// guard frees all allocated blocks on drop unless `commit` is called after
    /// the blocks have been spliced into the tree.
    fn allocate_blocks<'a>(
        &self,
        fs: &'a Ext2,
        indirect_blks: u32,
        data_blks: u32,
        walk: &BlockPointerWalk,
    ) -> Result<BlockAllocGuard<'a>> {
        let mut alloc_goal = walk
            .visited_entries
            .last()
            .copied()
            .unwrap_or(self.raw_block_ptrs.block_ptrs[0]);

        // Allocate the missing indirect metadata first, then place data blocks
        // immediately after the metadata run when possible.
        let mut guard = BlockAllocGuard::new(fs, indirect_blks);

        let mut remaining_indirect_blks = indirect_blks;
        while remaining_indirect_blks > 0 {
            let allocated = fs.alloc_blocks(remaining_indirect_blks, alloc_goal)?;
            debug_assert!(allocated.end >= allocated.start);
            let allocated_count = allocated.end - allocated.start;
            debug_assert!(allocated_count > 0 && allocated_count <= remaining_indirect_blks);

            alloc_goal = allocated.end;
            remaining_indirect_blks -= allocated_count;
            guard.extend_indirect_blocks(allocated);
        }

        let data_blocks_range = fs.alloc_blocks(data_blks, alloc_goal)?;
        debug_assert!(data_blocks_range.end >= data_blocks_range.start);
        let allocated_count = data_blocks_range.end - data_blocks_range.start;
        guard.track_data_blocks(data_blocks_range.clone());
        debug_assert!(allocated_count > 0 && allocated_count <= data_blks);

        // We must wait until the blocks are initialized before we can proceed.
        // Otherwise, concurrent page faults may see uninitialized blocks.
        //
        // TODO: In the write path, if the entire block is going to be
        // overwritten, we should write the real payload instead of zeroing it.
        Self::zero_new_blocks(fs, &data_blocks_range)?;

        Ok(guard)
    }

    /// Zeroes newly allocated data blocks before exposing them via mapped reads.
    fn zero_new_blocks(fs: &Ext2, block_range: &Range<Ext2Bid>) -> Result<()> {
        let mut io_batch = IoBatch::with_capacity(1);

        let bio_segment = BioSegment::alloc(block_range.len(), BioDirection::ToDevice);
        let mut segment_writer = bio_segment.writer().unwrap();
        segment_writer.fill_zeros(block_range.len() * BLOCK_SIZE);
        fs.write_blocks_async(block_range.start, bio_segment, None, &mut io_batch)?;

        io_batch.wait_all()?;
        Ok(())
    }

    /// Splices allocated blocks into the block-pointer tree.
    ///
    /// If no indirect metadata blocks were allocated, this only fills data bids
    /// into existing leaf slots. Otherwise it builds the missing indirect chain,
    /// links the chain root into the first hole, and updates the inode sector
    /// count for both metadata and data blocks.
    fn link_allocated_blocks(
        &mut self,
        guard: &BlockAllocGuard,
        walk: &BlockPointerWalk,
    ) -> Result<()> {
        let indirect_blocks = &guard.indirect_blocks;
        let data_blocks = &guard.data_blocks;

        let total_blks = indirect_blocks.len() as u32 + data_blocks.len() as u32;
        let added_sectors = total_blks * SECTORS_PER_BLOCK;
        let new_block_count = self
            .raw_block_ptrs
            .sector_count
            .checked_add(added_sectors)
            .ok_or_else(|| Error::with_message(Errno::EIO, "inode block count overflow"))?;

        if indirect_blocks.is_empty() {
            // The path already exists; only fill data pointers into existing slots.
            self.fill_existing_leaf_slots(walk, data_blocks)?;
        } else {
            self.link_new_indirect_chain(walk, indirect_blocks, data_blocks)?;
        }

        self.raw_block_ptrs.sector_count = new_block_count;
        Ok(())
    }

    /// Fills data pointers into existing slots when no new indirect blocks are needed.
    fn fill_existing_leaf_slots(
        &mut self,
        walk: &BlockPointerWalk,
        data_blocks: &Range<Ext2Bid>,
    ) -> Result<()> {
        let hole_level = walk.hole_level();

        if hole_level == 0 {
            let slot = walk.root_slot() as usize;
            self.write_data_range_to_direct_slots(slot, data_blocks)?;
        } else {
            let parent_bid = walk.parent_bid_at(hole_level);
            let slot = walk.slot_at(hole_level) as usize;
            let mut indirect_blocks_manager = self.indirect_blocks_manager.lock();
            let parent_block = indirect_blocks_manager.find_mut(parent_bid)?;
            Self::write_data_range_to_indirect_block(parent_block, slot, data_blocks)?;
        }
        Ok(())
    }

    /// Builds a new indirect-block chain and links it into the tree.
    fn link_new_indirect_chain(
        &mut self,
        walk: &BlockPointerWalk,
        indirect_blocks: &[Ext2Bid],
        data_blocks: &Range<Ext2Bid>,
    ) -> Result<()> {
        let hole_level = walk.hole_level();
        let chain_root_bid = indirect_blocks[0];
        let mut indirect_blocks_manager = self.indirect_blocks_manager.lock();

        let result = (|| -> Result<()> {
            // Build the indirect blocks chain: each block points to the next;
            // the leaf block points to the allocated data range.
            debug_assert_eq!(
                indirect_blocks.len() as u32,
                walk.subtree_indirect_levels(hole_level)
            );
            let mut child_levels = walk.child_levels(hole_level);
            for (indirect_blk_idx, new_bid) in indirect_blocks.iter().copied().enumerate() {
                let level = child_levels
                    .next()
                    .expect("one child level per allocated indirect block");

                let mut block = IndirectBlock::alloc_new(new_bid)?;
                let start_slot = walk.slot_at(level) as usize;
                if indirect_blk_idx + 1 == indirect_blocks.len() {
                    Self::write_data_range_to_indirect_block(&mut block, start_slot, data_blocks)?;
                } else {
                    block.write_bid(start_slot, indirect_blocks[indirect_blk_idx + 1])?;
                }
                indirect_blocks_manager.insert(block)?;
            }

            // Splice the chain root into the parent pointer.
            if hole_level == 0 {
                let slot = walk.root_slot() as usize;
                if self.raw_block_ptrs.block_ptrs[slot] != 0 {
                    return_errno_with_message!(
                        Errno::EIO,
                        "block pointer changed during allocation"
                    );
                }
                self.raw_block_ptrs.block_ptrs[slot] = chain_root_bid;
            } else {
                let parent_bid = walk.parent_bid_at(hole_level);
                let parent_block = indirect_blocks_manager.find_mut(parent_bid)?;
                let slot = walk.slot_at(hole_level) as usize;
                if parent_block.read_bid(slot)? != 0 {
                    return_errno_with_message!(
                        Errno::EIO,
                        "block pointer changed during allocation"
                    );
                }
                parent_block.write_bid(slot, chain_root_bid)?;
            }

            Ok(())
        })();

        if let Err(err) = result {
            // Evict any indirect blocks already inserted into the cache.
            for &bid in indirect_blocks.iter() {
                indirect_blocks_manager.remove(bid);
            }
            return Err(err);
        }

        Ok(())
    }

    /// Writes a contiguous range of allocated data block IDs into direct
    /// `i_block[]` slots.
    ///
    /// Callers guarantee that the target slots are zero because `walk_path`
    /// confirmed the hole and the inode lock is held throughout.
    fn write_data_range_to_direct_slots(
        &mut self,
        start_slot: usize,
        data_range: &Range<Ext2Bid>,
    ) -> Result<()> {
        let end_slot = start_slot + data_range.len();
        let slots = &mut self.raw_block_ptrs.block_ptrs[start_slot..end_slot];

        for (entry, bid) in slots.iter_mut().zip(data_range.clone()) {
            debug_assert_eq!(*entry, 0);
            *entry = bid;
        }
        Ok(())
    }

    /// Writes a contiguous range of allocated data block IDs into an indirect
    /// block.
    ///
    /// The caller guarantees that every target slot is zero; the debug
    /// assertion keeps that invariant explicit during development.
    fn write_data_range_to_indirect_block(
        block: &mut IndirectBlock,
        start_slot: usize,
        data_range: &Range<Ext2Bid>,
    ) -> Result<()> {
        let end_slot = start_slot + data_range.len();

        for (slot, bid) in (start_slot..end_slot).zip(data_range.clone()) {
            debug_assert_eq!(block.read_bid(slot).unwrap_or(0), 0);
            block.write_bid(slot, bid)?;
        }
        Ok(())
    }
}

/// On-disk block pointer state mirrored from `RawInode`.
///
/// The mirrored fields are `i_blocks` (sector count) and the 15-entry
/// `i_block[]` array.
///
/// This copy exists to break a locking cycle between `Inode` and
/// `PageCacheBackend`. Both need to look up or allocate blocks, but `Inode`
/// also drives `PageCacheBackend` operations. Giving both sides access to this
/// struct instead of routing everything through the `InodeInner` lock avoids a
/// deadlock. The struct is the authoritative in-memory state and is written
/// back to the inode on sync.
#[derive(Clone, Copy, Debug)]
pub(in inode) struct RawBlockPtrs {
    pub sector_count: u32,
    pub block_ptrs: [u32; RAW_BLOCK_PTRS_LEN],
}

impl RawBlockPtrs {
    /// Creates a `RawBlockPtrs` from the given sector count and pointer array.
    pub(in inode) fn new(sector_count: u32, block_ptrs: [u32; RAW_BLOCK_PTRS_LEN]) -> Self {
        Self {
            sector_count,
            block_ptrs,
        }
    }

    fn extent_bytes(&self) -> [u8; RAW_BLOCK_PTRS_LEN * size_of::<u32>()] {
        let mut bytes = [0; RAW_BLOCK_PTRS_LEN * size_of::<u32>()];
        for (index, value) in self.block_ptrs.iter().enumerate() {
            bytes[index * 4..index * 4 + 4].copy_from_slice(&value.to_le_bytes());
        }
        bytes
    }

    fn is_extent_tree(&self) -> bool {
        let bytes = self.extent_bytes();
        u16::from_le_bytes([bytes[0], bytes[1]]) == EXTENT_MAGIC
    }

    /// Reads the ext2 special-file device encoding stored in `i_block`.
    pub(in inode) fn read_device_id(&self) -> u64 {
        let (major, minor) = if self.block_ptrs[0] != 0 {
            let old_encoded_device = self.block_ptrs[0];
            // Old_decode_dev: (major << 8) | minor with 8-bit major/minor.
            (
                ((old_encoded_device >> 8) & 0xFF),
                (old_encoded_device & 0xFF),
            )
        } else {
            let encoded_device = self.block_ptrs[1];
            // Decode the extended major/minor bit layout.
            (
                ((encoded_device & 0xFFF00) >> 8),
                ((encoded_device & 0xFF) | ((encoded_device >> 12) & 0xFFF00)),
            )
        };

        encode_device_numbers(major, minor)
    }

    /// Writes a device ID into the ext2 special-file `i_block` layout.
    pub(in inode) fn write_device_id(&mut self, device_id: u64) {
        let (major, minor) = decode_device_numbers(device_id);

        // Old_valid_dev: MAJOR/MINOR must both fit in 8 bits.
        if major < 256 && minor < 256 {
            self.block_ptrs[0] = (major << 8) | minor;
            self.block_ptrs[1] = 0;
        } else {
            self.block_ptrs[0] = 0;
            self.block_ptrs[1] = (minor & 0xFF) | (major << 8) | ((minor & !0xFF) << 12);
            self.block_ptrs[2] = 0;
        }
    }
}

/// Result of resolving a physical block range from a logical block position.
#[derive(Debug)]
pub(in inode) enum ResolvedBlockRange {
    /// The physical range already exists on device.
    Existing(Range<Ext2Bid>),
    /// The physical range was freshly allocated on device.
    NewlyAllocated(Range<Ext2Bid>),
}

/// Full block-pointer path from `i_block[]` to a logical data block.
///
/// `slots[0]` is always the slot in the inode's `i_block[]` array.
/// `slots[1..num_levels]` are the successive slots inside indirect
/// blocks, from outermost to innermost.
#[derive(Clone, Copy, Debug)]
struct BlockPointerPath {
    slots: [u32; MAX_BLOCK_POINTER_LEVELS],
    num_levels: usize,
}

impl BlockPointerPath {
    /// Creates a block-pointer path for a logical block index.
    fn new(iblock: Iblock) -> Result<Self> {
        let ptrs = PTRS_PER_BLOCK as u32;
        let ptr_shift = ptrs.trailing_zeros();
        let ptr_mask = ptrs - 1;
        let double_blocks = 1u32 << (ptr_shift * 2);

        if iblock < 12 {
            return Ok(Self::from_slots([iblock, 0, 0, 0], 1));
        }
        let block = iblock - 12;

        if block < ptrs {
            return Ok(Self::from_slots([12, block, 0, 0], 2));
        }
        let block = block - ptrs;

        if block < double_blocks {
            return Ok(Self::from_slots(
                [13, block >> ptr_shift, block & ptr_mask, 0],
                3,
            ));
        }
        let block = block - double_blocks;

        if (block >> (ptr_shift * 2)) < ptrs {
            return Ok(Self::from_slots(
                [
                    14,
                    block >> (ptr_shift * 2),
                    (block >> ptr_shift) & ptr_mask,
                    block & ptr_mask,
                ],
                4,
            ));
        }

        return_errno_with_message!(Errno::EINVAL, "block number exceeds ext2 pointer tree");
    }

    fn from_slots(slots: [u32; MAX_BLOCK_POINTER_LEVELS], num_levels: usize) -> Self {
        debug_assert!((1..=MAX_BLOCK_POINTER_LEVELS).contains(&num_levels));
        Self { slots, num_levels }
    }
}

/// Traversal state from an inode's `i_block[]` array to a logical data block.
///
/// The state combines the slot path selected by the logical block index with
/// the block IDs observed in the on-disk pointer chain.
///
/// For a 4 KiB block size (1024 pointers per indirect block):
///
/// ```text
///   Logical block 0..11 (direct):
///     levels=1:  i_block[0..12]  →  data block
///
///   Logical block 12..1035 (single indirect):
///     levels=2:  i_block[12]  →  indirect[slot]  →  data block
///
///   Logical block 1036..1049611 (double indirect):
///     levels=3:  i_block[13]  →  L1[a]  →  L2[b]  →  data block
///
///   Logical block 1049612..1074791435 (triple indirect):
///     levels=4:  i_block[14]  →  L1[a]  →  L2[b]  →  L3[c]  →  data block
/// ```
///
/// `level` is a 0-based index into this chain: level 0 is always the inode
/// `i_block[]` slot, and levels 1.. are successive indirect-block slots.
#[derive(Debug)]
struct BlockPointerWalk {
    /// Slot path from inode `i_block[]` to the target logical block.
    ///
    /// This is pure addressing metadata; physical block numbers read while
    /// following the path are recorded in `visited_entries`.
    path: BlockPointerPath,
    /// Whether every pointer in the path was non-zero.
    is_complete: bool,
    /// Non-zero entries visited so far, one per completed level.
    ///
    /// If the walk is incomplete, the first hole is at
    /// `visited_entries.len()`.
    visited_entries: SmallVec<[Ext2Bid; MAX_BLOCK_POINTER_LEVELS]>,
}

impl BlockPointerWalk {
    /// Returns the root slot in inode `i_block[]`.
    fn root_slot(&self) -> u32 {
        self.path.slots[0]
    }

    /// Returns whether this walk names an inline direct data-block slot.
    fn is_direct_data_block(&self) -> bool {
        self.root_slot() < 12
    }

    /// Returns the total number of levels in the pointer chain.
    #[cfg(ktest)]
    fn num_levels(&self) -> usize {
        self.path.num_levels
    }

    /// Returns the slot at the given level of the block pointer tree.
    fn slot_at(&self, level: usize) -> u32 {
        debug_assert!(level < self.path.num_levels);
        self.path.slots[level]
    }

    /// Returns the slot at the deepest (leaf) level.
    fn leaf_slot(&self) -> u32 {
        self.slot_at(self.leaf_level())
    }

    /// Returns the index (0-based) of the deepest (leaf) level in the pointer chain.
    fn leaf_level(&self) -> usize {
        self.path.num_levels - 1
    }

    /// Trims trailing zero slots from the innermost levels.
    ///
    /// Used by `truncate_to_byte_len` to raise the shared path level when the
    /// truncation point sits at the start of an indirect block.
    fn trim_trailing_zero_slots(&self) -> BlockPointerPath {
        let mut num_levels = self.path.num_levels;
        while num_levels > 1 && self.path.slots[num_levels - 1] == 0 {
            num_levels -= 1;
        }

        BlockPointerPath::from_slots(self.path.slots, num_levels)
    }

    /// Returns the indirect-block levels in the subtree rooted at `root_level`.
    ///
    /// Equals `leaf_level() - root_level`: the number of indirect blocks that
    /// must still be traversed from the node at `root_level` down to data
    /// blocks.
    /// Pass this directly as the `indirect_levels` argument to `free_block_subtree`.
    fn subtree_indirect_levels(&self, root_level: usize) -> u32 {
        debug_assert!(root_level <= self.leaf_level());
        (self.leaf_level() - root_level) as u32
    }

    /// Returns the child levels below `level`, from outermost to innermost.
    fn child_levels(&self, level: usize) -> impl Iterator<Item = usize> {
        debug_assert!(level <= self.leaf_level());
        (level + 1)..=self.leaf_level()
    }

    /// Returns the maximum block count before crossing this walk's leaf boundary.
    fn max_blocks_in_leaf(&self, max_blocks: u32) -> u32 {
        let boundary = if self.is_direct_data_block() {
            11 - self.leaf_slot()
        } else {
            PTRS_PER_BLOCK as u32 - 1 - self.leaf_slot()
        };
        max_blocks.min(boundary + 1)
    }

    fn is_complete(&self) -> bool {
        self.is_complete
    }

    fn bid_at(&self, level: usize) -> Ext2Bid {
        debug_assert!(level < self.visited_entries.len());
        self.visited_entries[level]
    }

    fn parent_bid_at(&self, level: usize) -> Ext2Bid {
        debug_assert!(level > 0);
        self.bid_at(level - 1)
    }

    fn hole_level(&self) -> usize {
        debug_assert!(!self.is_complete());
        self.visited_entries.len()
    }
}

/// Rollback guard for metadata and data blocks allocated through the block-pointer tree.
#[derive(Debug)]
struct BlockAllocGuard<'a> {
    fs: &'a Ext2,
    indirect_blocks: Vec<Ext2Bid>,
    data_blocks: Range<Ext2Bid>,
    committed: bool,
}

impl<'a> BlockAllocGuard<'a> {
    fn new(fs: &'a Ext2, indirect_blocks: u32) -> Self {
        Self {
            fs,
            indirect_blocks: Vec::with_capacity(indirect_blocks as usize),
            data_blocks: Range { start: 0, end: 0 },
            committed: false,
        }
    }

    fn extend_indirect_blocks(&mut self, indirect_blocks: Range<Ext2Bid>) {
        self.indirect_blocks.extend(indirect_blocks);
    }

    fn track_data_blocks(&mut self, data_blocks: Range<Ext2Bid>) {
        debug_assert!(self.data_blocks.is_empty());
        self.data_blocks = data_blocks;
    }

    fn commit(&mut self) {
        self.committed = true;
    }
}

impl Drop for BlockAllocGuard<'_> {
    fn drop(&mut self) {
        if self.committed {
            return;
        }

        for &bid in self.indirect_blocks.iter() {
            if let Err(err) = self.fs.free_blocks(bid, 1) {
                error!(
                    "failed to free indirect block {} in rollback: {:?}",
                    bid, err
                );
            }
        }

        if self.data_blocks.is_empty() {
            return;
        }
        let free_result = self
            .fs
            .free_blocks(self.data_blocks.start, self.data_blocks.len() as u32);
        if let Err(err) = free_result {
            error!("failed to free data blocks in rollback: {:?}", err);
        }
    }
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::ktest;

    use super::*;
    use crate::{
        fs::fs_impls::ext2::test_utils::{
            Ext2FixtureBuilder, Ext2MemoryDisk, assert_errno, write_indirect_ptr,
        },
        time::clocks,
    };

    fn alloc_single_block(tree: &mut BlockPtrTree, fs: &Ext2, iblock: Iblock) -> Result<Ext2Bid> {
        let step = tree.resolve_block_range(fs, iblock, 1)?;
        let range = match step {
            ResolvedBlockRange::Existing(r) | ResolvedBlockRange::NewlyAllocated(r) => r,
        };
        assert!(!range.is_empty());
        Ok(range.start)
    }

    fn expect_allocated(step: ResolvedBlockRange) -> Range<Ext2Bid> {
        match step {
            ResolvedBlockRange::NewlyAllocated(range) => range,
            ResolvedBlockRange::Existing(range) => {
                panic!("expected allocated range, got existing range {:?}", range)
            }
        }
    }

    fn expect_existing(step: ResolvedBlockRange) -> Range<Ext2Bid> {
        match step {
            ResolvedBlockRange::Existing(range) => range,
            ResolvedBlockRange::NewlyAllocated(range) => {
                panic!(
                    "expected existing range, got newly allocated range {:?}",
                    range
                )
            }
        }
    }

    fn make_block_ptr_tree(
        block_ptrs: [u32; RAW_BLOCK_PTRS_LEN],
        sector_count: u32,
        fs: &Arc<Ext2>,
    ) -> BlockPtrTree {
        BlockPtrTree::new(
            RawBlockPtrs::new(sector_count, block_ptrs),
            Arc::downgrade(fs),
            0,
            0,
        )
    }

    #[ktest]
    fn block_ptr_tree_direct_and_indirect_ok() {
        let f = Ext2FixtureBuilder::new(2, 256).build().unwrap();
        let disk = &f.disk;

        let ptrs = PTRS_PER_BLOCK as u32;
        let ptrs_bits = ptrs.trailing_zeros();
        let double_blocks = 1u32 << (ptrs_bits * 2);

        let indirect_bid = 40u32;
        let indirect_index = 5u32;
        let existing_bid = 77u32;
        write_indirect_ptr(disk.as_ref(), indirect_bid, indirect_index, existing_bid);

        let double_l1_bid = 41u32;
        let double_l2_bid = 42u32;
        let double_data_bid = 78u32;
        write_indirect_ptr(disk.as_ref(), double_l1_bid, 3, double_l2_bid);
        write_indirect_ptr(disk.as_ref(), double_l2_bid, 4, double_data_bid);

        let triple_l1_bid = 43u32;
        let triple_l2_bid = 44u32;
        let triple_l3_bid = 45u32;
        let triple_data_bid = 79u32;
        write_indirect_ptr(disk.as_ref(), triple_l1_bid, 2, triple_l2_bid);
        write_indirect_ptr(disk.as_ref(), triple_l2_bid, 3, triple_l3_bid);
        write_indirect_ptr(disk.as_ref(), triple_l3_bid, 4, triple_data_bid);

        let mut block_ptrs = [0u32; RAW_BLOCK_PTRS_LEN];
        block_ptrs[0] = 11;
        block_ptrs[12] = indirect_bid;
        block_ptrs[13] = double_l1_bid;
        block_ptrs[14] = triple_l1_bid;
        let block_ptr_tree = make_block_ptr_tree(block_ptrs, 0, &f.ext2);

        // Cover exact transition boundaries across all block-pointer tree levels.
        let direct_walk = block_ptr_tree.walk_at(0).unwrap();
        assert_eq!(direct_walk.num_levels(), 1);
        assert_eq!(direct_walk.root_slot(), 0);
        assert_eq!(direct_walk.max_blocks_in_leaf(u32::MAX), 12);

        let direct_last_walk = block_ptr_tree.walk_at(11).unwrap();
        assert_eq!(direct_last_walk.num_levels(), 1);
        assert_eq!(direct_last_walk.root_slot(), 11);
        assert_eq!(direct_last_walk.max_blocks_in_leaf(u32::MAX), 1);

        let indirect_first_walk = block_ptr_tree.walk_at(12).unwrap();
        assert_eq!(indirect_first_walk.num_levels(), 2);
        assert_eq!(indirect_first_walk.root_slot(), 12);
        assert_eq!(indirect_first_walk.slot_at(1), 0);

        let indirect_walk = block_ptr_tree.walk_at(12 + indirect_index).unwrap();
        assert_eq!(indirect_walk.num_levels(), 2);
        assert_eq!(indirect_walk.root_slot(), 12);
        assert_eq!(indirect_walk.slot_at(1), indirect_index);

        let indirect_last_iblock = 12 + ptrs - 1;
        let indirect_last_walk = block_ptr_tree.walk_at(indirect_last_iblock).unwrap();
        assert_eq!(indirect_last_walk.num_levels(), 2);
        assert_eq!(indirect_last_walk.root_slot(), 12);
        assert_eq!(indirect_last_walk.slot_at(1), ptrs - 1);
        assert_eq!(indirect_last_walk.max_blocks_in_leaf(u32::MAX), 1);

        let first_double_iblock = 12 + ptrs;
        let first_double_walk = block_ptr_tree.walk_at(first_double_iblock).unwrap();
        assert_eq!(first_double_walk.num_levels(), 3);
        assert_eq!(first_double_walk.root_slot(), 13);
        assert_eq!(first_double_walk.slot_at(1), 0);
        assert_eq!(first_double_walk.slot_at(2), 0);

        let double_iblock = 12 + ptrs + (3 << ptrs_bits) + 4;
        let double_walk = block_ptr_tree.walk_at(double_iblock).unwrap();
        assert_eq!(double_walk.num_levels(), 3);
        assert_eq!(double_walk.root_slot(), 13);
        assert_eq!(double_walk.slot_at(1), 3);
        assert_eq!(double_walk.slot_at(2), 4);

        let first_triple_iblock = 12 + ptrs + double_blocks;
        let first_triple_walk = block_ptr_tree.walk_at(first_triple_iblock).unwrap();
        assert_eq!(first_triple_walk.num_levels(), 4);
        assert_eq!(first_triple_walk.root_slot(), 14);
        assert_eq!(first_triple_walk.slot_at(1), 0);
        assert_eq!(first_triple_walk.slot_at(2), 0);
        assert_eq!(first_triple_walk.slot_at(3), 0);

        let triple_iblock =
            12 + ptrs + double_blocks + (2 << (ptrs_bits * 2)) + (3 << ptrs_bits) + 4;
        let triple_walk = block_ptr_tree.walk_at(triple_iblock).unwrap();
        assert_eq!(triple_walk.num_levels(), 4);
        assert_eq!(triple_walk.root_slot(), 14);
        assert_eq!(triple_walk.slot_at(1), 2);
        assert_eq!(triple_walk.slot_at(2), 3);
        assert_eq!(triple_walk.slot_at(3), 4);

        // Verify block lookup resolves direct/indirect/double/triple chains.
        assert_eq!(block_ptr_tree.lookup_block(0).unwrap(), Some(11));
        assert_eq!(block_ptr_tree.lookup_block(1).unwrap(), None);
        assert_eq!(
            block_ptr_tree.lookup_block(12 + indirect_index).unwrap(),
            Some(existing_bid)
        );
        assert_eq!(
            block_ptr_tree.lookup_block(double_iblock).unwrap(),
            Some(double_data_bid)
        );
        assert_eq!(
            block_ptr_tree.lookup_block(triple_iblock).unwrap(),
            Some(triple_data_bid)
        );
    }

    #[ktest]
    fn block_ptr_tree_get_block_range_returns_contiguous_runs() {
        let f = Ext2FixtureBuilder::new(2, 256).build().unwrap();
        let disk = &f.disk;

        let indirect_bid = 40u32;
        write_indirect_ptr(disk.as_ref(), indirect_bid, 0, 70);
        write_indirect_ptr(disk.as_ref(), indirect_bid, 1, 71);
        write_indirect_ptr(disk.as_ref(), indirect_bid, 2, 72);
        write_indirect_ptr(disk.as_ref(), indirect_bid, 3, 90);

        let mut block_ptrs = [0u32; RAW_BLOCK_PTRS_LEN];
        block_ptrs[0] = 11;
        block_ptrs[1] = 12;
        block_ptrs[2] = 13;
        block_ptrs[12] = indirect_bid;
        let block_ptr_tree = make_block_ptr_tree(block_ptrs, 0, &f.ext2);

        assert_eq!(block_ptr_tree.lookup_block_range(0, 4).unwrap(), 11..14);
        assert_eq!(block_ptr_tree.lookup_block(0).unwrap(), Some(11));
        assert_eq!(block_ptr_tree.lookup_block_range(12, 4).unwrap(), 70..73);
        assert_eq!(block_ptr_tree.lookup_block_range(15, 4).unwrap(), 90..91);
        assert!(block_ptr_tree.lookup_block_range(3, 4).unwrap().is_empty());
    }

    #[ktest]
    fn block_ptr_tree_out_of_range_iblock_returns_err() {
        let f = Ext2FixtureBuilder::new(2, 256).build().unwrap();
        let disk = &f.disk;

        let ptrs = PTRS_PER_BLOCK as u64;
        let direct = 12u64;
        let indirect = ptrs;
        let double_blocks = 1u64 << (ptrs.trailing_zeros() * 2);
        let triple_blocks = 1u64 << (ptrs.trailing_zeros() * 3);
        let max_iblock = direct + indirect + double_blocks + triple_blocks - 1;
        let too_big = (max_iblock + 1) as u32;

        let block_ptr_tree = make_block_ptr_tree([0; RAW_BLOCK_PTRS_LEN], 0, &f.ext2);

        // Accept the maximum valid logical block and reject the next one.
        block_ptr_tree.walk_at(max_iblock as u32).unwrap();

        let too_big_err = block_ptr_tree.walk_at(too_big).unwrap_err();
        assert_eq!(too_big_err.error(), Errno::EINVAL);

        let get_too_big_err = block_ptr_tree.lookup_block(too_big).unwrap_err();
        assert_eq!(get_too_big_err.error(), Errno::EINVAL);

        // Any zero pointer on the path is treated as a hole (None).
        let mut ptrs_for_indirect_hole = [0u32; RAW_BLOCK_PTRS_LEN];
        ptrs_for_indirect_hole[12] = 40;
        let indirect_hole_block_ptr_tree = make_block_ptr_tree(ptrs_for_indirect_hole, 0, &f.ext2);
        assert_eq!(
            indirect_hole_block_ptr_tree.lookup_block(12 + 7).unwrap(),
            None
        );

        let mut ptrs_for_double_hole = [0u32; RAW_BLOCK_PTRS_LEN];
        ptrs_for_double_hole[13] = 41;
        write_indirect_ptr(disk.as_ref(), 41, 3, 0);
        let double_hole_block_ptr_tree = make_block_ptr_tree(ptrs_for_double_hole, 0, &f.ext2);
        let double_hole_iblock = 12 + (ptrs as u32) + (3 << ptrs.trailing_zeros()) + 4;
        assert_eq!(
            double_hole_block_ptr_tree
                .lookup_block(double_hole_iblock)
                .unwrap(),
            None
        );

        let mut ptrs_for_triple_hole = [0u32; RAW_BLOCK_PTRS_LEN];
        ptrs_for_triple_hole[14] = 43;
        write_indirect_ptr(disk.as_ref(), 43, 2, 44);
        write_indirect_ptr(disk.as_ref(), 44, 3, 0);
        let triple_hole_block_ptr_tree = make_block_ptr_tree(ptrs_for_triple_hole, 0, &f.ext2);
        let triple_hole_iblock = 12
            + (ptrs as u32)
            + (double_blocks as u32)
            + (2 << (ptrs.trailing_zeros() * 2))
            + (3 << ptrs.trailing_zeros())
            + 4;
        assert_eq!(
            triple_hole_block_ptr_tree
                .lookup_block(triple_hole_iblock)
                .unwrap(),
            None
        );
    }

    #[ktest]
    fn approx_hole_blocks_scans() {
        clocks::init_for_ktest();
        let f = Ext2FixtureBuilder::new(2, 256).build().unwrap();
        let disk = &f.disk;
        let ptrs = PTRS_PER_BLOCK;
        let ptrs_bits = (ptrs as u32).trailing_zeros();

        // Direct blocks scan only the inode's direct pointer array and stop
        // before the first allocated slot.
        let mut direct_ptrs = [0u32; RAW_BLOCK_PTRS_LEN];
        direct_ptrs[2] = 91;
        let direct_block_ptr_tree = make_block_ptr_tree(direct_ptrs, 0, &f.ext2);
        assert_eq!(direct_block_ptr_tree.approx_hole_blocks(0, 1).unwrap(), 1);
        assert_eq!(direct_block_ptr_tree.approx_hole_blocks(0, 8).unwrap(), 2);
        assert_eq!(direct_block_ptr_tree.approx_hole_blocks(1, 8).unwrap(), 1);
        assert_eq!(direct_block_ptr_tree.approx_hole_blocks(2, 8).unwrap(), 0);

        // A leaf-level hole inside a present indirect block is scanned slot by
        // slot, just like the direct case.
        let indirect_bid = 40u32;
        write_indirect_ptr(disk.as_ref(), indirect_bid, 2, 77);
        let mut indirect_ptrs = [0u32; RAW_BLOCK_PTRS_LEN];
        indirect_ptrs[12] = indirect_bid;
        let indirect_block_ptr_tree = make_block_ptr_tree(indirect_ptrs, 0, &f.ext2);
        assert_eq!(
            indirect_block_ptr_tree.approx_hole_blocks(12, 1).unwrap(),
            1
        );
        assert_eq!(
            indirect_block_ptr_tree.approx_hole_blocks(12, 8).unwrap(),
            2
        );
        assert_eq!(
            indirect_block_ptr_tree.approx_hole_blocks(13, 8).unwrap(),
            1
        );
        assert_eq!(
            indirect_block_ptr_tree.approx_hole_blocks(14, 8).unwrap(),
            0
        );

        // A missing double-indirect root means the whole remaining subtree is
        // sparse, so the helper can skip it without reading child blocks.
        let double_hole_iblock = 12 + (ptrs as u32) + (3 << ptrs_bits) + 4;
        let expected_double_hole = ptrs.pow(2) as u32 - ((3 * ptrs) as u32 + 4);
        let missing_double_root_map = make_block_ptr_tree([0u32; RAW_BLOCK_PTRS_LEN], 0, &f.ext2);
        assert_eq!(
            missing_double_root_map
                .approx_hole_blocks(double_hole_iblock, expected_double_hole + 1)
                .unwrap(),
            expected_double_hole
        );

        // If the double-indirect root exists but the selected child pointer is
        // zero, only that child subtree is known to be sparse.
        let double_l1_bid = 41u32;
        write_indirect_ptr(disk.as_ref(), double_l1_bid, 3, 0);
        let mut double_ptrs = [0u32; RAW_BLOCK_PTRS_LEN];
        double_ptrs[13] = double_l1_bid;
        let double_middle_hole_map = make_block_ptr_tree(double_ptrs, 0, &f.ext2);
        assert_eq!(
            double_middle_hole_map
                .approx_hole_blocks(double_hole_iblock, ptrs as u32)
                .unwrap(),
            ptrs as u32 - 4
        );

        // Triple-indirect roots follow the same rule: a missing top-level root
        // covers the rest of the selected triple-indirect region.
        let triple_hole_iblock =
            12 + ptrs as u32 + ptrs.pow(2) as u32 + (2 << (ptrs_bits * 2)) + (3 << ptrs_bits) + 4;
        let expected_triple_top_hole =
            ptrs.pow(3) as u32 - ((2 * ptrs.pow(2)) as u32 + (3 * ptrs) as u32 + 4);
        let missing_triple_root_map = make_block_ptr_tree([0u32; RAW_BLOCK_PTRS_LEN], 0, &f.ext2);
        assert_eq!(
            missing_triple_root_map
                .approx_hole_blocks(triple_hole_iblock, expected_triple_top_hole + 1)
                .unwrap(),
            expected_triple_top_hole
        );

        // With the triple root present, a zero first-level child bounds the
        // sparse run to that child subtree.
        let triple_l1_bid = 42u32;
        write_indirect_ptr(disk.as_ref(), triple_l1_bid, 2, 0);
        let mut triple_l1_ptrs = [0u32; RAW_BLOCK_PTRS_LEN];
        triple_l1_ptrs[14] = triple_l1_bid;
        let triple_l1_hole_map = make_block_ptr_tree(triple_l1_ptrs, 0, &f.ext2);
        let expected_triple_l1_hole = ptrs.pow(2) as u32 - ((3 * ptrs) as u32 + 4);
        assert_eq!(
            triple_l1_hole_map
                .approx_hole_blocks(triple_hole_iblock, expected_triple_l1_hole + 1)
                .unwrap(),
            expected_triple_l1_hole
        );

        // Once the first-level child exists, a zero second-level child narrows
        // the hole to the remaining slots in that leaf subtree.
        let triple_l2_bid = 43u32;
        write_indirect_ptr(disk.as_ref(), triple_l1_bid, 2, triple_l2_bid);
        write_indirect_ptr(disk.as_ref(), triple_l2_bid, 3, 0);
        let triple_l2_hole_map = make_block_ptr_tree(triple_l1_ptrs, 0, &f.ext2);
        assert_eq!(
            triple_l2_hole_map
                .approx_hole_blocks(triple_hole_iblock, ptrs as u32)
                .unwrap(),
            ptrs as u32 - 4
        );
    }

    #[ktest]
    fn block_alloc_direct_range_ok() {
        let f = Ext2FixtureBuilder::new(1, 256)
            .with_free_blocks(64, 64)
            .build()
            .unwrap();
        let ext2 = &f.ext2;

        let mut block_ptr_tree = make_block_ptr_tree([0u32; RAW_BLOCK_PTRS_LEN], 0, &f.ext2);
        let free_before = ext2.super_block().free_blocks_count();
        let allocated_range =
            expect_allocated(block_ptr_tree.resolve_block_range(ext2, 0, 3).unwrap());
        let free_after = ext2.super_block().free_blocks_count();

        assert_eq!(allocated_range.end - allocated_range.start, 3);
        assert_eq!(
            block_ptr_tree.raw_block_ptrs.block_ptrs[0],
            allocated_range.start
        );
        assert_eq!(
            block_ptr_tree.raw_block_ptrs.block_ptrs[1],
            allocated_range.start + 1
        );
        assert_eq!(
            block_ptr_tree.raw_block_ptrs.block_ptrs[2],
            allocated_range.start + 2
        );
        assert_eq!(
            block_ptr_tree.lookup_block_range(0, 3).unwrap(),
            allocated_range.clone()
        );
        assert_eq!(
            expect_existing(block_ptr_tree.resolve_block_range(ext2, 0, 1).unwrap()).start,
            allocated_range.start
        );
        assert_eq!(
            block_ptr_tree.raw_block_ptrs.sector_count,
            SECTORS_PER_BLOCK * 3
        );
        assert_eq!(free_before - free_after, 3);
    }

    #[ktest]
    fn block_alloc_indirect_range_ok() {
        let f = Ext2FixtureBuilder::new(1, 256)
            .with_free_blocks(64, 64)
            .build()
            .unwrap();
        let ext2 = &f.ext2;

        let mut block_ptr_tree = make_block_ptr_tree([0u32; RAW_BLOCK_PTRS_LEN], 0, &f.ext2);
        let free_before = ext2.super_block().free_blocks_count();
        let allocated_range =
            expect_allocated(block_ptr_tree.resolve_block_range(ext2, 12, 4).unwrap());
        let free_after = ext2.super_block().free_blocks_count();

        assert_ne!(block_ptr_tree.raw_block_ptrs.block_ptrs[12], 0);
        assert_eq!(allocated_range.end - allocated_range.start, 4);
        assert_eq!(
            block_ptr_tree.lookup_block_range(12, 4).unwrap(),
            allocated_range.clone()
        );
        assert_eq!(
            block_ptr_tree.lookup_block(12).unwrap(),
            Some(allocated_range.start)
        );
        assert_eq!(
            block_ptr_tree.raw_block_ptrs.sector_count,
            SECTORS_PER_BLOCK * 5
        );
        assert_eq!(free_before - free_after, 5);
    }

    #[ktest]
    fn truncate_indirect_frees_shared_path() {
        let f = Ext2FixtureBuilder::new(1, 256)
            .with_free_blocks(64, 64)
            .build()
            .unwrap();
        let ext2 = &f.ext2;
        let ptrs = PTRS_PER_BLOCK as u32;
        let first_double_iblock = 12 + ptrs;

        let mut block_ptr_tree = make_block_ptr_tree([0u32; RAW_BLOCK_PTRS_LEN], 0, &f.ext2);
        alloc_single_block(&mut block_ptr_tree, ext2, first_double_iblock).unwrap();
        alloc_single_block(&mut block_ptr_tree, ext2, first_double_iblock + 1).unwrap();
        alloc_single_block(&mut block_ptr_tree, ext2, first_double_iblock + 2).unwrap();

        block_ptr_tree.truncate_to_byte_len(ext2, (first_double_iblock as usize + 1) * BLOCK_SIZE);
        assert!(
            block_ptr_tree
                .lookup_block(first_double_iblock)
                .unwrap()
                .is_some()
        );
        assert_eq!(
            block_ptr_tree
                .lookup_block(first_double_iblock + 1)
                .unwrap(),
            None
        );
        assert_eq!(
            block_ptr_tree
                .lookup_block(first_double_iblock + 2)
                .unwrap(),
            None
        );
    }

    #[ktest]
    fn truncate_releases_all_indirect_blocks() {
        let f = Ext2FixtureBuilder::new(1, 256)
            .with_free_blocks(64, 64)
            .build()
            .unwrap();
        let ext2 = &f.ext2;
        let ptrs = PTRS_PER_BLOCK as u32;
        let first_double_iblock = 12 + ptrs;
        let first_triple_iblock = 12 + ptrs + (1u32 << (ptrs.trailing_zeros() * 2));

        let mut block_ptr_tree = make_block_ptr_tree([0u32; RAW_BLOCK_PTRS_LEN], 0, &f.ext2);
        alloc_single_block(&mut block_ptr_tree, ext2, 12).unwrap();
        alloc_single_block(&mut block_ptr_tree, ext2, first_double_iblock).unwrap();
        alloc_single_block(&mut block_ptr_tree, ext2, first_triple_iblock).unwrap();
        assert_ne!(block_ptr_tree.raw_block_ptrs.block_ptrs[12], 0);
        assert_ne!(block_ptr_tree.raw_block_ptrs.block_ptrs[13], 0);
        assert_ne!(block_ptr_tree.raw_block_ptrs.block_ptrs[14], 0);

        block_ptr_tree.truncate_to_byte_len(ext2, 0);
        assert_eq!(block_ptr_tree.raw_block_ptrs.block_ptrs[12], 0);
        assert_eq!(block_ptr_tree.raw_block_ptrs.block_ptrs[13], 0);
        assert_eq!(block_ptr_tree.raw_block_ptrs.block_ptrs[14], 0);
        assert_eq!(block_ptr_tree.lookup_block(12).unwrap(), None);
        assert_eq!(
            block_ptr_tree.lookup_block(first_double_iblock).unwrap(),
            None
        );
        assert_eq!(
            block_ptr_tree.lookup_block(first_triple_iblock).unwrap(),
            None
        );
        assert_eq!(block_ptr_tree.raw_block_ptrs.sector_count, 0);
    }

    #[ktest]
    fn free_block_subtree_recursively_releases_blocks() {
        let f = Ext2FixtureBuilder::new(1, 256)
            .with_free_blocks(64, 64)
            .build()
            .unwrap();
        let ext2 = &f.ext2;

        let ptrs = PTRS_PER_BLOCK as u32;
        let first_triple_iblock = 12 + ptrs + (1u32 << (ptrs.trailing_zeros() * 2));

        let mut block_ptr_tree = make_block_ptr_tree([0u32; RAW_BLOCK_PTRS_LEN], 0, &f.ext2);
        alloc_single_block(&mut block_ptr_tree, ext2, first_triple_iblock).unwrap();
        let root = block_ptr_tree.raw_block_ptrs.block_ptrs[14];
        assert_ne!(root, 0);
        assert_eq!(
            block_ptr_tree.raw_block_ptrs.sector_count,
            SECTORS_PER_BLOCK * 4
        );

        let free_before = ext2.super_block().free_blocks_count();
        block_ptr_tree.free_block_subtree(ext2, root, 3);
        block_ptr_tree.raw_block_ptrs.block_ptrs[14] = 0;
        let free_after = ext2.super_block().free_blocks_count();

        assert_eq!(free_after - free_before, 4);
        assert_eq!(block_ptr_tree.raw_block_ptrs.sector_count, 0);
    }

    #[ktest]
    fn lookup_inline_extent_range() {
        let f = Ext2FixtureBuilder::new(1, 256).build().unwrap();
        let mut bytes = [0u8; RAW_BLOCK_PTRS_LEN * size_of::<u32>()];
        bytes[0..2].copy_from_slice(&EXTENT_MAGIC.to_le_bytes());
        bytes[2..4].copy_from_slice(&1u16.to_le_bytes());
        bytes[4..6].copy_from_slice(&4u16.to_le_bytes());
        bytes[6..8].copy_from_slice(&0u16.to_le_bytes());
        bytes[12..16].copy_from_slice(&3u32.to_le_bytes());
        bytes[16..18].copy_from_slice(&5u16.to_le_bytes());
        bytes[20..24].copy_from_slice(&700u32.to_le_bytes());
        let mut block_ptrs = [0u32; RAW_BLOCK_PTRS_LEN];
        for (index, chunk) in bytes.chunks_exact(4).enumerate() {
            block_ptrs[index] = u32::from_le_bytes(chunk.try_into().unwrap());
        }
        let tree = make_block_ptr_tree(block_ptrs, 0, &f.ext2);
        assert_eq!(tree.lookup_block_range(3, 8).unwrap(), 700..705);
        assert_eq!(tree.lookup_block(7).unwrap(), Some(704));
        assert_eq!(tree.lookup_block(8).unwrap(), None);
    }

    /// Builds a depth-one root with `index` entries and returns its inode
    /// block words.
    fn depth_one_root(index: &[(u32, Ext2Bid)], generation: u32) -> [u32; RAW_BLOCK_PTRS_LEN] {
        let mut bytes = [0u8; RAW_BLOCK_PTRS_LEN * size_of::<u32>()];
        bytes[0..2].copy_from_slice(&EXTENT_MAGIC.to_le_bytes());
        bytes[2..4].copy_from_slice(&(index.len() as u16).to_le_bytes());
        bytes[4..6].copy_from_slice(&4u16.to_le_bytes());
        bytes[6..8].copy_from_slice(&1u16.to_le_bytes());
        bytes[8..12].copy_from_slice(&generation.to_le_bytes());
        for (position, (logical, leaf)) in index.iter().enumerate() {
            let offset = EXTENT_HEADER_SIZE + position * EXTENT_RECORD_SIZE;
            bytes[offset..offset + 4].copy_from_slice(&logical.to_le_bytes());
            bytes[offset + 4..offset + 8].copy_from_slice(&leaf.to_le_bytes());
        }
        let mut block_ptrs = [0u32; RAW_BLOCK_PTRS_LEN];
        for (position, chunk) in bytes.chunks_exact(4).enumerate() {
            block_ptrs[position] = u32::from_le_bytes(chunk.try_into().unwrap());
        }
        block_ptrs
    }

    fn write_leaf(
        disk: &Ext2MemoryDisk,
        leaf: Ext2Bid,
        records: &[ExtentRecord],
        max_entries: usize,
        generation: u32,
    ) {
        let mut bytes = [0u8; BLOCK_SIZE];
        BlockPtrTree::encode_extent_block(&mut bytes, records, max_entries, generation).unwrap();
        disk.segment()
            .write_bytes(Bid::new(leaf as u64).to_offset(), &bytes)
            .unwrap();
    }

    #[ktest]
    fn depth_one_hole_write_splits_a_full_leaf() {
        let f = Ext2FixtureBuilder::new(2, 256)
            .with_free_blocks(64, 64)
            .build()
            .unwrap();
        let ext2 = &f.ext2;
        let disk = &f.disk;
        let first = ext2.alloc_blocks(1, 0).unwrap().start;
        let second = ext2.alloc_blocks(1, 0).unwrap().start;
        let leaf = ext2.alloc_blocks(1, 0).unwrap().start;
        let generation = 7;

        let records = [
            ExtentRecord {
                logical: 0,
                len: 1,
                physical: first,
                unwritten: false,
            },
            ExtentRecord {
                logical: 2,
                len: 1,
                physical: second,
                unwritten: false,
            },
        ];
        write_leaf(disk, leaf, &records, 2, generation);
        let mut tree =
            make_block_ptr_tree(depth_one_root(&[(0, leaf)], generation), SECTORS_PER_BLOCK, ext2);

        // Block 4 is a hole past the leaf's last extent; the leaf is full, so
        // the insertion splits it and adds a second index entry.
        let allocated = alloc_single_block(&mut tree, ext2, 4).unwrap();
        assert_eq!(tree.lookup_block(0).unwrap(), Some(first));
        assert_eq!(tree.lookup_block(2).unwrap(), Some(second));
        assert_eq!(tree.lookup_block(4).unwrap(), Some(allocated));

        let root = tree.raw_block_ptrs.extent_bytes();
        assert_eq!(u16::from_le_bytes([root[2], root[3]]), 2);
        assert_eq!(u16::from_le_bytes([root[6], root[7]]), 1);
        assert_eq!(u32::from_le_bytes(root[12..16].try_into().unwrap()), 0);
        assert_eq!(u32::from_le_bytes(root[24..28].try_into().unwrap()), 2);
        let new_leaf = u32::from_le_bytes(root[28..32].try_into().unwrap());

        let first_leaf = tree.read_extent_leaf(leaf).unwrap();
        assert_eq!(u16::from_le_bytes([first_leaf[2], first_leaf[3]]), 1);
        let second_leaf = tree.read_extent_leaf(new_leaf).unwrap();
        assert_eq!(u16::from_le_bytes([second_leaf[2], second_leaf[3]]), 2);
        assert_eq!(
            u32::from_le_bytes(second_leaf[12..16].try_into().unwrap()),
            2
        );
        assert_eq!(
            u32::from_le_bytes(second_leaf[24..28].try_into().unwrap()),
            4
        );
    }

    #[ktest]
    fn depth_one_hole_write_rejects_a_full_index() {
        let f = Ext2FixtureBuilder::new(2, 256)
            .with_free_blocks(64, 64)
            .build()
            .unwrap();
        let ext2 = &f.ext2;
        let disk = &f.disk;
        let first = ext2.alloc_blocks(1, 0).unwrap().start;
        let second = ext2.alloc_blocks(1, 0).unwrap().start;
        let leaf = ext2.alloc_blocks(1, 0).unwrap().start;

        let records = [
            ExtentRecord {
                logical: 0,
                len: 1,
                physical: first,
                unwritten: false,
            },
            ExtentRecord {
                logical: 2,
                len: 1,
                physical: second,
                unwritten: false,
            },
        ];
        write_leaf(disk, leaf, &records, 2, 7);
        let index = [(0, leaf), (10, leaf), (20, leaf), (30, leaf)];
        let mut tree = make_block_ptr_tree(depth_one_root(&index, 7), SECTORS_PER_BLOCK, ext2);

        // Splitting would need a fifth index entry, which the inode cannot
        // hold; the error must be reported instead of dropping the write.
        assert_errno!(
            alloc_single_block(&mut tree, ext2, 4),
            Errno::EOPNOTSUPP
        );
        let root = tree.raw_block_ptrs.extent_bytes();
        assert_eq!(u16::from_le_bytes([root[2], root[3]]), 4);
        assert_eq!(tree.lookup_block(4).unwrap(), None);
    }

    #[ktest]
    fn rejects_overflowing_inline_extent_range() {
        let f = Ext2FixtureBuilder::new(1, 256).build().unwrap();
        let mut bytes = [0u8; RAW_BLOCK_PTRS_LEN * size_of::<u32>()];
        bytes[0..2].copy_from_slice(&EXTENT_MAGIC.to_le_bytes());
        bytes[2..4].copy_from_slice(&1u16.to_le_bytes());
        bytes[4..6].copy_from_slice(&4u16.to_le_bytes());
        bytes[6..8].copy_from_slice(&0u16.to_le_bytes());
        bytes[12..16].copy_from_slice(&u32::MAX.to_le_bytes());
        bytes[16..18].copy_from_slice(&2u16.to_le_bytes());
        bytes[20..24].copy_from_slice(&700u32.to_le_bytes());
        let mut block_ptrs = [0u32; RAW_BLOCK_PTRS_LEN];
        for (index, chunk) in bytes.chunks_exact(4).enumerate() {
            block_ptrs[index] = u32::from_le_bytes(chunk.try_into().unwrap());
        }
        let tree = make_block_ptr_tree(block_ptrs, 0, &f.ext2);
        assert!(tree.lookup_block_range(u32::MAX, 1).is_err());
    }

    #[ktest]
    fn truncate_inline_extent_range_releases_suffix() {
        let f = Ext2FixtureBuilder::new(1, 256)
            .with_free_blocks(64, 64)
            .build()
            .unwrap();
        let data = f.ext2.alloc_blocks(4, 64).unwrap();
        let mut bytes = [0u8; RAW_BLOCK_PTRS_LEN * size_of::<u32>()];
        bytes[0..2].copy_from_slice(&EXTENT_MAGIC.to_le_bytes());
        bytes[2..4].copy_from_slice(&1u16.to_le_bytes());
        bytes[4..6].copy_from_slice(&4u16.to_le_bytes());
        bytes[12..16].copy_from_slice(&0u32.to_le_bytes());
        bytes[16..18].copy_from_slice(&4u16.to_le_bytes());
        bytes[20..24].copy_from_slice(&data.start.to_le_bytes());
        let mut block_ptrs = [0u32; RAW_BLOCK_PTRS_LEN];
        for (index, chunk) in bytes.chunks_exact(4).enumerate() {
            block_ptrs[index] = u32::from_le_bytes(chunk.try_into().unwrap());
        }
        let mut tree = make_block_ptr_tree(block_ptrs, SECTORS_PER_BLOCK * 4, &f.ext2);
        let free_before = f.ext2.super_block().free_blocks_count();

        tree.truncate_to_byte_len(&f.ext2, BLOCK_SIZE);

        assert_eq!(tree.lookup_block(0).unwrap(), Some(data.start));
        assert_eq!(tree.lookup_block(1).unwrap(), None);
        assert_eq!(tree.raw_block_ptrs.sector_count, SECTORS_PER_BLOCK);
        assert_eq!(f.ext2.super_block().free_blocks_count() - free_before, 3);
    }
}
