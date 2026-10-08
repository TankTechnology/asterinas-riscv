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
const SUPERBLOCK_SIZE: usize = 28;
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
        let start = be32(block, 24);
        if block_size as usize != BLOCK_SIZE || max_length == 0 || first >= max_length {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal geometry");
        }
        if start >= max_length && start != 0 {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal start");
        }
        Ok(Self { block_size, max_length, first, start })
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

pub(super) fn parse_header(block: &[u8]) -> Result<Header> {
    if block.len() < HEADER_SIZE || be32(block, 0) != MAGIC {
        return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal block header");
    }
    Ok(Header { block_type: be32(block, 4), sequence: be32(block, 8) })
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
        let tag = Tag { block_number: be32(block, offset), flags: be32(block, offset + 4) };
        let known = FLAG_ESCAPE | FLAG_SAME_UUID | FLAG_DELETED | FLAG_LAST_TAG;
        if tag.flags & !known != 0 || tag.block_number == 0 {
            return_errno_with_message!(Errno::EUCLEAN, "invalid ext4 journal descriptor tag");
        }
        tags.push(tag);
        offset += TAG_SIZE;
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
    fn validates_descriptor_commit_and_revoke() {
        let mut descriptor = [0; BLOCK_SIZE];
        descriptor[0..4].copy_from_slice(&MAGIC.to_be_bytes());
        descriptor[4..8].copy_from_slice(&DESCRIPTOR.to_be_bytes());
        descriptor[8..12].copy_from_slice(&7u32.to_be_bytes());
        descriptor[12..16].copy_from_slice(&31u32.to_be_bytes());
        descriptor[20..24].copy_from_slice(&FLAG_LAST_TAG.to_be_bytes());
        assert_eq!(parse_descriptor(&descriptor, 7).unwrap()[0].block_number, 31);

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
}
