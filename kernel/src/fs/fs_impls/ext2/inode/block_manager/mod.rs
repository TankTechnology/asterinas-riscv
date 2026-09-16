// SPDX-License-Identifier: MPL-2.0

//! Physical-block lifecycle management for a single ext2 inode.

mod block_ptr_tree;
mod indirect_block_manager;

use core::sync::atomic::{AtomicUsize, Ordering};

use aster_block::bio::BioCompleteFn;
use ostd::mm::io::util::HasVmReaderWriter;

use self::block_ptr_tree::ResolvedBlockRange;
pub(super) use self::block_ptr_tree::{BlockPtrTree, RawBlockPtrs};
use super::io_range::IoRangeIter;
use crate::fs::ext2::{fs::Ext2, prelude::*};

/// Bridges the inode's logical file view and the physical block device.
///
/// `InodeBlockManager` maintains the relationship between logical file
/// blocks, ext2 physical block addresses, and the page-cache view of file
/// contents. Sparse logical ranges are represented by absent block mappings,
/// while allocated ranges must remain consistent with the inode's
/// block-pointer tree.
#[derive(Debug)]
pub(super) struct InodeBlockManager {
    /// Translates logical file block indices to physical device block addresses and
    /// manages block allocation and truncation.
    block_ptr_tree: RwMutex<BlockPtrTree>,
    /// Cached `npages` bound for `PageCache`.
    npages: AtomicUsize,
    /// File system handle for indirect I/O and BIO submission.
    fs: Weak<Ext2>,
}

impl InodeBlockManager {
    /// Creates a new block manager wrapping the given block-pointer tree.
    pub(super) fn new(block_ptr_tree: BlockPtrTree, fs: Weak<Ext2>, npages: usize) -> Self {
        Self {
            block_ptr_tree: RwMutex::new(block_ptr_tree),
            npages: AtomicUsize::new(npages),
            fs,
        }
    }

    /// Returns a strong reference to the owning filesystem.
    pub(super) fn fs(&self) -> Result<Arc<Ext2>> {
        self.fs
            .upgrade()
            .ok_or_else(|| Error::with_message(Errno::EIO, "filesystem already dropped"))
    }

    /// Looks up a single logical block -> physical block.
    pub(super) fn lookup_block(&self, iblock: Iblock) -> Result<Option<Ext2Bid>> {
        let tree = self.block_ptr_tree.read();
        tree.lookup_block(iblock)
    }

    /// Returns a snapshot of the raw block pointer state.
    pub(super) fn raw_block_ptrs(&self) -> RawBlockPtrs {
        *self.block_ptr_tree.read().raw_block_ptrs()
    }

    /// Returns whether the block-pointer tree has uncommitted changes.
    pub(super) fn is_dirty(&self) -> bool {
        self.block_ptr_tree.read().is_dirty()
    }

    /// Clears the block-pointer dirty flag after writeback.
    pub(super) fn clear_dirty(&self) {
        self.block_ptr_tree.write().clear_dirty();
    }

    /// Accounts for an external xattr block in the inode's 512-byte sector count.
    pub(super) fn update_xattr_block_accounting(
        &self,
        old_bid: Ext2Bid,
        new_bid: Ext2Bid,
    ) -> Result<()> {
        self.block_ptr_tree
            .write()
            .update_xattr_block_accounting(old_bid, new_bid)
    }

    /// Creates an iterator over existing and hole block ranges.
    ///
    /// The returned iterator holds a read lock on the block-pointer tree for
    /// its entire lifetime. Callers should consume it promptly to avoid
    /// blocking concurrent allocations or truncations on this inode.
    pub(super) fn iter_io_ranges(&self, block_range: Range<Iblock>) -> IoRangeIter<'_> {
        let tree = self.block_ptr_tree.read();
        IoRangeIter::new(block_range, tree)
    }

    /// Truncates blocks to the new_size (best-effort).
    pub(super) fn truncate_to_byte_len(&self, new_size: usize) {
        let fs = match self.fs() {
            Ok(fs) => fs,
            Err(err) => {
                error!("truncate: failed to get fs reference, err: {:?}", err);
                return;
            }
        };
        let mut tree = self.block_ptr_tree.write();
        tree.truncate_to_byte_len(&fs, new_size)
    }

    /// Flushes all dirty cached indirect blocks to the device.
    pub(super) fn sync_indirect_blocks(&self) -> Result<()> {
        self.block_ptr_tree.write().sync_indirect_blocks()
    }

    /// Allocates missing data blocks that cover the requested logical block range.
    pub(super) fn allocate_range_blocks(&self, start_block: usize, end_block: usize) -> Result<()> {
        let fs = self.fs()?;
        let mut tree = self.block_ptr_tree.write();
        let mut current_block = start_block;
        while current_block < end_block {
            let iblock = Iblock::try_from(current_block)
                .map_err(|_| Error::with_message(Errno::EINVAL, "logical block number overflow"))?;
            let remaining = u32::try_from(end_block - current_block)
                .map_err(|_| Error::with_message(Errno::EINVAL, "block range length overflow"))?;

            let block_range = tree.resolve_block_range(&fs, iblock, remaining)?;
            match block_range {
                ResolvedBlockRange::Existing(range) => {
                    debug_assert!(!range.is_empty());
                    current_block += range.len();
                }
                ResolvedBlockRange::NewlyAllocated(range) => {
                    debug_assert!(!range.is_empty());
                    current_block += range.len();
                }
            }
        }
        Ok(())
    }

    /// Updates the cached page-cache capacity bound.
    pub(super) fn set_npages(&self, npages: usize) {
        self.npages.store(npages, Ordering::Release);
    }
}

impl BlockAsPageCacheBackend for InodeBlockManager {
    fn submit_read_bios(
        &self,
        requests: Vec<PageCacheReadRequest>,
        io_batch: &mut IoBatch,
    ) -> Result<()> {
        let npages = self.npages.load(Ordering::Acquire);
        if requests.iter().any(|request| request.idx() >= npages) {
            return_errno_with_message!(Errno::EINVAL, "invalid read size");
        }

        let fs = self.fs()?;
        let mut run_start_bid = None;
        let mut run = Vec::new();

        for request in requests {
            let idx = request.idx();
            let iblock = Iblock::try_from(idx)
                .map_err(|_| Error::with_message(Errno::EINVAL, "logical block number overflow"))?;
            let Some(bid) = self.lookup_block(iblock)? else {
                submit_contiguous_read_run(
                    &fs,
                    run_start_bid.take(),
                    core::mem::take(&mut run),
                    io_batch,
                )?;
                let (_, _, complete_fn) = request.into_parts();
                complete_fn(BioStatus::Zeros);
                continue;
            };

            let is_continuation = run_start_bid.is_some_and(|start_bid| {
                run.first().is_some_and(|first: &PageCacheReadRequest| {
                    continues_read_run(first.idx(), start_bid, run.len(), idx, bid)
                })
            });
            if !run.is_empty() && !is_continuation {
                submit_contiguous_read_run(
                    &fs,
                    run_start_bid.take(),
                    core::mem::take(&mut run),
                    io_batch,
                )?;
            }
            if run.is_empty() {
                run_start_bid = Some(bid);
            }
            run.push(request);
        }

        submit_contiguous_read_run(&fs, run_start_bid, run, io_batch)
    }

    fn submit_read_bio(
        &self,
        idx: usize,
        bio_segment: BioSegment,
        complete_fn: BioCompleteFn,
        io_batch: &mut IoBatch,
    ) -> Result<()> {
        if idx >= self.npages.load(Ordering::Acquire) {
            return_errno_with_message!(Errno::EINVAL, "invalid read size");
        }
        let iblock = Iblock::try_from(idx)
            .map_err(|_| Error::with_message(Errno::EINVAL, "logical block number overflow"))?;
        match self.lookup_block(iblock)? {
            Some(bid) => {
                let fs = self.fs()?;
                fs.read_blocks_async(bid, bio_segment, Some(complete_fn), io_batch)
            }
            None => {
                // Encountered a hole, zero fill the page.
                complete_fn(BioStatus::Zeros);
                Ok(())
            }
        }
    }

    fn submit_write_bio(
        &self,
        idx: usize,
        bio_segment: BioSegment,
        complete_fn: BioCompleteFn,
        io_batch: &mut IoBatch,
    ) -> Result<()> {
        if idx >= self.npages.load(Ordering::Acquire) {
            return_errno_with_message!(Errno::EINVAL, "invalid write size");
        }
        let iblock = Iblock::try_from(idx)
            .map_err(|_| Error::with_message(Errno::EINVAL, "logical block number overflow"))?;
        let fs = self.fs()?;

        // TODO: Refactor `lookup_block` and `resolve_block_range`. Currently
        // `bio_segment.nblocks()` is always 1, so the point lookup works correctly, but the
        // semantics are misleading because `bio_segment` can represent a contiguous block range.
        // The block is already allocated; write it directly.
        if let Some(bid) = self.lookup_block(iblock)? {
            return fs.write_blocks_async(bid, bio_segment, Some(complete_fn), io_batch);
        }

        // Encounter a hole; allocate a block. Since we dropped the read lock
        // above, another thread may have filled the hole; the `Existing` arm
        // below handles that race.
        let mut tree = self.block_ptr_tree.write();
        let step = tree.resolve_block_range(&fs, iblock, bio_segment.nblocks() as u32)?;
        let bid = match step {
            ResolvedBlockRange::NewlyAllocated(r) => r.start,
            ResolvedBlockRange::Existing(r) => r.start,
        };

        fs.write_blocks_async(bid, bio_segment, Some(complete_fn), io_batch)
    }
}

fn continues_read_run(
    first_idx: usize,
    start_bid: Ext2Bid,
    run_len: usize,
    next_idx: usize,
    next_bid: Ext2Bid,
) -> bool {
    first_idx.checked_add(run_len) == Some(next_idx)
        && Ext2Bid::try_from(run_len)
            .ok()
            .and_then(|len| start_bid.checked_add(len))
            == Some(next_bid)
}

/// Submits one physically contiguous extent and scatters its completion data
/// into the original page-cache segments without allocating in the callback.
fn submit_contiguous_read_run(
    fs: &Ext2,
    start_bid: Option<Ext2Bid>,
    requests: Vec<PageCacheReadRequest>,
    io_batch: &mut IoBatch,
) -> Result<()> {
    let Some(start_bid) = start_bid else {
        debug_assert!(requests.is_empty());
        return Ok(());
    };
    debug_assert!(!requests.is_empty());

    let extent_segment = BioSegment::alloc(requests.len(), BioDirection::FromDevice);
    let scatter_source = extent_segment.clone();
    let complete_fn: BioCompleteFn = Box::new(move |status| {
        if status != BioStatus::Complete {
            for request in requests {
                let (_, _, page_complete_fn) = request.into_parts();
                page_complete_fn(status);
            }
            return;
        }

        let Ok(mut reader) = scatter_source.reader() else {
            for request in requests {
                let (_, _, page_complete_fn) = request.into_parts();
                page_complete_fn(BioStatus::IoError);
            }
            return;
        };
        for request in requests {
            let (_, page_segment, page_complete_fn) = request.into_parts();
            let page_status = match page_segment.write_from_device_reader(&mut reader) {
                Ok(()) => BioStatus::Complete,
                Err(_) => BioStatus::IoError,
            };
            page_complete_fn(page_status);
        }
    });

    fs.read_blocks_async(start_bid, extent_segment, Some(complete_fn), io_batch)
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::{Ext2Bid, continues_read_run};

    #[ktest]
    fn read_run_requires_logical_and_physical_contiguity() {
        assert!(continues_read_run(4, 100, 3, 7, 103));
        assert!(!continues_read_run(4, 100, 3, 8, 103));
        assert!(!continues_read_run(4, 100, 3, 7, 104));
    }

    #[ktest]
    fn read_run_rejects_overflow() {
        assert!(!continues_read_run(usize::MAX, Ext2Bid::MAX, 1, 0, 0));
    }
}
