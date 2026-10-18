// SPDX-License-Identifier: MPL-2.0

//! Ext2 filesystem implementation, providing file I/O, directory operations,
//! symlinks, and extended attributes through the Asterinas VFS trait interfaces.
//!
//! This module is the entry point for ext2 support in Asterinas. A caller
//! registers `Ext2` as a filesystem type via `init`, after which the VFS
//! can mount ext2 volumes and operate on them through the standard
//! filesystem trait interfaces. Buffered I/O is delegated to the
//! `PageCache` subsystem; this module does not cache block data itself.
//!
//! The Second Extended File System (ext2) is a classic Linux filesystem
//! introduced in 1993 as a replacement for the original ext filesystem.
//! It was the default Linux filesystem throughout the 1990s and remains
//! the on-disk foundation for ext3 and ext4. The ext4 compatibility mount
//! adds validated JBD2 replay and inline/depth-one extent lookup and
//! allocation. Multi-level extent mutation, inline data, and journaled
//! writeback remain explicitly unsupported until their transaction paths
//! are migrated into this compiled implementation.
//!
//! # On-disk layout
//!
//! An ext2 volume is divided into fixed-size block groups. Each block group
//! contains a block bitmap, an inode bitmap, an inode table, and data blocks.
//! The superblock and the block group descriptor table are stored in block
//! group 0 (with backup copies in select other groups). The `fs` module
//! manages the superblock and block group descriptor table, while
//! `block_group` tracks per-group allocation state.
//!
//! Individual files and directories are represented by inodes. The `inode`
//! module handles inode I/O, including data block mapping, directory entry
//! management, and symlink resolution. The `impl_for_vfs` module wires
//! these operations into the VFS trait interfaces so that the rest of the
//! kernel accesses ext2 through the common filesystem API.
//!
//! # Module structure
//!
//! | Module         | Responsibility                                       |
//! |----------------|------------------------------------------------------|
//! | `fs`           | Filesystem-level state: superblock, block groups     |
//! | `inode`        | Inode operations: file I/O, directories, symlinks    |
//! | `xattr`        | Extended attribute block management                  |
//! | `block_group`  | Block group descriptor and per-group allocation      |
//! | `super_block`  | On-disk superblock parsing and writeback             |
//! | `impl_for_vfs` | Wires ext2 types into the VFS trait interfaces       |
//! | `fs_type`      | `FsType` registration glue                           |
//! | `utils`        | Dirty tracking, sparse-super helpers, and time utils |
//! | `prelude`      | Common imports shared across submodules              |
//!
//! Directory entry layout helpers live under `inode::dir::dir_entry`,
//! next to the directory operations that use them.
//!
//! # References
//!
//! - <https://www.kernel.org/doc/html/latest/filesystems/ext2.html>
//! - <https://www.nongnu.org/ext2-doc/ext2.html>

// Set this module's log prefix for `ostd::log`.
macro_rules! __log_prefix {
    () => {
        "ext2: "
    };
}

pub(crate) use fs::Ext2;
pub(crate) use inode::{FilePerm, Inode};

pub(in crate::fs) use self::fs_type::{EXT2_TYPE, EXT4_TYPE};
use crate::fs::vfs::registry;

use core::sync::atomic::{AtomicU8, Ordering};

mod block_group;
mod fs;
mod fs_type;
mod impl_for_vfs;
mod inode;
mod journal;
mod metadata_txn;
mod prelude;
mod super_block;
mod utils;
mod xattr;

#[cfg(ktest)]
mod test_utils;

/// The journal commit stage at which a power loss should be simulated.
///
/// Zero disables the fault. The boot path sets it with
/// `asterinas.ext4_fault_stage=N`; the acceptance harness uses it to prove that
/// every point inside a commit recovers through journal replay.
static EXT2_FAULT_STAGE: AtomicU8 = AtomicU8::new(0);
aster_cmdline::define_kv_param!("asterinas.ext4_fault_stage", EXT2_FAULT_STAGE);

/// Stops the machine at `stage` when fault injection requests it.
///
/// The panic path halts every CPU, which is the closest in-guest equivalent of
/// pulling the power. The device state at this instant is exactly what the
/// journal must be able to recover.
pub(super) fn fault_point(stage: u8) {
    if EXT2_FAULT_STAGE.load(Ordering::Relaxed) == stage {
        ostd::warn!("ext2: fault injection at stage {}", stage);
        panic!("ext2 fault injection at journal stage {}", stage);
    }
}

/// Registers the ext2 filesystem type with the VFS registry.
pub(super) fn init() {
    registry::register(&EXT2_TYPE).unwrap();
    registry::register(&EXT4_TYPE).unwrap();
}
