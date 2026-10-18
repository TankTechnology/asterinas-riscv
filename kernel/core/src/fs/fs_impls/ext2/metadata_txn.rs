// SPDX-License-Identifier: MPL-2.0

//! Staged metadata for the next journal transaction.
//!
//! ext4 makes a group of metadata blocks atomic by writing all of them into
//! one journal transaction and publishing a single commit record. This module
//! is the in-memory half of that: it collects the latest contents of every
//! metadata block that has changed, keyed by block number, until the filesystem
//! reaches a commit point.
//!
//! The staging set is deliberately a map rather than a list. The journal layer
//! rejects duplicate block numbers, and several paths can touch the same block
//! before a commit (the inode table block of a group is written once per
//! flushed inode descriptor, for example).

use super::prelude::*;

/// Metadata blocks waiting for the next commit.
///
/// A block is staged at most once; staging a block again replaces its contents
/// and keeps the original insertion position.
#[derive(Debug)]
pub(super) struct MetadataTxn {
    blocks: BTreeMap<Ext2Bid, Vec<u8>>,
}

impl MetadataTxn {
    pub(super) fn new() -> Self {
        Self {
            blocks: BTreeMap::new(),
        }
    }

    /// Stages one block.
    ///
    /// The caller must pass a full block. A short or overlong block almost
    /// always means the caller confused an inode-local offset with a block
    /// offset, and accepting it would write a torn block into the journal.
    pub(super) fn stage(&mut self, bid: Ext2Bid, bytes: &[u8]) -> Result<()> {
        if bytes.len() != BLOCK_SIZE {
            return_errno_with_message!(Errno::EINVAL, "metadata block is not block-sized");
        }

        match self.blocks.get_mut(&bid) {
            Some(staged) => {
                if staged.as_slice() != bytes {
                    staged.copy_from_slice(bytes);
                }
            }
            None => {
                self.blocks.insert(bid, bytes.to_vec());
            }
        }

        Ok(())
    }

    pub(super) fn is_empty(&self) -> bool {
        self.blocks.is_empty()
    }

    /// Removes and returns every staged block.
    ///
    /// The caller writes them outside this lock. A path reached from the
    /// journal write itself (syncing the journal inode) may stage metadata,
    /// and holding the lock across the write would deadlock against it.
    pub(super) fn take(&mut self) -> Vec<(Ext2Bid, Vec<u8>)> {
        core::mem::take(&mut self.blocks).into_iter().collect()
    }

    /// Puts blocks back after a failed write.
    ///
    /// A block that was staged again while the write was in flight keeps the
    /// newer contents: it reflects state the failed transaction did not cover.
    pub(super) fn restore(&mut self, blocks: Vec<(Ext2Bid, Vec<u8>)>) {
        for (bid, bytes) in blocks {
            self.blocks.entry(bid).or_insert(bytes);
        }
    }
}
