// SPDX-License-Identifier: MPL-2.0

use crate::{
    fs::{
        file::{InodeMode, mkmod},
        fs_impls::ramfs::{self, RamFs},
        pseudofs::AnonDeviceId,
        vfs::{
            file_system::{FileSystem, SuperBlock},
            inode::RevalidationPolicy,
            registry::{FsCreationCtx, FsProperties, FsType},
        },
    },
    prelude::*,
};

const TMPFS_MAGIC: u64 = 0x0102_1994;

/// The temporary file system (tmpfs) structure.
//
// TODO: `TmpFs` currently aliases `RamFs` and relies on `RamFs::new_tmpfs()`
// to create tmpfs-flavored ramfs instances. In the future we need to
// implement a dedicated tmpfs with tmpfs-specific features such as memory
// limits and swap support.
pub(crate) type TmpFs = RamFs;

impl TmpFs {
    /// Creates a tmpfs filesystem instance.
    pub(in crate::fs) fn new_tmpfs() -> Arc<Self> {
        Self::new_tmpfs_backing("tmpfs", RevalidationPolicy::empty())
    }

    /// Creates a tmpfs-backed filesystem instance with a custom filesystem name
    /// and directory-entry revalidation policy.
    pub(in crate::fs) fn new_tmpfs_backing(
        name: &'static str,
        revalidation_policy: RevalidationPolicy,
    ) -> Arc<Self> {
        let anon_device_id = AnonDeviceId::acquire().expect("no device ID is available for tmpfs");
        let sb = {
            let mut super_block = SuperBlock::new(
                TMPFS_MAGIC,
                ramfs::BLOCK_SIZE,
                ramfs::NAME_MAX,
                anon_device_id.id(),
            );
            let max_blocks = default_max_blocks();
            let max_inodes = default_max_inodes();
            super_block.blocks = max_blocks;
            super_block.bfree = max_blocks;
            super_block.bavail = max_blocks;
            super_block.files = max_inodes;
            super_block.ffree = max_inodes;
            super_block
        };
        Self::new_with_sb(
            name,
            anon_device_id,
            sb,
            revalidation_policy,
            // Linux tmpfs roots are 1777 (sticky, world-writable); without it
            // non-root users cannot create POSIX shm objects in /dev/shm.
            mkmod!(a+rwx) | InodeMode::S_ISVTX,
        )
    }
}

// FIXME: These defaults are only a rough approximation for tmpfs-over-ramfs.
// A dedicated tmpfs implementation should replace them with real tmpfs limit
// and accounting semantics.
fn default_max_blocks() -> usize {
    crate::vm::mem_total() / PAGE_SIZE / 2
}
fn default_max_inodes() -> usize {
    crate::vm::mem_total() / PAGE_SIZE / 2
}

pub(super) struct TmpFsType;

impl FsType for TmpFsType {
    type Key = ();

    fn name(&self) -> &'static str {
        "tmpfs"
    }

    fn properties(&self) -> FsProperties {
        FsProperties::empty()
    }

    fn create(&self, _fs_creation_ctx: &mut FsCreationCtx) -> Result<Arc<dyn FileSystem>> {
        Ok(TmpFs::new_tmpfs())
    }

    fn sysnode(&self) -> Option<Arc<dyn aster_systree::SysNode>> {
        None
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::TmpFs;
    use crate::{
        fs::{
            file::{InodeMode, mkmod},
            fs_impls::ramfs::RamFs,
            vfs::{file_system::FileSystem, inode::Inode},
        },
        time::clocks::init_for_ktest as time_init_for_ktest,
    };

    /// Linux tmpfs roots are `S_IRWXUGO | S_ISVTX` (1777), so that non-root
    /// users can create POSIX shm objects under `/dev/shm`.
    #[ktest]
    fn tmpfs_root_mode_is_1777() {
        time_init_for_ktest();
        let fs = TmpFs::new_tmpfs();

        assert_eq!(
            fs.root_inode().mode().unwrap(),
            mkmod!(a+rwx) | InodeMode::S_ISVTX
        );
    }

    /// Plain ramfs roots stay 0755, like Linux ramfs roots.
    #[ktest]
    fn ramfs_root_mode_stays_0755() {
        time_init_for_ktest();
        let fs = RamFs::new();

        assert_eq!(fs.root_inode().mode().unwrap(), mkmod!(a+rx, u+w));
    }
}
