// SPDX-License-Identifier: MPL-2.0

//! JBD2 journal block decoding used by the ext4 compatibility filesystem.
//!
//! This module deliberately contains no I/O or recovery policy.  Keeping the
//! on-disk validation separate lets the mount and replay code share the same
//! strict checks when it is migrated from the Debian branch.

use super::{checksum, prelude::*};

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
/// `JBD2_FEATURE_INCOMPAT_REVOKE`: the journal may carry revoke records.
const INCOMPAT_REVOKE: u32 = 0x1;
/// `JBD2_FEATURE_INCOMPAT_CSUM_V3`: checksummed descriptor/commit/data blocks.
const INCOMPAT_CSUM_V3: u32 = 0x10;
/// `JBD2_CRC32C_CHKSUM`, the only journal checksum type.
const CRC32C_CHKSUM: u8 = 4;
/// `struct jbd2_journal_block_tail`: a trailing checksum on descriptor and
/// revoke blocks.
const BLOCK_TAIL_SIZE: usize = 4;
/// Size of a checksum v3 tag (`journal_block_tag3_t`).
const TAG3_SIZE: usize = 16;
/// Offset of the journal superblock checksum.
const SUPERBLOCK_CHECKSUM_OFFSET: usize = 0xFC;
/// Offset of the journal superblock checksum type.
const SUPERBLOCK_CHECKSUM_TYPE_OFFSET: usize = 80;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct SuperBlock {
    pub(super) block_size: u32,
    pub(super) max_length: u32,
    pub(super) first: u32,
    pub(super) sequence: u32,
    pub(super) start: u32,
    /// Whether descriptor/commit/data blocks carry checksum v3.
    pub(super) csum_v3: bool,
    /// The journal UUID, used by the v3 checksum seed.
    pub(super) uuid: [u8; 16],
    /// Checksum seed derived from the UUID.
    pub(super) csum_seed: u32,
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
        // Only the 32-bit block numbers of the legacy and csum-v3 tag layouts
        // are decoded; 64-bit journals and the older csum-v2 format are
        // rejected instead of being misread.
        let feature_compat = be32(block, 36);
        let feature_incompat = be32(block, 40);
        let feature_ro_compat = be32(block, 44);
        let allowed_incompat = INCOMPAT_REVOKE | INCOMPAT_CSUM_V3;
        if feature_compat != 0
            || feature_incompat & !allowed_incompat != 0
            || feature_ro_compat != 0
        {
            return_errno_with_message!(Errno::EOPNOTSUPP, "unsupported ext4 journal features");
        }
        let csum_v3 = feature_incompat & INCOMPAT_CSUM_V3 != 0;
        let mut uuid = [0u8; 16];
        uuid.copy_from_slice(&block[48..64]);
        let csum_seed = checksum::seed_from_uuid(&uuid);
        if csum_v3 && !verify_superblock_checksum(block) {
            return_errno_with_message!(Errno::EUCLEAN, "invalid journal superblock checksum");
        }
        Ok(Self {
            block_size,
            max_length,
            first,
            sequence,
            start,
            csum_v3,
            uuid,
            csum_seed,
        })
    }

    pub(super) const fn needs_recovery(self) -> bool {
        self.start != 0
    }

    pub(super) fn with_start(self, start: u32) -> Result<Self> {
        if start != 0 && (start < self.first || start >= self.max_length) {
            return_errno_with_message!(Errno::EINVAL, "invalid ext4 journal start");
        }
        Ok(Self { start, ..self })
    }

    pub(super) fn with_sequence(self, sequence: u32) -> Result<Self> {
        if sequence == 0 {
            return_errno_with_message!(Errno::EINVAL, "zero ext4 journal sequence");
        }
        Ok(Self { sequence, ..self })
    }

    pub(super) fn write_start(block: &mut [u8], start: u32) -> Result<()> {
        let super_block = Self::parse(block)?.with_start(start)?;
        block[28..32].copy_from_slice(&super_block.start.to_be_bytes());
        if super_block.csum_v3 {
            seal_superblock(block);
        }
        Ok(())
    }

    pub(super) fn write_sequence(block: &mut [u8], sequence: u32) -> Result<()> {
        let super_block = Self::parse(block)?.with_sequence(sequence)?;
        block[24..28].copy_from_slice(&super_block.sequence.to_be_bytes());
        if super_block.csum_v3 {
            seal_superblock(block);
        }
        Ok(())
    }

    /// Returns the size of one descriptor tag on disk.
    pub(super) const fn tag_size(&self) -> usize {
        if self.csum_v3 { TAG3_SIZE } else { TAG_SIZE }
    }

    /// Returns the space reserved at the end of descriptor and revoke blocks.
    pub(super) const fn block_tail_size(&self) -> usize {
        if self.csum_v3 { BLOCK_TAIL_SIZE } else { 0 }
    }
}

/// Verifies the CRC32C covering a checksummed journal superblock.
fn verify_superblock_checksum(block: &[u8]) -> bool {
    if block.len() < SUPERBLOCK_SIZE || block[SUPERBLOCK_CHECKSUM_TYPE_OFFSET] != CRC32C_CHKSUM {
        return false;
    }
    let stored = be32(block, SUPERBLOCK_CHECKSUM_OFFSET);
    let mut copy = [0u8; SUPERBLOCK_SIZE];
    copy.copy_from_slice(&block[..SUPERBLOCK_SIZE]);
    copy[SUPERBLOCK_CHECKSUM_OFFSET..SUPERBLOCK_CHECKSUM_OFFSET + 4].fill(0);
    checksum::crc32c(checksum::CRC32C_INIT, &copy) == stored
}

/// Recomputes and stores the journal superblock checksum.
fn seal_superblock(block: &mut [u8]) {
    block[SUPERBLOCK_CHECKSUM_TYPE_OFFSET] = CRC32C_CHKSUM;
    block[SUPERBLOCK_CHECKSUM_TYPE_OFFSET + 1..SUPERBLOCK_CHECKSUM_TYPE_OFFSET + 4].fill(0);
    block[SUPERBLOCK_CHECKSUM_OFFSET..SUPERBLOCK_CHECKSUM_OFFSET + 4].fill(0);
    let crc = checksum::crc32c(checksum::CRC32C_INIT, &block[..SUPERBLOCK_SIZE]);
    block[SUPERBLOCK_CHECKSUM_OFFSET..SUPERBLOCK_CHECKSUM_OFFSET + 4]
        .copy_from_slice(&crc.to_be_bytes());
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
    /// Checksum stored in the tag (v3 journals only; zero otherwise).
    pub(super) checksum: u32,
}

/// In-memory encoder for the legacy, unchecksummed JBD2 transaction format.
///
/// The filesystem layer owns ring placement and durable write ordering. This
/// type only validates targets and emits descriptor, payload, and commit
/// blocks so those concerns can be migrated independently.
#[derive(Debug)]
pub(super) struct JournalTransaction {
    sequence: u32,
    blocks: Vec<(u32, Vec<u8>)>,
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
        // Block zero is the boot block, and with 4 KiB blocks it also holds the
        // primary superblock, which ext4 journals like any other metadata.
        if payload.len() != BLOCK_SIZE {
            return_errno_with_message!(Errno::EINVAL, "invalid ext4 journal target");
        }
        if self.blocks.iter().any(|(block, _)| *block == block_number) {
            return_errno_with_message!(Errno::EEXIST, "duplicate ext4 journal target block");
        }
        self.blocks.push((block_number, payload.to_vec()));
        Ok(())
    }

    /// Number of tags a single descriptor block can hold.
    ///
    /// The first tag is followed by the 16-byte journal UUID; checksum-v3
    /// descriptors also reserve a 4-byte tail. Neither can be used for tags.
    fn descriptor_capacity(journal: &SuperBlock) -> usize {
        let tag_size = journal.tag_size();
        let tail_size = journal.block_tail_size();
        (BLOCK_SIZE - HEADER_SIZE - 16 - tail_size) / tag_size
    }

    /// Encodes one descriptor block for at most `descriptor_capacity` blocks.
    ///
    /// The first tag of *each* descriptor carries the UUID, and each
    /// descriptor's last tag is marked `FLAG_LAST_TAG`; that is how jbd2
    /// delimits the tag list of a descriptor that is followed by another
    /// descriptor rather than by the commit record.
    fn encode_descriptor(
        blocks: &[(u32, Vec<u8>)],
        sequence: u32,
        journal: &SuperBlock,
    ) -> Vec<u8> {
        let tag_size = journal.tag_size();
        let sequence_bytes = sequence.to_be_bytes();
        let mut descriptor = vec![0; BLOCK_SIZE];
        put_be32(&mut descriptor, 0, MAGIC);
        put_be32(&mut descriptor, 4, DESCRIPTOR);
        put_be32(&mut descriptor, 8, sequence);
        let mut offset = HEADER_SIZE;
        for (index, (block, payload)) in blocks.iter().enumerate() {
            let mut flags = if index == 0 { 0 } else { FLAG_SAME_UUID };
            if index + 1 == blocks.len() {
                flags |= FLAG_LAST_TAG;
            }
            if be32(payload, 0) == MAGIC {
                flags |= FLAG_ESCAPE;
            }
            put_be32(&mut descriptor, offset, *block);
            if journal.csum_v3 {
                put_be32(&mut descriptor, offset + 4, flags);
                put_be32(&mut descriptor, offset + 8, 0);
                // jbd2 computes the tag checksum after the escape word (if
                // any) has been zeroed in the logged copy.
                let mut logged = payload.clone();
                if flags & FLAG_ESCAPE != 0 {
                    logged[..4].fill(0);
                }
                let crc = checksum::crc32c(journal.csum_seed, &sequence_bytes);
                let crc = checksum::crc32c(crc, &logged);
                put_be32(&mut descriptor, offset + 12, crc);
            } else {
                put_be32(&mut descriptor, offset + 4, flags);
            }
            offset += tag_size;
            if index == 0 {
                descriptor[offset..offset + 16].copy_from_slice(&journal.uuid);
                offset += 16;
            }
        }
        if journal.csum_v3 {
            // The tail is still zero here, and jbd2 hashes the whole block
            // with the checksum field zeroed.
            let tail = BLOCK_SIZE - BLOCK_TAIL_SIZE;
            let crc = checksum::crc32c(journal.csum_seed, &descriptor);
            put_be32(&mut descriptor, tail, crc);
        }
        descriptor
    }

    pub(super) fn encode(self, journal: &SuperBlock) -> Result<Vec<Vec<u8>>> {
        if self.blocks.is_empty() {
            return_errno_with_message!(Errno::EINVAL, "empty ext4 journal transaction");
        }
        let capacity = Self::descriptor_capacity(journal);
        if capacity == 0 {
            return_errno_with_message!(Errno::EINVAL, "ext4 journal descriptor has no tag space");
        }
        let sequence = self.sequence;
        let mut encoded = Vec::new();
        // A JBD2 transaction may span several descriptors. The commit record
        // stays unique and last, so an interrupted sequence is still discarded
        // by replay and atomicity is unchanged.
        for chunk in self.blocks.chunks(capacity) {
            encoded.push(Self::encode_descriptor(chunk, sequence, journal));
            for (_, payload) in chunk {
                let mut data = payload.clone();
                if be32(&data, 0) == MAGIC {
                    data[..4].fill(0);
                }
                encoded.push(data);
            }
        }
        let mut commit = vec![0; BLOCK_SIZE];
        put_be32(&mut commit, 0, MAGIC);
        put_be32(&mut commit, 4, COMMIT);
        put_be32(&mut commit, 8, sequence);
        if journal.csum_v3 {
            let crc = checksum::crc32c(journal.csum_seed, &commit);
            put_be32(&mut commit, 16, crc);
        }
        encoded.push(commit);
        Ok(encoded)
    }

    pub(super) const fn sequence(&self) -> u32 {
        self.sequence
    }
}

/// Validated placement cursor for the circular journal data area.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct JournalRing {
    first: u32,
    max_length: u32,
    next: u32,
}

impl JournalRing {
    pub(super) fn new(super_block: SuperBlock) -> Result<Self> {
        let next = if super_block.start == 0 {
            super_block.first
        } else {
            super_block.start
        };
        if next < super_block.first || next >= super_block.max_length {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal ring cursor");
        }
        Ok(Self {
            first: super_block.first,
            max_length: super_block.max_length,
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

/// Decode a sequence of descriptor/data groups plus an optional revoke and one
/// trailing commit block.
///
/// `blocks` contains journal blocks in on-disk order, beginning with the first
/// descriptor. A transaction is considered replayable only after its single
/// commit block has been validated; callers may then write the returned blocks
/// to the filesystem, after a recovery-wide revoke pass. This helper only
/// applies revocations in the supplied transaction; it is not a full journal
/// replay engine. A transaction that carries only revokes has no descriptor to
/// decode (see [`scan_log`], which handles that form). No device I/O happens
/// here.
pub(super) fn decode_transaction(
    blocks: &[&[u8]],
    sequence: u32,
    journal: &SuperBlock,
) -> Result<Vec<ReplayBlock>> {
    if blocks.len() < 3 {
        return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal transaction");
    }
    let mut replay = Vec::new();
    let mut cursor = 0;
    while cursor < blocks.len() && header_type(blocks[cursor]) == Some(DESCRIPTOR) {
        let tags = parse_descriptor(blocks[cursor], sequence, journal)?;
        let data_start = cursor + 1;
        let data_end = data_start + tags.len();
        if data_end > blocks.len() {
            return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal transaction data");
        }
        for (index, tag) in tags.iter().enumerate() {
            let mut data = blocks[data_start + index].to_vec();
            if data.len() != BLOCK_SIZE {
                return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal data block");
            }
            if tag.flags & FLAG_ESCAPE != 0 {
                if data[..4] != [0; 4] {
                    return_errno_with_message!(Errno::EUCLEAN, "invalid escaped journal block");
                }
                data[0..4].copy_from_slice(&MAGIC.to_be_bytes());
            }
            if journal.csum_v3 {
                // The checksum covers the logged copy, i.e. before the escape
                // word is restored.
                let crc = checksum::crc32c(journal.csum_seed, &sequence.to_be_bytes());
                let crc = checksum::crc32c(crc, blocks[data_start + index]);
                if crc != tag.checksum {
                    return_errno_with_message!(Errno::EUCLEAN, "invalid journal data checksum");
                }
            }
            replay.push(ReplayBlock {
                block_number: tag.block_number,
                data,
                escaped: tag.flags & FLAG_ESCAPE != 0,
            });
        }
        cursor = data_end;
    }
    if cursor == 0 {
        return_errno_with_message!(Errno::EUCLEAN, "ext4 journal transaction has no descriptor");
    }

    let mut revoked = Vec::new();
    if cursor < blocks.len() && header_type(blocks[cursor]) == Some(REVOKE) {
        revoked = parse_revoke(blocks[cursor], sequence, journal)?;
        cursor += 1;
    }
    if blocks.len() != cursor + 1 {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal transaction length");
    }
    validate_commit(blocks[cursor], sequence, journal)?;
    if !revoked.is_empty() {
        replay.retain(|block| !revoked.contains(&block.block_number));
    }
    Ok(replay)
}

/// Returns the JBD2 block type when `block` carries a valid header.
fn header_type(block: &[u8]) -> Option<u32> {
    if block.len() < HEADER_SIZE || be32(block, 0) != MAGIC {
        return None;
    }
    Some(be32(block, 4))
}

/// Scan a linearized journal log and produce the final replay set.
///
/// The caller supplies blocks in journal order, beginning at `s_first` (with
/// wrap-around already normalized by the caller).  A transaction without a
/// commit record is treated as the crash tail and discarded.  Revoke records
/// remove older replay entries, while a later write to the same target block
/// replaces the older entry.  This gives the recovery writer a deterministic,
/// last-writer-wins set without performing device I/O.
pub(super) fn scan_log(
    blocks: &[&[u8]],
    first_sequence: u32,
    journal: &SuperBlock,
) -> Result<Vec<ReplayBlock>> {
    let mut replay: Vec<ReplayBlock> = Vec::new();
    let mut offset = 0;
    let mut sequence = first_sequence;
    while offset < blocks.len() {
        if blocks[offset].len() < HEADER_SIZE {
            break;
        }
        // A block without the JBD2 magic ends the valid log: the ring is
        // larger than the transactions in it, and the tail is not initialised.
        // This is how jbd2 treats it ("no magic number at block N: end of
        // journal"). Only at the very start is a bad header corruption.
        let header = match parse_header(blocks[offset]) {
            Ok(header) => header,
            Err(_) if offset > 0 => break,
            Err(err) => return Err(err),
        };
        match header.block_type {
            DESCRIPTOR => {
                if header.sequence != sequence {
                    return_errno_with_message!(
                        Errno::EUCLEAN,
                        "non-contiguous ext4 journal sequence"
                    );
                }
                // One transaction may span several descriptor blocks; each is
                // followed by its own data blocks. Consume consecutive
                // descriptors until a revoke or the final commit record.
                let transaction_start = offset;
                let mut cursor = offset;
                loop {
                    let tags = parse_descriptor(blocks[cursor], sequence, journal)?;
                    let data_end = cursor + 1 + tags.len();
                    if data_end > blocks.len() {
                        // Incomplete transaction at the tail: discard it.
                        cursor = blocks.len();
                        break;
                    }
                    cursor = data_end;
                    if cursor < blocks.len() && header_type(blocks[cursor]) == Some(DESCRIPTOR) {
                        continue;
                    }
                    break;
                }
                if cursor >= blocks.len() {
                    break;
                }
                if header_type(blocks[cursor]) == Some(REVOKE) {
                    cursor += 1;
                    if cursor >= blocks.len() {
                        break;
                    }
                }
                if header_type(blocks[cursor]) != Some(COMMIT) {
                    return_errno_with_message!(
                        Errno::EUCLEAN,
                        "journal transaction missing commit"
                    );
                }
                let end = cursor;
                let transaction =
                    decode_transaction(&blocks[transaction_start..=end], sequence, journal)?;
                for block in transaction {
                    replay.retain(|old| old.block_number != block.block_number);
                    replay.push(block);
                }
                offset = end + 1;
                sequence = sequence.wrapping_add(1);
            }
            REVOKE => {
                let revoke = parse_revoke(blocks[offset], sequence, journal)?;
                let commit = offset + 1;
                if commit >= blocks.len() {
                    break;
                }
                validate_commit(blocks[commit], sequence, journal)?;
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

pub(super) fn parse_descriptor(
    block: &[u8],
    sequence: u32,
    journal: &SuperBlock,
) -> Result<Vec<Tag>> {
    let header = parse_header(block)?;
    if header.block_type != DESCRIPTOR || header.sequence != sequence {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal descriptor");
    }
    if !verify_block_tail(block, journal) {
        return_errno_with_message!(Errno::EUCLEAN, "invalid journal descriptor checksum");
    }
    let tag_size = journal.tag_size();
    let mut tags = Vec::new();
    let mut offset = HEADER_SIZE;
    loop {
        if offset.checked_add(tag_size).is_none() || offset + tag_size > block.len() {
            return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal descriptor tag");
        }
        let tag = if journal.csum_v3 {
            let high = be32(block, offset + 8);
            if high != 0 {
                return_errno_with_message!(
                    Errno::EOPNOTSUPP,
                    "64-bit journal block numbers are unsupported"
                );
            }
            Tag {
                block_number: be32(block, offset),
                flags: be32(block, offset + 4),
                checksum: be32(block, offset + 12),
            }
        } else {
            Tag {
                block_number: be32(block, offset),
                flags: u16::from_be_bytes([block[offset + 6], block[offset + 7]]) as u32,
                checksum: 0,
            }
        };
        let known = FLAG_ESCAPE | FLAG_SAME_UUID | FLAG_DELETED | FLAG_LAST_TAG;
        if tag.flags & !known != 0 {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal descriptor tag");
        }
        // `FLAG_DELETED` is a legacy marker; jbd2 recovery consumes the tag's
        // data block like any other tag, so it needs no special handling.
        tags.push(tag);
        offset += tag_size;
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

/// Verifies the trailing CRC32C of a descriptor or revoke block.
fn verify_block_tail(block: &[u8], journal: &SuperBlock) -> bool {
    if !journal.csum_v3 {
        return true;
    }
    if block.len() != BLOCK_SIZE {
        return false;
    }
    let tail = BLOCK_SIZE - BLOCK_TAIL_SIZE;
    let stored = be32(block, tail);
    let mut copy = [0u8; BLOCK_SIZE];
    copy.copy_from_slice(block);
    copy[tail..].fill(0);
    checksum::crc32c(journal.csum_seed, &copy) == stored
}

pub(super) fn validate_commit(
    block: &[u8],
    sequence: u32,
    journal: &SuperBlock,
) -> Result<()> {
    let header = parse_header(block)?;
    if header.block_type != COMMIT || header.sequence != sequence {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal commit");
    }
    if journal.csum_v3 {
        if block.len() != BLOCK_SIZE {
            return_errno_with_message!(Errno::EUCLEAN, "truncated ext4 journal commit");
        }
        let stored = be32(block, 16);
        let mut copy = [0u8; BLOCK_SIZE];
        copy.copy_from_slice(block);
        copy[12..16].fill(0);
        copy[16..20].fill(0);
        if checksum::crc32c(journal.csum_seed, &copy) != stored {
            return_errno_with_message!(Errno::EUCLEAN, "invalid journal commit checksum");
        }
    }
    Ok(())
}

pub(super) fn parse_revoke(
    block: &[u8],
    sequence: u32,
    journal: &SuperBlock,
) -> Result<Vec<u32>> {
    let header = parse_header(block)?;
    if header.block_type != REVOKE || header.sequence != sequence || block.len() < 16 {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal revoke");
    }
    if !verify_block_tail(block, journal) {
        return_errno_with_message!(Errno::EUCLEAN, "invalid journal revoke checksum");
    }
    let limit = block.len() - journal.block_tail_size();
    let bytes = be32(block, 12) as usize;
    if bytes < 16 || bytes > limit || (bytes - 16) % 4 != 0 {
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

#[inline]
fn put_be32(block: &mut [u8], offset: usize, value: u32) {
    block[offset..offset + 4].copy_from_slice(&value.to_be_bytes());
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::ktest;

    use super::*;

    fn journal() -> SuperBlock {
        SuperBlock::parse(&superblock()).unwrap()
    }

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
    fn journal_ring_wraps_at_max_length() {
        let journal = SuperBlock::parse(&superblock()).unwrap();
        let mut ring = JournalRing::new(journal).unwrap();
        ring.next = 1022;
        assert_eq!(ring.reserve(3).unwrap(), vec![1022, 1023, 1]);
        assert_eq!(ring.next, 2);
    }

    #[ktest]
    fn journal_ring_rejects_oversized_reservation() {
        let journal = SuperBlock::parse(&superblock()).unwrap();
        let mut ring = JournalRing::new(journal).unwrap();
        assert!(ring.reserve(1024).is_err());
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
    fn validates_journal_start_updates() {
        let mut block = superblock();
        SuperBlock::write_start(&mut block, 9).unwrap();
        assert_eq!(SuperBlock::parse(&block).unwrap().start, 9);
        assert!(SuperBlock::write_start(&mut block, 1024).is_err());
        SuperBlock::write_sequence(&mut block, 73).unwrap();
        assert_eq!(SuperBlock::parse(&block).unwrap().sequence, 73);
        assert!(SuperBlock::write_sequence(&mut block, 0).is_err());
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
        let tags = parse_descriptor(&descriptor, 7, &journal()).unwrap();
        assert_eq!(tags.len(), 2);
        assert_eq!(tags[1].block_number, 32);
        assert!(parse_descriptor(&descriptor[..35], 7, &journal()).is_err());
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
            parse_descriptor(&descriptor, 7, &journal()).unwrap()[0].block_number,
            31
        );

        let mut commit = [0; BLOCK_SIZE];
        commit[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        commit[4..8].copy_from_slice(&COMMIT.to_be_bytes());
        commit[8..12].copy_from_slice(&7u32.to_be_bytes());
        validate_commit(&commit, 7, &journal()).unwrap();

        let mut revoke = commit;
        revoke[4..8].copy_from_slice(&REVOKE.to_be_bytes());
        revoke[12..16].copy_from_slice(&20u32.to_be_bytes());
        revoke[16..20].copy_from_slice(&99u32.to_be_bytes());
        assert_eq!(parse_revoke(&revoke, 7, &journal()).unwrap(), vec![99]);
    }

    #[ktest]
    fn encodes_transaction_and_escapes_magic_payload() {
        let mut transaction = JournalTransaction::new(7).unwrap();
        let mut payload = vec![0; BLOCK_SIZE];
        payload[..4].copy_from_slice(&MAGIC.to_be_bytes());
        transaction.add_block(31, &payload).unwrap();
        let journal = SuperBlock::parse(&superblock()).unwrap();
        let blocks = transaction.encode(&journal).unwrap();
        assert_eq!(blocks.len(), 3);
        let tags = parse_descriptor(&blocks[0], 7, &journal).unwrap();
        assert_eq!(
            tags,
            vec![Tag {
                block_number: 31,
                flags: FLAG_ESCAPE | FLAG_LAST_TAG,
                checksum: 0,
            }]
        );
        assert_eq!(be32(&blocks[1], 0), 0);
        validate_commit(&blocks[2], 7, &journal).unwrap();
    }

    #[ktest]
    fn encodes_multiple_blocks_with_the_uuid_once() {
        let mut transaction = JournalTransaction::new(9).unwrap();
        transaction.add_block(31, &[0x11; BLOCK_SIZE]).unwrap();
        transaction.add_block(32, &[0x22; BLOCK_SIZE]).unwrap();
        let mut sb_block = superblock();
        sb_block[48..64].fill(0x5a);
        let journal = SuperBlock::parse(&sb_block).unwrap();
        let blocks = transaction.encode(&journal).unwrap();

        // The first tag carries the UUID; the second is only reachable if the
        // sixteen UUID bytes are actually present in the descriptor.
        assert_eq!(&blocks[0][20..36], &[0x5a; 16]);
        let tags = parse_descriptor(&blocks[0], 9, &journal).unwrap();
        assert_eq!(tags.len(), 2);
        assert_eq!(tags[0].block_number, 31);
        assert_eq!(tags[0].flags & FLAG_SAME_UUID, 0);
        assert_eq!(tags[1].block_number, 32);
        assert_ne!(tags[1].flags & FLAG_SAME_UUID, 0);

        let replay = scan_log(
            &[&blocks[0], &blocks[1], &blocks[2], &blocks[3]],
            9,
            &journal,
        )
        .unwrap();
        assert_eq!(replay.len(), 2);
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
            decode_transaction(&[&descriptor, &data_a, &data_b, &revoke, &commit], 7, &journal())
                .unwrap();
        assert_eq!(replay.len(), 1);
        assert_eq!(replay[0].block_number, 31);
        assert!(replay[0].escaped);
        assert_eq!(&replay[0].data[..4], &MAGIC.to_be_bytes());
        assert!(
            decode_transaction(&[&descriptor, &data_a, &data_b, &revoke], 7, &journal())
                .is_err()
        );
        commit[8..12].copy_from_slice(&8u32.to_be_bytes());
        assert!(
            decode_transaction(&[&descriptor, &data_a, &data_b, &revoke, &commit], 7, &journal())
                .is_err()
        );
    }

    #[ktest]
    fn checksum_v3_transactions_round_trip_and_detect_corruption() {
        let mut block = superblock();
        block[48..64].fill(0x6d);
        block[40..44].copy_from_slice(&INCOMPAT_CSUM_V3.to_be_bytes());
        seal_superblock(&mut block);
        let journal = SuperBlock::parse(&block).unwrap();
        assert!(journal.csum_v3);

        let mut transaction = JournalTransaction::new(21).unwrap();
        let mut escaped = vec![0x77; BLOCK_SIZE];
        escaped[..4].copy_from_slice(&MAGIC.to_be_bytes());
        transaction.add_block(31, &escaped).unwrap();
        transaction.add_block(32, &[0x22; BLOCK_SIZE]).unwrap();
        let blocks = transaction.encode(&journal).unwrap();
        assert_eq!(blocks.len(), 4);
        assert_eq!(journal.tag_size(), TAG3_SIZE);

        let refs: Vec<&[u8]> = blocks.iter().map(Vec::as_slice).collect();
        let replay = decode_transaction(&refs, 21, &journal).unwrap();
        assert_eq!(replay.len(), 2);
        assert_eq!(&replay[0].data[..4], &MAGIC.to_be_bytes());

        // A flipped data byte, a flipped tag checksum and a flipped descriptor
        // tail must all be rejected.
        let mut bad_data = blocks[1].clone();
        bad_data[100] ^= 0xff;
        assert!(
            decode_transaction(&[&blocks[0], &bad_data, &blocks[2], &blocks[3]], 21, &journal)
                .is_err()
        );
        let mut bad_tag = blocks[0].clone();
        bad_tag[12 + 12] ^= 0xff;
        assert!(
            decode_transaction(&[&bad_tag, &blocks[1], &blocks[2], &blocks[3]], 21, &journal)
                .is_err()
        );
        let mut bad_tail = blocks[0].clone();
        *bad_tail.last_mut().unwrap() ^= 0xff;
        assert!(parse_descriptor(&bad_tail, 21, &journal).is_err());
    }

    #[ktest]
    fn deleted_tags_are_replayed_like_ordinary_tags() {
        let mut descriptor = [0; BLOCK_SIZE];
        descriptor[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        descriptor[4..8].copy_from_slice(&DESCRIPTOR.to_be_bytes());
        descriptor[8..12].copy_from_slice(&7u32.to_be_bytes());
        descriptor[12..16].copy_from_slice(&31u32.to_be_bytes());
        descriptor[16..20]
            .copy_from_slice(&(FLAG_DELETED | FLAG_SAME_UUID | FLAG_LAST_TAG).to_be_bytes());

        let data = [0x44; BLOCK_SIZE];
        let mut commit = [0; BLOCK_SIZE];
        commit[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        commit[4..8].copy_from_slice(&COMMIT.to_be_bytes());
        commit[8..12].copy_from_slice(&7u32.to_be_bytes());

        let replay = decode_transaction(&[&descriptor, &data, &commit], 7, &journal()).unwrap();
        assert_eq!(replay.len(), 1);
        assert_eq!(replay[0].block_number, 31);
        assert_eq!(replay[0].data, data);
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
            &journal(),
        )
        .unwrap();
        assert!(replay.is_empty());
    }

    #[ktest]
    fn later_transaction_wins_for_same_target_block() {
        let mut descriptor_a = [0; BLOCK_SIZE];
        descriptor_a[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        descriptor_a[4..8].copy_from_slice(&DESCRIPTOR.to_be_bytes());
        descriptor_a[8..12].copy_from_slice(&11u32.to_be_bytes());
        descriptor_a[12..16].copy_from_slice(&44u32.to_be_bytes());
        descriptor_a[16..20].copy_from_slice(&(FLAG_LAST_TAG | FLAG_SAME_UUID).to_be_bytes());

        let mut descriptor_b = descriptor_a;
        descriptor_b[8..12].copy_from_slice(&12u32.to_be_bytes());
        let data_a = [0x11; BLOCK_SIZE];
        let data_b = [0x22; BLOCK_SIZE];

        let mut commit_a = [0; BLOCK_SIZE];
        commit_a[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        commit_a[4..8].copy_from_slice(&COMMIT.to_be_bytes());
        commit_a[8..12].copy_from_slice(&11u32.to_be_bytes());
        let mut commit_b = commit_a;
        commit_b[8..12].copy_from_slice(&12u32.to_be_bytes());

        let replay = scan_log(
            &[
                &descriptor_a,
                &data_a,
                &commit_a,
                &descriptor_b,
                &data_b,
                &commit_b,
            ],
            11,
            &journal(),
        )
        .unwrap();
        assert_eq!(replay.len(), 1);
        assert_eq!(replay[0].block_number, 44);
        assert_eq!(replay[0].data, data_b);
    }

    fn transaction_payload(index: usize) -> Vec<u8> {
        let mut payload = vec![0x40 + (index % 16) as u8; BLOCK_SIZE];
        // Keep the first word away from MAGIC so the block is not escaped.
        payload[..4].fill(0);
        payload
    }

    fn encode_many(journal: &SuperBlock, count: usize) -> Vec<Vec<u8>> {
        let mut transaction = JournalTransaction::new(23).unwrap();
        for index in 0..count {
            transaction
                .add_block(1000 + index as u32, &transaction_payload(index))
                .unwrap();
        }
        transaction.encode(journal).unwrap()
    }

    #[ktest]
    fn splits_oversized_legacy_transactions_across_descriptors() {
        let journal = SuperBlock::parse(&superblock()).unwrap();
        let capacity = JournalTransaction::descriptor_capacity(&journal);
        assert_eq!(capacity, (BLOCK_SIZE - HEADER_SIZE - 16) / TAG_SIZE);

        let count = capacity + 10;
        let blocks = encode_many(&journal, count);
        // Two descriptors, one data block per tag, one trailing commit.
        assert_eq!(blocks.len(), 2 + count + 1);
        assert_eq!(be32(&blocks[0], 4), DESCRIPTOR);
        assert_eq!(be32(&blocks[1 + capacity], 4), DESCRIPTOR);

        let refs: Vec<&[u8]> = blocks.iter().map(Vec::as_slice).collect();
        let replay = decode_transaction(&refs, 23, &journal).unwrap();
        assert_eq!(replay.len(), count);
        assert_eq!(replay[0].block_number, 1000);
        assert_eq!(replay[count - 1].block_number, 1000 + count as u32 - 1);

        let scanned = scan_log(&refs, 23, &journal).unwrap();
        assert_eq!(scanned.len(), count);
        assert_eq!(scanned[0].block_number, 1000);
    }

    #[ktest]
    fn splits_oversized_checksummed_transactions_across_descriptors() {
        let mut block = superblock();
        block[48..64].fill(0x6d);
        block[40..44].copy_from_slice(&INCOMPAT_CSUM_V3.to_be_bytes());
        seal_superblock(&mut block);
        let journal = SuperBlock::parse(&block).unwrap();
        assert!(journal.csum_v3);
        let capacity = JournalTransaction::descriptor_capacity(&journal);
        assert_eq!(
            capacity,
            (BLOCK_SIZE - HEADER_SIZE - 16 - BLOCK_TAIL_SIZE) / TAG3_SIZE
        );

        let count = capacity + 3;
        let blocks = encode_many(&journal, count);
        assert_eq!(blocks.len(), 2 + count + 1);

        let refs: Vec<&[u8]> = blocks.iter().map(Vec::as_slice).collect();
        let replay = decode_transaction(&refs, 23, &journal).unwrap();
        assert_eq!(replay.len(), count);
        let scanned = scan_log(&refs, 23, &journal).unwrap();
        assert_eq!(scanned.len(), count);
    }
}
