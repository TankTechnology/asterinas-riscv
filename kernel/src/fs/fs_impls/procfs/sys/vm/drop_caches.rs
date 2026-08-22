// SPDX-License-Identifier: MPL-2.0

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps, read_i32_from},
        vfs::inode::Inode,
    },
    prelude::*,
};

/// Represents the inode at `/proc/sys/vm/drop_caches`.
///
/// Writing 1/2/3 asks the kernel to drop the page cache / slab caches,
/// which is purely a performance hint with no correctness effect. As our
/// in-memory filesystems cannot discard their only copy of the data, the
/// write is validated and accepted as a no-op, mirroring the user-visible
/// semantics (LTP's `madvise06`/`preadv203` only require the write to
/// succeed as root).
pub struct DropCachesFileOps;

impl DropCachesFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Linux marks `/proc/sys/vm/drop_caches` as write-only by the owner
        // (mode 0200, root:root).
        // Reference:
        // <https://elixir.bootlin.com/linux/v6.16.5/source/mm/drop_caches.c>
        ProcFile::new(Self, parent, mkmod!(u+w))
    }
}

impl ProcFileOps for DropCachesFileOps {
    fn read_at(&self, _offset: usize, _writer: &mut VmWriter) -> Result<usize> {
        // Linux has no read handler for drop_caches; reading fails with
        // EINVAL even for root (the mode already makes it unreachable for
        // non-root users).
        return_errno_with_message!(Errno::EINVAL, "drop_caches is write-only");
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        let (val, read_bytes) = read_i32_from(reader)?;

        // Only 1 (page cache), 2 (slab), and 3 (both) are valid on Linux.
        if !(1..=3).contains(&val) {
            return_errno_with_message!(Errno::EINVAL, "invalid drop_caches value");
        }

        // No-op: dropping caches must never change the observable state.
        Ok(read_bytes)
    }
}
