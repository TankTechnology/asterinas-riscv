// SPDX-License-Identifier: MPL-2.0

//! JBD2 journal block decoding used by the ext4 compatibility filesystem.
//!
//! This module deliberately contains no I/O or recovery policy.  Keeping the
//! on-disk validation separate lets the mount and replay code share the same
//! strict checks when it is migrated from the Debian branch.

use super::prelude::*;

const MAGIC: u32 = 0xc03b3998;
const SUPER_V2: u32 = 4;
const DESCRIPTOR: u32 = 1;
const COMMIT: u32 = 2;
const REVOKE: u32 = 5;
const HEADER_SIZE: usize = 12;
const SUPERBLOCK_SIZE: usize = 1024;
const TAG_SIZE: usize = 8;
const FLAG_ESCAPE: u32 = 1;
const FLAG_SAME_UUID: u32 = 2;
const FLAG_DELETED: u32 = 4;
const FLAG_LAST_TAG: u32 = 8;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct SuperBlock {
    pub(super) block_size: u32,
    pub(super) max_length: u32,
    pub(super) first: u32,
    pub(super) sequence: u32,
    pub(super) start: u32,
}

impl SuperBlock {
    pub(super) fn parse(block: &[u8]) -> Result<Self> {
        if block.len() < SUPERBLOCK_SIZE {
            return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal superblock");
        }
        if be32(block, 0) != MAGIC {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal magic");
        }
        if be32(block, 4) != SUPER_V2 {
            return_errno_with_message!(Errno::EOPNOTSUPP, "unsupported ext4 journal version");
        }
        let block_size = be32(block, 12);
        let max_length = be32(block, 16);
        let first = be32(block, 20);
        let sequence = be32(block, 24);
        let start = be32(block, 28);
        if block_size as usize != BLOCK_SIZE || first == 0 || first >= max_length {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal geometry");
        }
        if start != 0 && (start < first || start >= max_length) {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal start");
        }
        // Only the legacy 32-bit tag layout is decoded below. In particular,
        // never interpret checksummed or 64-bit tags as legacy tags.
        if be32(block, 36) != 0 || be32(block, 40) & !1 != 0 || be32(block, 44) != 0 {
            return_errno_with_message!(Errno::EOPNOTSUPP, "unsupported ext4 journal features");
        }
        Ok(Self {
            block_size,
            max_length,
            first,
            sequence,
            start,
        })
    }

    pub(super) const fn needs_recovery(self) -> bool {
        self.start != 0
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct Header {
    pub(super) block_type: u32,
    pub(super) sequence: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct Tag {
    pub(super) block_number: u32,
    pub(super) flags: u32,
}

/// A validated data block from one committed journal transaction.
///
/// The decoder intentionally returns owned data.  Recovery can therefore
/// validate an entire transaction before issuing any writes to the target
/// device, which prevents a malformed tail from producing a partial replay.
#[derive(Debug, Eq, PartialEq)]
pub(super) struct ReplayBlock {
    pub(super) block_number: u32,
    pub(super) data: Vec<u8>,
    pub(super) escaped: bool,
}

/// Decode one descriptor/data/(optional revoke)/commit transaction.
///
/// `blocks` contains journal blocks in on-disk order, beginning with the
/// descriptor.  A transaction is considered replayable only after its commit
/// block has been validated; callers may then write the returned blocks to the
/// filesystem, after a recovery-wide revoke pass. This helper only applies
/// revocations in the supplied transaction; it is not a full journal replay
/// engine. Multiple descriptors and revoke-only transactions are not supported
/// here. No device I/O happens here.
pub(super) fn decode_transaction(blocks: &[&[u8]], sequence: u32) -> Result<Vec<ReplayBlock>> {
    if blocks.len() < 3 {
        return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal transaction");
    }
    let tags = parse_descriptor(blocks[0], sequence)?;
    let data_end = 1 + tags.len();
    if blocks.len() <= data_end {
        return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal transaction data");
    }

    let mut revoked = Vec::new();
    let mut commit_index = data_end;
    if blocks[commit_index].len() >= HEADER_SIZE && be32(blocks[commit_index], 4) == REVOKE {
        revoked = parse_revoke(blocks[commit_index], sequence)?;
        commit_index += 1;
    }
    if blocks.len() != commit_index + 1 {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal transaction length");
    }
    validate_commit(blocks[commit_index], sequence)?;

    let mut replay = Vec::with_capacity(tags.len());
    for (index, tag) in tags.iter().enumerate() {
        if revoked.contains(&tag.block_number) {
            continue;
        }
        let mut data = blocks[index + 1].to_vec();
        if data.len() != BLOCK_SIZE {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal data block");
        }
        if tag.flags & FLAG_ESCAPE != 0 {
            if data[..4] != [0; 4] {
                return_errno_with_message!(Errno::EUCLEAN, "invalid escaped journal block");
            }
            data[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        }
        replay.push(ReplayBlock {
            block_number: tag.block_number,
            data,
            escaped: tag.flags & FLAG_ESCAPE != 0,
        });
    }
    Ok(replay)
}

/// Scan a linearized journal log and produce the final replay set.
///
/// The caller supplies blocks in journal order, beginning at `s_first` (with
/// wrap-around already normalized by the caller).  A transaction without a
/// commit record is treated as the crash tail and discarded.  Revoke records
/// remove older replay entries, while a later write to the same target block
/// replaces the older entry.  This gives the recovery writer a deterministic,
/// last-writer-wins set without performing device I/O.
pub(super) fn scan_log(blocks: &[&[u8]], first_sequence: u32) -> Result<Vec<ReplayBlock>> {
    let mut replay: Vec<ReplayBlock> = Vec::new();
    let mut offset = 0;
    let mut sequence = first_sequence;
    while offset < blocks.len() {
        if blocks[offset].len() < HEADER_SIZE {
            break;
        }
        let header = parse_header(blocks[offset])?;
        match header.block_type {
            DESCRIPTOR => {
                if header.sequence != sequence {
                    return_errno_with_message!(
                        Errno::EUCLEAN,
                        "non-contiguous ext4 journal sequence"
                    );
                }
                let tags = parse_descriptor(blocks[offset], sequence)?;
                let data_start = offset + 1;
                let data_end = data_start + tags.len();
                if data_end > blocks.len() {
                    break;
                }
                let mut end = data_end;
                if end < blocks.len()
                    && blocks[end].len() >= HEADER_SIZE
                    && be32(blocks[end], 4) == REVOKE
                {
                    end += 1;
                }
                if end >= blocks.len() {
                    break;
                }
                if parse_header(blocks[end])?.block_type != COMMIT {
                    return_errno_with_message!(
                        Errno::EUCLEAN,
                        "journal transaction missing commit"
                    );
                }
                let transaction = decode_transaction(&blocks[offset..=end], sequence)?;
                for block in transaction {
                    replay.retain(|old| old.block_number != block.block_number);
                    replay.push(block);
                }
                offset = end + 1;
                sequence = sequence.wrapping_add(1);
            }
            REVOKE => {
                let revoke = parse_revoke(blocks[offset], sequence)?;
                let commit = offset + 1;
                if commit >= blocks.len() {
                    break;
                }
                validate_commit(blocks[commit], sequence)?;
                replay.retain(|block| !revoke.contains(&block.block_number));
                offset = commit + 1;
                sequence = sequence.wrapping_add(1);
            }
            _ => return_errno_with_message!(Errno::EUCLEAN, "unexpected ext4 journal block"),
        }
    }
    Ok(replay)
}

pub(super) fn parse_header(block: &[u8]) -> Result<Header> {
    if block.len() < HEADER_SIZE || be32(block, 0) != MAGIC {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal block header");
    }
    Ok(Header {
        block_type: be32(block, 4),
        sequence: be32(block, 8),
    })
}

pub(super) fn parse_descriptor(block: &[u8], sequence: u32) -> Result<Vec<Tag>> {
    let header = parse_header(block)?;
    if header.block_type != DESCRIPTOR || header.sequence != sequence {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal descriptor");
    }
    let mut tags = Vec::new();
    let mut offset = HEADER_SIZE;
    loop {
        if offset.checked_add(TAG_SIZE).is_none() || offset + TAG_SIZE > block.len() {
            return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal descriptor tag");
        }
        let tag = Tag {
            block_number: be32(block, offset),
            flags: be32(block, offset + 4),
        };
        let known = FLAG_ESCAPE | FLAG_SAME_UUID | FLAG_DELETED | FLAG_LAST_TAG;
        if tag.flags & !known != 0 || tag.block_number == 0 {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal descriptor tag");
        }
        if tag.flags & FLAG_DELETED != 0 {
            return_errno_with_message!(Errno::EOPNOTSUPP, "deleted journal tags are unsupported");
        }
        tags.push(tag);
        offset += TAG_SIZE;
        if tag.flags & FLAG_SAME_UUID == 0 {
            if block.len() - offset < 16 {
                return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal tag UUID");
            }
            offset += 16;
        }
        if tag.flags & FLAG_LAST_TAG != 0 {
            return Ok(tags);
        }
    }
}

pub(super) fn validate_commit(block: &[u8], sequence: u32) -> Result<()> {
    let header = parse_header(block)?;
    if header.block_type != COMMIT || header.sequence != sequence {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal commit");
    }
    Ok(())
}

pub(super) fn parse_revoke(block: &[u8], sequence: u32) -> Result<Vec<u32>> {
    let header = parse_header(block)?;
    if header.block_type != REVOKE || header.sequence != sequence || block.len() < 16 {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal revoke");
    }
    let bytes = be32(block, 12) as usize;
    if bytes < 16 || bytes > block.len() || (bytes - 16) % 4 != 0 {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal revoke length");
    }
    let mut revoked = Vec::with_capacity((bytes - 16) / 4);
    for offset in (16..bytes).step_by(4) {
        let block_number = be32(block, offset);
        if block_number == 0 {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 revoked block");
        }
        revoked.push(block_number);
    }
    Ok(revoked)
}

#[inline]
fn be32(block: &[u8], offset: usize) -> u32 {
    u32::from_be_bytes(block[offset..offset + 4].try_into().unwrap())
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::ktest;

    use super::*;

    fn superblock() -> [u8; BLOCK_SIZE] {
        let mut block = [0; BLOCK_SIZE];
        block[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        block[4..8].copy_from_slice(&SUPER_V2.to_be_bytes());
        block[12..16].copy_from_slice(&(BLOCK_SIZE as u32).to_be_bytes());
        block[16..20].copy_from_slice(&1024u32.to_be_bytes());
        block[20..24].copy_from_slice(&1u32.to_be_bytes());
        block
    }

    #[ktest]
    fn accepts_clean_superblock() {
        let journal = SuperBlock::parse(&superblock()).unwrap();
        assert_eq!(journal.max_length, 1024);
        assert!(!journal.needs_recovery());
    }

    #[ktest]
    fn rejects_truncated_superblock() {
        assert!(SuperBlock::parse(&[0; 24]).is_err());
    }

    #[ktest]
    fn distinguishes_sequence_from_log_start() {
        let mut block = superblock();
        block[24..28].copy_from_slice(&73u32.to_be_bytes());
        let journal = SuperBlock::parse(&block).unwrap();
        assert_eq!(journal.sequence, 73);
        assert!(!journal.needs_recovery());
        block[28..32].copy_from_slice(&9u32.to_be_bytes());
        assert!(SuperBlock::parse(&block).unwrap().needs_recovery());
        block[40..44].copy_from_slice(&2u32.to_be_bytes());
        assert!(SuperBlock::parse(&block).is_err());
    }

    #[ktest]
    fn descriptor_skips_uuid_and_rejects_truncated_uuid() {
        let mut descriptor = [0; 52];
        descriptor[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        descriptor[4..8].copy_from_slice(&DESCRIPTOR.to_be_bytes());
        descriptor[8..12].copy_from_slice(&7u32.to_be_bytes());
        descriptor[12..16].copy_from_slice(&31u32.to_be_bytes());
        descriptor[20..36].fill(0xaa);
        descriptor[36..40].copy_from_slice(&32u32.to_be_bytes());
        descriptor[40..44].copy_from_slice(&(FLAG_SAME_UUID | FLAG_LAST_TAG).to_be_bytes());
        let tags = parse_descriptor(&descriptor, 7).unwrap();
        assert_eq!(tags.len(), 2);
        assert_eq!(tags[1].block_number, 32);
        assert!(parse_descriptor(&descriptor[..35], 7).is_err());
    }

    #[ktest]
    fn validates_descriptor_commit_and_revoke() {
        let mut descriptor = [0; BLOCK_SIZE];
        descriptor[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        descriptor[4..8].copy_from_slice(&DESCRIPTOR.to_be_bytes());
        descriptor[8..12].copy_from_slice(&7u32.to_be_bytes());
        descriptor[12..16].copy_from_slice(&31u32.to_be_bytes());
        descriptor[16..20].copy_from_slice(&FLAG_LAST_TAG.to_be_bytes());
        assert_eq!(
            parse_descriptor(&descriptor, 7).unwrap()[0].block_number,
            31
        );

        let mut commit = [0; BLOCK_SIZE];
        commit[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        commit[4..8].copy_from_slice(&COMMIT.to_be_bytes());
        commit[8..12].copy_from_slice(&7u32.to_be_bytes());
        validate_commit(&commit, 7).unwrap();

        let mut revoke = commit;
        revoke[4..8].copy_from_slice(&REVOKE.to_be_bytes());
        revoke[12..16].copy_from_slice(&20u32.to_be_bytes());
        revoke[16..20].copy_from_slice(&99u32.to_be_bytes());
        assert_eq!(parse_revoke(&revoke, 7).unwrap(), vec![99]);
    }

    #[ktest]
    fn decodes_committed_transaction_and_skips_revoked_blocks() {
        let mut descriptor = [0; BLOCK_SIZE];
        descriptor[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        descriptor[4..8].copy_from_slice(&DESCRIPTOR.to_be_bytes());
        descriptor[8..12].copy_from_slice(&7u32.to_be_bytes());
        descriptor[12..16].copy_from_slice(&31u32.to_be_bytes());
        descriptor[16..20].copy_from_slice(&(FLAG_ESCAPE | FLAG_SAME_UUID).to_be_bytes());
        descriptor[20..24].copy_from_slice(&32u32.to_be_bytes());
        descriptor[24..28].copy_from_slice(&(FLAG_LAST_TAG | FLAG_SAME_UUID).to_be_bytes());

        let mut data_a = [0x11; BLOCK_SIZE];
        data_a[0..4].copy_from_slice(&0u32.to_be_bytes());
        let data_b = [0x22; BLOCK_SIZE];

        let mut revoke = [0; BLOCK_SIZE];
        revoke[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        revoke[4..8].copy_from_slice(&REVOKE.to_be_bytes());
        revoke[8..12].copy_from_slice(&7u32.to_be_bytes());
        revoke[12..16].copy_from_slice(&20u32.to_be_bytes());
        revoke[16..20].copy_from_slice(&32u32.to_be_bytes());

        let mut commit = [0; BLOCK_SIZE];
        commit[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        commit[4..8].copy_from_slice(&COMMIT.to_be_bytes());
        commit[8..12].copy_from_slice(&7u32.to_be_bytes());

        let replay =
            decode_transaction(&[&descriptor, &data_a, &data_b, &revoke, &commit], 7).unwrap();
        assert_eq!(replay.len(), 1);
        assert_eq!(replay[0].block_number, 31);
        assert!(replay[0].escaped);
        assert_eq!(&replay[0].data[..4], &MAGIC.to_be_bytes());
        assert!(decode_transaction(&[&descriptor, &data_a, &data_b, &revoke], 7).is_err());
        commit[8..12].copy_from_slice(&8u32.to_be_bytes());
        assert!(decode_transaction(&[&descriptor, &data_a, &data_b, &revoke, &commit], 7).is_err());
    }

    #[ktest]
    fn scans_transactions_with_cross_transaction_revoke_and_tail() {
        let mut descriptor = [0; BLOCK_SIZE];
        descriptor[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        descriptor[4..8].copy_from_slice(&DESCRIPTOR.to_be_bytes());
        descriptor[8..12].copy_from_slice(&7u32.to_be_bytes());
        descriptor[12..16].copy_from_slice(&31u32.to_be_bytes());
        descriptor[16..20].copy_from_slice(&(FLAG_LAST_TAG | FLAG_SAME_UUID).to_be_bytes());
        let data = [0x33; BLOCK_SIZE];
        let mut commit = [0; BLOCK_SIZE];
        commit[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        commit[4..8].copy_from_slice(&COMMIT.to_be_bytes());
        commit[8..12].copy_from_slice(&7u32.to_be_bytes());

        let mut revoke = [0; BLOCK_SIZE];
        revoke[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        revoke[4..8].copy_from_slice(&REVOKE.to_be_bytes());
        revoke[8..12].copy_from_slice(&8u32.to_be_bytes());
        revoke[12..16].copy_from_slice(&20u32.to_be_bytes());
        revoke[16..20].copy_from_slice(&31u32.to_be_bytes());
        let mut commit_revoke = commit;
        commit_revoke[8..12].copy_from_slice(&8u32.to_be_bytes());
        let tail = [0; 8];

        let replay = scan_log(
            &[&descriptor, &data, &commit, &revoke, &commit_revoke, &tail],
            7,
        )
        .unwrap();
        assert!(replay.is_empty());
    }
}
