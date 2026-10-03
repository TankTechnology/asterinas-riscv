// SPDX-License-Identifier: MPL-2.0

//! Minimal JBD2 journal-superblock decoding shared by the ext4 compatibility
//! mount and the future transaction/replay implementation.

use super::prelude::*;

const JBD2_MAGIC: u32 = 0xc03b3998;
const JBD2_SUPERBLOCK_V2: u32 = 4;
const JBD2_SUPERBLOCK_HEADER_SIZE: usize = 12;
const JBD2_BLOCK_SIZE_OFFSET: usize = 12;
const JBD2_MAX_LENGTH_OFFSET: usize = 16;
const JBD2_FIRST_OFFSET: usize = 20;
pub(super) const JBD2_SEQUENCE_OFFSET: usize = 24;
pub(super) const JBD2_START_OFFSET: usize = 28;
const JBD2_FEATURE_COMPAT_OFFSET: usize = 36;
const JBD2_FEATURE_INCOMPAT_OFFSET: usize = 40;
const JBD2_FEATURE_RO_COMPAT_OFFSET: usize = 44;
const JBD2_SUPERBLOCK_MIN_SIZE: usize = JBD2_FEATURE_RO_COMPAT_OFFSET + 4;
const JBD2_DESCRIPTOR_BLOCK: u32 = 1;
const JBD2_COMMIT_BLOCK: u32 = 2;
const JBD2_REVOKE_BLOCK: u32 = 5;
const JBD2_TAG_SIZE: usize = 8;
const JBD2_FLAG_ESCAPE: u32 = 1;
const JBD2_FLAG_SAME_UUID: u32 = 2;
const JBD2_FLAG_DELETED: u32 = 4;
const JBD2_FLAG_LAST_TAG: u32 = 8;
const JBD2_FEATURE_INCOMPAT_REVOKE: u32 = 1;
pub(super) const JOURNAL_FLAG_ESCAPE: u32 = JBD2_FLAG_ESCAPE;
pub(super) const JOURNAL_FLAG_DELETED: u32 = JBD2_FLAG_DELETED;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct JournalSuperBlock {
    pub(super) sequence: u32,
    pub(super) block_size: u32,
    pub(super) max_length: u32,
    pub(super) first: u32,
    pub(super) start: u32,
}

impl JournalSuperBlock {
    pub(super) fn parse(block: &[u8]) -> Result<Self> {
        if block.len() < JBD2_SUPERBLOCK_MIN_SIZE {
            return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal superblock");
        }
        if read_be_u32(block, 0) != JBD2_MAGIC {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal magic");
        }
        if read_be_u32(block, 4) != JBD2_SUPERBLOCK_V2 {
            return_errno_with_message!(Errno::EOPNOTSUPP, "unsupported ext4 journal version");
        }

        let block_size = read_be_u32(block, JBD2_BLOCK_SIZE_OFFSET);
        let max_length = read_be_u32(block, JBD2_MAX_LENGTH_OFFSET);
        let first = read_be_u32(block, JBD2_FIRST_OFFSET);
        let start = read_be_u32(block, JBD2_START_OFFSET);
        let feature_compat = read_be_u32(block, JBD2_FEATURE_COMPAT_OFFSET);
        let feature_incompat = read_be_u32(block, JBD2_FEATURE_INCOMPAT_OFFSET);
        let feature_ro_compat = read_be_u32(block, JBD2_FEATURE_RO_COMPAT_OFFSET);
        if feature_compat != 0
            || feature_incompat & !JBD2_FEATURE_INCOMPAT_REVOKE != 0
            || feature_ro_compat != 0
        {
            return_errno_with_message!(Errno::EOPNOTSUPP, "unsupported ext4 journal features");
        }
        if block_size as usize != BLOCK_SIZE || max_length <= 1 || first == 0 || first >= max_length
        {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal geometry");
        }
        if start != 0 && (start < first || start >= max_length) {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal start");
        }
        let sequence = read_be_u32(block, JBD2_SEQUENCE_OFFSET);
        if start != 0 && sequence == 0 {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal sequence");
        }

        Ok(Self {
            sequence,
            block_size,
            max_length,
            first,
            start,
        })
    }

    pub(super) const fn needs_recovery(self) -> bool {
        self.start != 0
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct JournalHeader {
    pub(super) block_type: u32,
    pub(super) sequence: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct JournalTag {
    pub(super) block_number: u32,
    pub(super) flags: u32,
}

/// A single metadata transaction in the unchecksummed JBD2 v2 format.
///
/// The encoder deliberately keeps the transaction in memory and emits one
/// descriptor block followed by one payload block per target and a commit
/// block. Ring placement and durable writes belong to the filesystem layer,
/// which can then order journal writes before home-block writes.
#[derive(Debug)]
pub(super) struct JournalTransaction {
    sequence: u32,
    blocks: Vec<(u32, Vec<u8>)>,
}

/// Validated placement cursor for the circular JBD2 journal area.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct JournalRing {
    first: u32,
    max_length: u32,
    next: u32,
}

impl JournalRing {
    pub(super) fn new(journal: JournalSuperBlock) -> Result<Self> {
        let next = if journal.start == 0 {
            journal.first
        } else {
            journal.start
        };
        if next < journal.first || next >= journal.max_length {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal ring cursor");
        }
        Ok(Self {
            first: journal.first,
            max_length: journal.max_length,
            next,
        })
    }

    pub(super) fn reserve(&mut self, blocks: usize) -> Result<Vec<u32>> {
        if blocks == 0 {
            return_errno_with_message!(Errno::EINVAL, "zero ext4 journal blocks requested");
        }
        let capacity = (self.max_length - self.first) as usize;
        if blocks > capacity {
            return_errno_with_message!(Errno::ENOSPC, "ext4 journal transaction exceeds ring");
        }
        let mut positions = Vec::with_capacity(blocks);
        for _ in 0..blocks {
            positions.push(self.next);
            self.next = if self.next + 1 >= self.max_length {
                self.first
            } else {
                self.next + 1
            };
        }
        Ok(positions)
    }

    pub(super) const fn cursor(self) -> u32 {
        self.next
    }
}

impl JournalTransaction {
    pub(super) fn new(sequence: u32) -> Result<Self> {
        if sequence == 0 {
            return_errno_with_message!(Errno::EINVAL, "zero ext4 journal sequence");
        }
        Ok(Self {
            sequence,
            blocks: Vec::new(),
        })
    }

    pub(super) fn add_block(&mut self, block_number: u32, payload: &[u8]) -> Result<()> {
        if block_number == 0 {
            return_errno_with_message!(Errno::EINVAL, "zero ext4 journal target block");
        }
        if payload.len() != BLOCK_SIZE {
            return_errno_with_message!(Errno::EINVAL, "invalid ext4 journal payload size");
        }
        if self.blocks.iter().any(|(block, _)| *block == block_number) {
            return_errno_with_message!(Errno::EEXIST, "duplicate ext4 journal target block");
        }
        self.blocks.push((block_number, payload.to_vec()));
        Ok(())
    }

    pub(super) const fn sequence(&self) -> u32 {
        self.sequence
    }

    pub(super) fn encode(self) -> Result<Vec<Vec<u8>>> {
        if self.blocks.is_empty() {
            return_errno_with_message!(Errno::EINVAL, "empty ext4 journal transaction");
        }
        let tag_capacity = (BLOCK_SIZE - JBD2_SUPERBLOCK_HEADER_SIZE) / JBD2_TAG_SIZE;
        if self.blocks.len() > tag_capacity {
            return_errno_with_message!(Errno::E2BIG, "ext4 journal transaction is too large");
        }
        let mut descriptor = vec![0; BLOCK_SIZE];
        put_be_u32(&mut descriptor, 0, JBD2_MAGIC);
        put_be_u32(&mut descriptor, 4, JBD2_DESCRIPTOR_BLOCK);
        put_be_u32(&mut descriptor, 8, self.sequence);
        for (index, (block_number, payload)) in self.blocks.iter().enumerate() {
            let offset = JBD2_SUPERBLOCK_HEADER_SIZE + index * JBD2_TAG_SIZE;
            let mut flags = if index + 1 == self.blocks.len() {
                JBD2_FLAG_LAST_TAG
            } else {
                0
            };
            if read_be_u32(payload, 0) == JBD2_MAGIC {
                flags |= JBD2_FLAG_ESCAPE;
            }
            put_be_u32(&mut descriptor, offset, *block_number);
            put_be_u32(&mut descriptor, offset + 4, flags);
        }

        let mut encoded = vec![descriptor];
        for (_, payload) in self.blocks {
            let mut payload = payload;
            if read_be_u32(&payload, 0) == JBD2_MAGIC {
                payload[..4].fill(0);
            }
            encoded.push(payload);
        }
        let mut commit = vec![0; BLOCK_SIZE];
        put_be_u32(&mut commit, 0, JBD2_MAGIC);
        put_be_u32(&mut commit, 4, JBD2_COMMIT_BLOCK);
        put_be_u32(&mut commit, 8, self.sequence);
        encoded.push(commit);
        Ok(encoded)
    }
}

pub(super) fn parse_header(block: &[u8]) -> Result<JournalHeader> {
    if block.len() < JBD2_SUPERBLOCK_HEADER_SIZE {
        return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal block");
    }
    if read_be_u32(block, 0) != JBD2_MAGIC {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal block magic");
    }
    Ok(JournalHeader {
        block_type: read_be_u32(block, 4),
        sequence: read_be_u32(block, 8),
    })
}

pub(super) fn parse_descriptor(block: &[u8], sequence: u32) -> Result<Vec<JournalTag>> {
    let header = parse_header(block)?;
    if header.block_type != JBD2_DESCRIPTOR_BLOCK || header.sequence != sequence {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal descriptor");
    }

    let mut tags = Vec::new();
    let mut offset = JBD2_SUPERBLOCK_HEADER_SIZE;
    loop {
        if offset.checked_add(JBD2_TAG_SIZE).is_none() || offset + JBD2_TAG_SIZE > block.len() {
            return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal descriptor tag");
        }
        let tag = JournalTag {
            block_number: read_be_u32(block, offset),
            flags: read_be_u32(block, offset + 4),
        };
        let known_flags =
            JBD2_FLAG_ESCAPE | JBD2_FLAG_SAME_UUID | JBD2_FLAG_DELETED | JBD2_FLAG_LAST_TAG;
        if tag.flags & !known_flags != 0 {
            return_errno_with_message!(Errno::EOPNOTSUPP, "unsupported ext4 journal tag flags");
        }
        if tag.block_number == 0 {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal target block");
        }
        tags.push(tag);
        offset += JBD2_TAG_SIZE;
        if tag.flags & JBD2_FLAG_LAST_TAG != 0 {
            break;
        }
    }
    Ok(tags)
}

pub(super) fn validate_commit(block: &[u8], sequence: u32) -> Result<()> {
    let header = parse_header(block)?;
    if header.block_type != JBD2_COMMIT_BLOCK || header.sequence != sequence {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal commit");
    }
    Ok(())
}

pub(super) fn parse_revoke(block: &[u8], sequence: u32) -> Result<Vec<u32>> {
    let header = parse_header(block)?;
    if header.block_type != JBD2_REVOKE_BLOCK || header.sequence != sequence {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal revoke");
    }
    if block.len() < 16 {
        return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal revoke");
    }
    let bytes = read_be_u32(block, 12) as usize;
    if bytes < 16 || bytes > block.len() || (bytes - 16) % 4 != 0 {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal revoke length");
    }
    let mut revoked = Vec::with_capacity((bytes - 16) / 4);
    for offset in (16..bytes).step_by(4) {
        let block_number = read_be_u32(block, offset);
        if block_number == 0 {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 revoked block");
        }
        revoked.push(block_number);
    }
    Ok(revoked)
}

fn read_be_u32(block: &[u8], offset: usize) -> u32 {
    u32::from_be_bytes([
        block[offset],
        block[offset + 1],
        block[offset + 2],
        block[offset + 3],
    ])
}

fn put_be_u32(block: &mut [u8], offset: usize, value: u32) {
    block[offset..offset + 4].copy_from_slice(&value.to_be_bytes());
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::*;

    use super::*;

    fn clean_block() -> [u8; BLOCK_SIZE] {
        let mut block = [0; BLOCK_SIZE];
        block[0..4].copy_from_slice(&JBD2_MAGIC.to_be_bytes());
        block[4..8].copy_from_slice(&JBD2_SUPERBLOCK_V2.to_be_bytes());
        block[JBD2_BLOCK_SIZE_OFFSET..JBD2_BLOCK_SIZE_OFFSET + 4]
            .copy_from_slice(&(BLOCK_SIZE as u32).to_be_bytes());
        block[JBD2_MAX_LENGTH_OFFSET..JBD2_MAX_LENGTH_OFFSET + 4]
            .copy_from_slice(&1024u32.to_be_bytes());
        block[JBD2_FIRST_OFFSET..JBD2_FIRST_OFFSET + 4].copy_from_slice(&1u32.to_be_bytes());
        block[JBD2_SEQUENCE_OFFSET..JBD2_SEQUENCE_OFFSET + 4].copy_from_slice(&1u32.to_be_bytes());
        block
    }

    #[ktest]
    fn parses_clean_journal() {
        let journal = JournalSuperBlock::parse(&clean_block()).unwrap();
        assert!(!journal.needs_recovery());
        assert_eq!(journal.sequence, 1);
        assert_eq!(journal.max_length, 1024);
    }

    #[ktest]
    fn detects_pending_transaction() {
        let mut block = clean_block();
        block[JBD2_START_OFFSET..JBD2_START_OFFSET + 4].copy_from_slice(&2u32.to_be_bytes());
        block[JBD2_SEQUENCE_OFFSET..JBD2_SEQUENCE_OFFSET + 4].copy_from_slice(&7u32.to_be_bytes());
        let journal = JournalSuperBlock::parse(&block).unwrap();
        assert!(journal.needs_recovery());
        assert_eq!(journal.sequence, 7);
    }

    #[ktest]
    fn rejects_bad_geometry() {
        let mut block = clean_block();
        block[JBD2_BLOCK_SIZE_OFFSET..JBD2_BLOCK_SIZE_OFFSET + 4]
            .copy_from_slice(&1024u32.to_be_bytes());
        assert!(JournalSuperBlock::parse(&block).is_err());
    }

    #[ktest]
    fn rejects_truncated_feature_header() {
        assert!(JournalSuperBlock::parse(&[0; JBD2_SUPERBLOCK_HEADER_SIZE]).is_err());
    }

    #[ktest]
    fn rejects_unimplemented_journal_features() {
        let mut block = clean_block();
        block[JBD2_FEATURE_INCOMPAT_OFFSET..JBD2_FEATURE_INCOMPAT_OFFSET + 4]
            .copy_from_slice(&0x2u32.to_be_bytes());
        assert!(JournalSuperBlock::parse(&block).is_err());
    }

    #[ktest]
    fn rejects_recovery_outside_the_journal_ring() {
        let mut block = clean_block();
        block[JBD2_START_OFFSET..JBD2_START_OFFSET + 4].copy_from_slice(&1u32.to_be_bytes());
        block[JBD2_FIRST_OFFSET..JBD2_FIRST_OFFSET + 4].copy_from_slice(&2u32.to_be_bytes());
        block[JBD2_SEQUENCE_OFFSET..JBD2_SEQUENCE_OFFSET + 4].copy_from_slice(&7u32.to_be_bytes());
        assert!(JournalSuperBlock::parse(&block).is_err());
    }

    #[ktest]
    fn encodes_transaction_and_escapes_payload_magic() {
        let mut transaction = JournalTransaction::new(7).unwrap();
        let mut payload = vec![0; BLOCK_SIZE];
        payload[..4].copy_from_slice(&JBD2_MAGIC.to_be_bytes());
        transaction.add_block(31, &payload).unwrap();
        transaction.add_block(32, &[0x5a; BLOCK_SIZE]).unwrap();

        let blocks = transaction.encode().unwrap();
        assert_eq!(blocks.len(), 4);
        assert_eq!(read_be_u32(&blocks[0], 0), JBD2_MAGIC);
        assert_eq!(read_be_u32(&blocks[0], 4), JBD2_DESCRIPTOR_BLOCK);
        assert_eq!(read_be_u32(&blocks[0], 8), 7);
        assert_eq!(read_be_u32(&blocks[0], 12), 31);
        assert_eq!(read_be_u32(&blocks[0], 16), JBD2_FLAG_ESCAPE);
        assert_eq!(read_be_u32(&blocks[0], 20), 32);
        assert_eq!(read_be_u32(&blocks[0], 24), JBD2_FLAG_LAST_TAG);
        assert_eq!(&blocks[1][..4], &[0; 4]);
        assert_eq!(blocks[2], vec![0x5a; BLOCK_SIZE]);
        assert_eq!(read_be_u32(&blocks[3], 4), JBD2_COMMIT_BLOCK);
    }

    #[ktest]
    fn rejects_duplicate_or_oversized_transaction_targets() {
        let mut transaction = JournalTransaction::new(1).unwrap();
        transaction.add_block(4, &[0; BLOCK_SIZE]).unwrap();
        assert!(transaction.add_block(4, &[0; BLOCK_SIZE]).is_err());

        let mut transaction = JournalTransaction::new(2).unwrap();
        for block in 1..=64 {
            transaction.add_block(block, &[0; BLOCK_SIZE]).unwrap();
        }
        assert!(transaction.encode().is_err());
    }

    #[ktest]
    fn reserves_journal_ring_positions_with_wraparound() {
        let mut block = clean_block();
        block[JBD2_MAX_LENGTH_OFFSET..JBD2_MAX_LENGTH_OFFSET + 4]
            .copy_from_slice(&8u32.to_be_bytes());
        block[JBD2_FIRST_OFFSET..JBD2_FIRST_OFFSET + 4].copy_from_slice(&3u32.to_be_bytes());
        block[JBD2_START_OFFSET..JBD2_START_OFFSET + 4].copy_from_slice(&6u32.to_be_bytes());
        let journal = JournalSuperBlock::parse(&block).unwrap();
        let mut ring = JournalRing::new(journal).unwrap();
        assert_eq!(ring.reserve(4).unwrap(), vec![6, 7, 3, 4]);
        assert_eq!(ring.cursor(), 5);
        assert!(ring.reserve(6).is_err());
    }

    fn header(block_type: u32) -> [u8; BLOCK_SIZE] {
        let mut block = [0; BLOCK_SIZE];
        block[0..4].copy_from_slice(&JBD2_MAGIC.to_be_bytes());
        block[4..8].copy_from_slice(&block_type.to_be_bytes());
        block[8..12].copy_from_slice(&7u32.to_be_bytes());
        block
    }

    #[ktest]
    fn parses_descriptor_tags_until_last() {
        let mut block = header(JBD2_DESCRIPTOR_BLOCK);
        block[12..16].copy_from_slice(&31u32.to_be_bytes());
        block[16..20].copy_from_slice(&0u32.to_be_bytes());
        block[20..24].copy_from_slice(&32u32.to_be_bytes());
        block[24..28].copy_from_slice(&JBD2_FLAG_LAST_TAG.to_be_bytes());
        assert_eq!(
            parse_descriptor(&block, 7).unwrap(),
            vec![
                JournalTag {
                    block_number: 31,
                    flags: 0,
                },
                JournalTag {
                    block_number: 32,
                    flags: JBD2_FLAG_LAST_TAG,
                },
            ]
        );
    }

    #[ktest]
    fn validates_commit_and_revoke_blocks() {
        let commit = header(JBD2_COMMIT_BLOCK);
        validate_commit(&commit, 7).unwrap();

        let mut revoke = header(JBD2_REVOKE_BLOCK);
        revoke[12..16].copy_from_slice(&24u32.to_be_bytes());
        revoke[16..20].copy_from_slice(&41u32.to_be_bytes());
        revoke[20..24].copy_from_slice(&42u32.to_be_bytes());
        assert_eq!(parse_revoke(&revoke, 7).unwrap(), vec![41, 42]);
    }
}
