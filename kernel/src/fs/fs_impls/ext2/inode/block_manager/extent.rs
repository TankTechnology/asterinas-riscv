// SPDX-License-Identifier: MPL-2.0

//! Read-side support for ext4 extent trees.
//!
//! Extent inodes use the same 60-byte `i_block` area as ext2, but store a
//! small extent header and either leaf extents or indexes to child extent
//! blocks there.  The compatibility filesystem keeps allocation and tree
//! mutation on the established indirect path; extent inodes therefore expose
//! existing mappings for reads and overwrites, while attempts to allocate or
//! truncate them are rejected by the block-pointer facade.

use crate::fs::fs_impls::ext2::{Ext2, prelude::*};

const EXTENT_MAGIC: u16 = 0xf30a;
const EXTENT_HEADER_SIZE: usize = 12;
const EXTENT_ENTRY_SIZE: usize = 12;
const EXTENT_ROOT_WORDS: usize = 15;
const MAX_EXTENT_DEPTH: u16 = 5;

#[derive(Debug)]
pub(super) struct ExtentTree {
    root: [u32; EXTENT_ROOT_WORDS],
    fs: Weak<Ext2>,
}

impl ExtentTree {
    pub(super) fn new(root: [u32; EXTENT_ROOT_WORDS], fs: Weak<Ext2>) -> Self {
        Self { root, fs }
    }

    pub(super) fn lookup_block_range(
        &self,
        iblock: Iblock,
        max_blocks: u32,
    ) -> Result<Range<Ext2Bid>> {
        if max_blocks == 0 {
            return_errno_with_message!(Errno::EINVAL, "zero block range requested");
        }
        let header = ExtentHeader::from_words(&self.root)?;
        self.lookup_node(&self.root, header.depth, iblock, max_blocks)
    }

    fn lookup_node(
        &self,
        words: &[u32],
        depth: u16,
        iblock: Iblock,
        max_blocks: u32,
    ) -> Result<Range<Ext2Bid>> {
        let header = ExtentHeader::from_words(words)?;
        if header.depth != depth {
            return_errno_with_message!(Errno::EUCLEAN, "inconsistent ext4 extent depth");
        }
        let fs = self
            .fs
            .upgrade()
            .ok_or_else(|| Error::with_message(Errno::EIO, "filesystem already dropped"))?;
        validate_entries(words, header, fs.super_block().total_blocks())?;
        if depth == 0 {
            return self.lookup_leaf(words, header.entries, iblock, max_blocks);
        }

        let mut selected_leaf = None;
        for index in 0..header.entries {
            let offset = EXTENT_HEADER_SIZE + index * EXTENT_ENTRY_SIZE;
            let logical = read_word(words, offset)?;
            if logical > iblock {
                break;
            }
            let leaf_lo = read_word(words, offset + 4)?;
            let leaf_hi = read_half(words, offset + 8)?;
            selected_leaf = Some((logical, (u64::from(leaf_hi) << 32) | u64::from(leaf_lo)));
        }
        let Some((_, leaf)) = selected_leaf else {
            return Ok(0..0);
        };
        let leaf = Ext2Bid::try_from(leaf).map_err(|_| {
            Error::with_message(Errno::EOPNOTSUPP, "ext4 extent block is too large")
        })?;
        let child = self.read_block(leaf)?;
        self.lookup_node(&child, depth - 1, iblock, max_blocks)
    }

    fn lookup_leaf(
        &self,
        words: &[u32],
        entries: usize,
        iblock: Iblock,
        max_blocks: u32,
    ) -> Result<Range<Ext2Bid>> {
        for index in 0..entries {
            let offset = EXTENT_HEADER_SIZE + index * EXTENT_ENTRY_SIZE;
            let logical = read_word(words, offset)?;
            let raw_len = read_half(words, offset + 4)?;
            let unwritten = raw_len > 0x8000;
            let len = if unwritten { raw_len - 0x8000 } else { raw_len };
            if len == 0 {
                return_errno_with_message!(Errno::EUCLEAN, "zero-length ext4 extent");
            }
            let end = logical
                .checked_add(u32::from(len))
                .ok_or_else(|| Error::with_message(Errno::EUCLEAN, "ext4 extent overflows"))?;
            if iblock < logical {
                return Ok(0..0);
            }
            if iblock >= end {
                continue;
            }
            if unwritten {
                return Ok(0..0);
            }
            let start_hi = read_half(words, offset + 6)?;
            let start_lo = read_word(words, offset + 8)?;
            let physical = (u64::from(start_hi) << 32) | u64::from(start_lo);
            let physical = Ext2Bid::try_from(physical).map_err(|_| {
                Error::with_message(Errno::EOPNOTSUPP, "ext4 extent block is too large")
            })?;
            let skip = iblock - logical;
            let start = physical
                .checked_add(skip)
                .ok_or_else(|| Error::with_message(Errno::EUCLEAN, "ext4 extent overflows"))?;
            let length = u32::from(len) - skip;
            let length = length.min(max_blocks);
            let end = start.checked_add(length).ok_or_else(|| {
                Error::with_message(Errno::EUCLEAN, "ext4 physical extent overflows")
            })?;
            return Ok(start..end);
        }
        Ok(0..0)
    }

    fn read_block(&self, bid: Ext2Bid) -> Result<Vec<u32>> {
        let fs = self
            .fs
            .upgrade()
            .ok_or_else(|| Error::with_message(Errno::EIO, "filesystem already dropped"))?;
        let frame = FrameAllocOptions::new().zeroed(false).alloc_frame()?;
        let segment = BioSegment::new_from_segment(
            Segment::<()>::from(frame.clone()).into(),
            BioDirection::FromDevice,
        );
        fs.read_blocks(bid, segment)?;
        let mut words = Vec::with_capacity(BLOCK_SIZE / size_of::<u32>());
        for offset in (0..BLOCK_SIZE).step_by(size_of::<u32>()) {
            words.push(frame.read_val(offset).map_err(|_| {
                Error::with_message(Errno::EIO, "failed to read ext4 extent block")
            })?);
        }
        Ok(words)
    }
}

#[derive(Clone, Copy)]
struct ExtentHeader {
    entries: usize,
    depth: u16,
}

impl ExtentHeader {
    fn from_words(words: &[u32]) -> Result<Self> {
        if words.len() * size_of::<u32>() < EXTENT_HEADER_SIZE {
            return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 extent header");
        }
        let header_word = words[0];
        let magic = header_word as u16;
        let entries = (header_word >> 16) as usize;
        let limit_word = words[1];
        let max_entries = (limit_word & 0xffff) as usize;
        let depth = (limit_word >> 16) as u16;
        if magic != EXTENT_MAGIC
            || entries > max_entries
            || max_entries == 0
            || max_entries
                > (words.len() * size_of::<u32>() - EXTENT_HEADER_SIZE) / EXTENT_ENTRY_SIZE
            || depth > MAX_EXTENT_DEPTH
            || (depth != 0 && entries == 0)
        {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 extent header");
        }
        Ok(Self { entries, depth })
    }
}

/// Validate every entry, not only the one selected by a lookup. This prevents
/// a corrupt ordering from disguising an allocated range as a sparse hole.
fn validate_entries(words: &[u32], header: ExtentHeader, total_blocks: u32) -> Result<()> {
    let mut previous_end = 0u64;
    for index in 0..header.entries {
        let offset = EXTENT_HEADER_SIZE + index * EXTENT_ENTRY_SIZE;
        let logical = u64::from(read_word(words, offset)?);
        if index != 0 && logical < previous_end {
            return_errno_with_message!(Errno::EUCLEAN, "unordered ext4 extent entries");
        }
        let (physical, length) = if header.depth == 0 {
            let raw_len = read_half(words, offset + 4)?;
            let length = if raw_len > 0x8000 {
                raw_len - 0x8000
            } else {
                raw_len
            };
            if length == 0 {
                return_errno_with_message!(Errno::EUCLEAN, "zero-length ext4 extent");
            }
            previous_end = logical.checked_add(u64::from(length)).ok_or_else(|| {
                Error::with_message(Errno::EUCLEAN, "ext4 logical extent overflows")
            })?;
            if previous_end > u64::from(u32::MAX) + 1 {
                return_errno_with_message!(Errno::EUCLEAN, "ext4 logical extent overflows");
            }
            (
                (u64::from(read_half(words, offset + 6)?) << 32)
                    | u64::from(read_word(words, offset + 8)?),
                u64::from(length),
            )
        } else {
            previous_end = logical.checked_add(1).ok_or_else(|| {
                Error::with_message(Errno::EUCLEAN, "ext4 extent index overflows")
            })?;
            (
                (u64::from(read_half(words, offset + 8)?) << 32)
                    | u64::from(read_word(words, offset + 4)?),
                1,
            )
        };
        let physical_end = physical
            .checked_add(length)
            .ok_or_else(|| Error::with_message(Errno::EUCLEAN, "ext4 physical extent overflows"))?;
        if physical == 0 || physical_end > u64::from(total_blocks) {
            return_errno_with_message!(Errno::EUCLEAN, "ext4 extent outside filesystem");
        }
    }
    Ok(())
}

fn read_word(words: &[u32], byte_offset: usize) -> Result<u32> {
    words
        .get(byte_offset / size_of::<u32>())
        .copied()
        .ok_or_else(|| Error::with_message(Errno::EUCLEAN, "truncated ext4 extent entry"))
}

fn read_half(words: &[u32], byte_offset: usize) -> Result<u16> {
    let word = read_word(words, byte_offset & !3)?;
    Ok(if byte_offset & 2 == 0 {
        word as u16
    } else {
        (word >> 16) as u16
    })
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::*;

    use super::*;
    use crate::fs::fs_impls::ext2::test_utils::Ext2FixtureBuilder;

    fn direct_root() -> [u32; EXTENT_ROOT_WORDS] {
        let mut root = [0; EXTENT_ROOT_WORDS];
        root[0] = u32::from(EXTENT_MAGIC) | (1 << 16);
        root[1] = 4;
        root[3] = 0;
        root[4] = 4;
        root[5] = 100;
        root
    }

    #[ktest]
    fn resolves_direct_extent_ranges() {
        let fixture = Ext2FixtureBuilder::new(1, 256).build().unwrap();
        let tree = ExtentTree::new(direct_root(), Arc::downgrade(&fixture.ext2));
        assert_eq!(tree.lookup_block_range(0, 4).unwrap(), 100..104);
        assert_eq!(tree.lookup_block_range(2, 8).unwrap(), 102..104);
        assert_eq!(tree.lookup_block_range(8, 1).unwrap(), 0..0);
    }

    #[ktest]
    fn rejects_invalid_extent_header() {
        let fixture = Ext2FixtureBuilder::new(1, 256).build().unwrap();
        let tree = ExtentTree::new([0; EXTENT_ROOT_WORDS], Arc::downgrade(&fixture.ext2));
        assert!(tree.lookup_block_range(0, 1).is_err());
    }
}
