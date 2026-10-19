# ext2/ext4 support in the Asterinas kernel

This document records what the `kernel/core/src/fs/fs_impls/ext2` implementation
supports today, where it stops, and which gates keep the supported surface
honest. It is an inventory, not a specification: the Debian root image is
created with the feature set below, so an image from a default `mke2fs` may not
mount.

The same implementation serves both filesystem names:

- `ext2` mounts images without a journal (`allow_journal = false`).
- `ext4` accepts the internal journal and the extent features (`allow_journal = true`).

## Mount-time compatibility

Validation lives in `super_block.rs` (`SuperBlock::try_from_with_journal`).

| Property | Accepted | Rejected |
|---|---|---|
| Block size | 4096 only | 1024, 2048 |
| Creator OS | Linux | everything else |
| `errors=` behavior | `continue` | `remount-ro`, `panic` |
| Revision | 0 (GoodOld), 1 (Dynamic) | other values |
| Inode size | 128..=4096, power of two | smaller, larger, non-power-of-two |
| Incompat features | `FILETYPE`, `EXTENTS`, `RECOVER` | `64BIT`, `META_BG`, `FLEX_BG`, `INLINE_DATA`, `MMP`, ... |
| RO-compat features | `SPARSE_SUPER`, `LARGE_FILE`, `BTREE_DIR` | `METADATA_CSUM`, `QUOTA`, `HUGE_FILE`, `GDT_CSUM`, ... |
| Compat features | not validated (unknown bits are ignored) | - |
| Fragments | fragment size must equal block size | - |

Consequences: images with `metadata_csum`, 64-bit block numbers, `flex_bg`, or
`inline_data` cannot be mounted. The Debian builder therefore calls `mke2fs`
with those features disabled; `build_rootfs.sh` also runs `e2fsck -fy` and
requires a clean `e2fsck -fn` before publishing an image.

## Feature matrix

Legend: ✅ supported, 🟡 partial (limits noted), ❌ not implemented.

### Data and block mapping

| Feature | Status | Notes |
|---|---|---|
| Buffered read/write, page cache | ✅ | data=ordered style writeback |
| Sparse files and holes | ✅ | holes read as zeros; allocation fills them |
| Legacy indirect mapping | ✅ | 12 direct + single/double/triple indirect |
| Extent read | 🟡 | depth 0 and depth 1 index trees |
| Extent allocation (write) | 🟡 | depth-0 inline trees only, append-only, at most 4 extents; new files use legacy mapping |
| Extent writeback of existing ranges | 🟡 | depth 0 and 1 |
| Extent truncate | 🟡 | depth 0 and 1; deeper trees are left untouched and logged |
| 64-bit extents | ❌ | rejected as `EOPNOTSUPP` |
| `fallocate` | 🟡 | `Allocate` and `AllocateKeepSize`; punch-hole and range modes are `EOPNOTSUPP` |
| `O_TMPFILE` | ❌ | VFS default returns `EOPNOTSUPP` |
| File size | ✅ | up to 2,196,873,666,560 bytes with 4 KiB blocks (indirect-mapped limit) |

Because new files are created without `EXTENTS`, ordinary workloads allocate
through the indirect-block path even on an ext4 image. Files that arrive with
extent trees (the Debian image has several, e.g. `libLLVM`) can be read,
written in place, and truncated; appending to them beyond the inline extents
returns `EOPNOTSUPP`.

### Directories

| Feature | Status | Notes |
|---|---|---|
| Linear directories | ✅ | `file_type` entries, lookups, create, unlink, rename |
| Htree (`INDEX_DIR`) directories | 🟡 | converted to linear on the first insertion; the index is not maintained |
| `rec_len` compaction on delete | ✅ | |
| `RENAME_EXCHANGE` | ❌ | `EINVAL` |
| Directory growth | ✅ | one block at a time |

The htree conversion repacks live entries into the existing blocks, clears
`INDEX_DIR` and keeps the inode size. Lookups in those directories become
linear; `e2fsck -D` can rebuild an index later.

### Inodes, links, special files

| Feature | Status | Notes |
|---|---|---|
| Regular files, directories | ✅ | |
| Fast symlinks | ✅ | target up to 60 bytes, stored in `i_block` |
| Block-backed symlinks | ✅ | conversion in both directions |
| Hard links | ✅ | link count and `i_blocks` accounting |
| `mknod` (char/block device, FIFO) | ✅ | device id encoded in `i_block` |
| Sockets on disk | ✅ | as an inode type |
| Ownership, mode, timestamps | 🟡 | uid/gid/mode full; timestamps have one-second resolution |
| `i_blocks` accounting | ✅ | data + tree + xattr blocks; verified by the storage gate |

### Extended attributes

| Feature | Status | Notes |
|---|---|---|
| `user.*`, `trusted.*`, `security.*` | ✅ | one xattr block per inode |
| External value blocks (`i_file_acl` pointing to EA blocks) | ❌ | `EIO` on read |
| POSIX ACLs (`system.posix_acl_*`) | ❌ | namespace maps, semantics not implemented |
| xattr block sharing | ❌ | reference count is always 1 |
| xattr capacity | 🟡 | value must fit the single xattr block |

### Journal (JBD2)

| Feature | Status | Notes |
|---|---|---|
| Internal journal | ✅ | external journals rejected |
| Journal superblock | 🟡 | v2 only; 32-bit tags; no checksum or 64-bit tag layout |
| Writing metadata transactions | ✅ | descriptor + data + optional revoke + commit, then home blocks and checkpoint |
| Replay after a crash | 🟡 | one descriptor per transaction; revoke-only transactions and deleted tags are `EOPNOTSUPP` |
| Journal inode geometry | 🟡 | must be contiguous for replay |
| `noload` mount option | ✅ | skips journal replay |

`fsync` and every metadata commit go through the journal and end with a block
device flush; `sync` commits the whole filesystem (full inode-table flush),
which is heavier than Linux's per-inode writeback but keeps ordering simple.

### Allocation and mount behavior

| Feature | Status | Notes |
|---|---|---|
| Block/inode bitmap allocation | ✅ | per block group; no `flex_bg` |
| Fragmentation-aware placement | ❌ | simple goal-based allocation; TODO in `block_group.rs` |
| Read-only remount and journal finalization | ✅ | clears `RECOVER` on a clean shutdown |
| Crash recovery | ✅ | validated with `asterinas.ext4_fault_stage` fault injection |
| `bsddf` / `minixdf` | ✅ | `statfs` block accounting mode |

## What this means for real package workloads

The current gates cover the paths package management exercises:

- `tools/x86/debian/persistence_acceptance.sh` — repeated boots, writes from
  several boots, crash replay, `e2fsck -fn`.
- `tools/x86/debian/apt_acceptance.sh` — `apt-get update`, `apt-get install`,
  dpkg unpack into htree directories, `fsync`, reboot, verify, `e2fsck -fn`.

Known gaps that a package workload can still hit:

1. **Extent files that need to grow or take a hole.** Allocation is
   append-only at depth 0 for inline trees. A large pre-existing extent file
   that is extended past its inline capacity, or written into a hole, fails
   with `EOPNOTSUPP`. Most Debian files are small or indirect-mapped, which is
   why installation works today.
2. **`metadata_csum` images.** Any image produced by default `mke2fs` on a
   modern distribution is rejected at mount unless the builder disables the
   feature.
3. **POSIX ACLs and xattrs larger than one block.**
4. **Journal edge cases:** revoke-only transactions, deleted tags, and
   non-contiguous journal inodes.
5. **`O_TMPFILE`, `RENAME_EXCHANGE`, punch-hole fallocate.**

The next storage milestone is therefore not "more fsck coverage" but extending
extent allocation to depth-1 trees (existing and new holes), which is the main
remaining blocker for arbitrary package upgrades and file rewrites.

## Validation

- 54 in-kernel ktests in `ext2/` cover superblock validation, block-pointer
  trees, directory entries, truncation, xattr layout and sync ordering.
- Host gates: `test_x86_debian_storage` and `test_x86_debian_apt`.
- Every gate checks `e2fsck -fn` after a clean shutdown; crash tests replay the
  journal from a fault-injected boot.
