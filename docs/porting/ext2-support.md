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
| Incompat features | `FILETYPE`, `EXTENTS`, `RECOVER`, `64BIT`, `FLEX_BG` | `META_BG`, `INLINE_DATA`, `MMP`, ... |
| RO-compat features | `SPARSE_SUPER`, `LARGE_FILE`, `BTREE_DIR`, `HUGE_FILE`, `DIR_NLINK`, `EXTRA_ISIZE` | `METADATA_CSUM`, `GDT_CSUM`, `QUOTA`, ... |
| Compat features | not validated (unknown bits are ignored) | - |
| Fragments | fragment size must equal block size | - |
| Group descriptors | 32 bytes, or 64 bytes with `64BIT` | other sizes, non-zero high block words |

Consequences: images with `metadata_csum`, metablock groups or `inline_data`
cannot be mounted. The Debian builder therefore calls `mke2fs` with those
features disabled; `build_rootfs.sh` also runs `e2fsck -fy` and requires a
clean `e2fsck -fn` before publishing an image.

`64BIT` is supported for images whose block numbers still fit in 32 bits: the
descriptor stride becomes `s_desc_size` (at least 64 bytes), the three high
block-number words of every descriptor must be zero, and the high block-count
words of the superblock must be zero as well. Only the classic 32-byte
descriptor body is rewritten; the high words and the unused tail stay as they
are on disk. `FLEX_BG` lifts the mount-time requirement that a group's block
bitmap, inode bitmap and inode table lie inside that group's own block range:
`mke2fs` packs them into the first group of each flex group, and their
allocation is recorded by the bitmap of the group that physically holds them.
`HUGE_FILE`, `DIR_NLINK` and `EXTRA_ISIZE` are capability bits; ordinary files
keep sector-based `i_blocks` accounting and the 32-byte inode extras that
`i_extra_isize` already describes.

## Feature matrix

Legend: ✅ supported, 🟡 partial (limits noted), ❌ not implemented.

### Data and block mapping

| Feature | Status | Notes |
|---|---|---|
| Buffered read/write, page cache | ✅ | data=ordered style writeback |
| Sparse files and holes | ✅ | holes read as zeros; allocation fills them |
| Legacy indirect mapping | ✅ | 12 direct + single/double/triple indirect |
| Extent read | 🟡 | depth 0 and depth 1 index trees |
| Extent allocation (write) | 🟡 | append at the logical end of depth-0 and depth-1 trees, plus hole allocation for depth-0 (records are inserted in order); a full inline tree is promoted to a leaf, and appends go into the last leaf. Depth-1 hole writes and leaf splits are `EOPNOTSUPP`; new files use legacy mapping |
| Extent writeback of existing ranges | 🟡 | depth 0 and 1 |
| Extent truncate | 🟡 | depth 0 and 1; deeper trees are left untouched and logged |
| 64-bit extents | ❌ | rejected as `EOPNOTSUPP` |
| `fallocate` | 🟡 | `Allocate`, `AllocateKeepSize`, `ZeroRange`, `ZeroRangeKeepSize` and `PunchHoleKeepSize` (indirect mapping and extent trees up to depth one); collapse, insert and unshare are `EOPNOTSUPP` |
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
| `RENAME_NOREPLACE` | ✅ | `EEXIST` when the target exists |
| `RENAME_EXCHANGE` | ✅ | files, directories and mixed types; `..` and parent link counts follow the moved entries |
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
| Ownership, mode, timestamps | ✅ | uid/gid/mode full; nanosecond timestamps for inodes larger than 128 bytes (see below) |
| `i_blocks` accounting | ✅ | data + tree + xattr blocks; verified by the storage gate |

### Extended attributes

| Feature | Status | Notes |
|---|---|---|
| `user.*`, `trusted.*`, `security.*` | ✅ | one xattr block per inode |
| External value blocks (`i_file_acl` pointing to EA blocks) | ❌ | `EIO` on read |
| POSIX ACLs (`system.posix_acl_*`) | 🟡 | access ACLs stored and enforced (owner, named user, group class with mask, other); `chmod` keeps the mask/group/other entries in sync; default ACLs are stored but not inherited |
| xattr block sharing | ❌ | reference count is always 1 |
| xattr capacity | 🟡 | value must fit the single xattr block |

### POSIX ACLs

Both `system.posix_acl_access` and `system.posix_acl_default` are stored as
ordinary xattr entries with an empty name suffix, which is how ext4
represents them. Access decisions in `check_permission` follow Linux
`posix_acl_permission`: the owner entry is unmasked; a matching named user is
part of the group class and is masked; the group class is the union of the
owning-group entry and every matching named group entry, masked when the ACL
has a mask; everything else uses the other entry. `DAC_OVERRIDE` still
bypasses the check before the ACL is consulted.

`chmod` follows Linux's `posix_acl_chmod`: an ACL with a mask has its owner
entry, mask and other entry rewritten from the new mode bits (the group
entry is left alone); a minimal three-entry ACL has its group entry
rewritten instead. An ACL that has named entries but no mask is malformed
and is left untouched.

Not yet implemented: default ACL inheritance on create (the attribute is
stored and readable but new children do not derive an ACL from it). An
invalid ACL value fails the access check with `EIO`/`EINVAL` rather than
being ignored.

### Journal (JBD2)

| Feature | Status | Notes |
|---|---|---|
| Internal journal | ✅ | external journals rejected |
| Journal superblock | 🟡 | v2 only; 32-bit tags; no checksum or 64-bit tag layout |
| Writing metadata transactions | ✅ | descriptor + data + optional revoke + commit, then home blocks and checkpoint; directory data blocks join the transaction that describes them |
| Replay after a crash | 🟡 | one descriptor per transaction; revoke-only transactions and deleted tags are `EOPNOTSUPP` |
| Journal inode geometry | 🟡 | must be contiguous for replay |
| `noload` mount option | ✅ | skips journal replay |
| Cache coherence after replay | ✅ | replayed home blocks bypass the in-memory caches, so bitmaps, group descriptors, the superblock and inode-table pages are reloaded before the filesystem is exposed |
| Recovery marker ordering | ✅ | `s_start` is cleared only after the replayed blocks are flushed to the device |

`fsync` and every metadata commit go through the journal and end with a block
device flush; `sync` commits the whole filesystem (full inode-table flush),
which is heavier than Linux's per-inode writeback but keeps ordering simple.

Directory data blocks are metadata: the writeback backend stages them into
the next transaction instead of writing them home. Without that, a crash
between the data=ordered flush of a directory block and the metadata commit
left an entry on disk pointing at an inode whose allocation never committed
(`Entry 'f-2-204' ... has deleted/unused inode 19771`). A staged directory
block is discarded when its block is freed, so the snapshot cannot overwrite
the block's next owner.

The fault-injection stages only fire inside a commit and did not see this
window; the concurrency power-cut gate, which kills QEMU at an arbitrary
moment during mixed churn, did.

### Timestamps

The in-memory inode stores `Duration`, so the nanosecond part exists from the
moment a file is created. Large inodes (`s_inode_size > 128`) persist it in
ext4's `i_atime_extra`, `i_ctime_extra` and `i_mtime_extra` words when the
slot's `i_extra_isize` covers them. The encoding is Linux's: nanoseconds in
the upper 30 bits, two epoch bits in the low two bits to extend the 32-bit
seconds field through 2106.

A new inode has the large-inode area zeroed and `i_extra_isize = 32` written,
matching Linux's `s_want_extra_isize`. This matters for reused slots: without
it a file would inherit the previous file's `i_crtime`, version and project
id. Older 128-byte inodes keep one-second resolution, which is the format's
limit.

The same area carries the birth time: `i_crtime`/`i_crtime_extra` are decoded
into `birth_at`, so `statx` reports `STATX_BTIME` for inodes that store it,
and a newly created inode records the creation time there.

Replay correctness was hardened by the crash matrix: block groups load their
bitmaps and descriptors during mount, *before* `replay_journal` writes the
replayed home blocks directly to the device. Without an explicit reload the
first commit after mount staged the stale pre-replay bitmaps and silently
undid the recovery (freed inodes and blocks came back as leaks). The mount
path now re-reads the superblock, the group descriptor table, every group's
bitmaps and discards cached inode-table pages after a replay. The recovery
marker is also cleared only after a device flush of the replayed blocks.

### Allocation and mount behavior

| Feature | Status | Notes |
|---|---|---|
| Block/inode bitmap allocation | ✅ | per block group; `flex_bg` bitmaps and inode tables may live in another group |
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

1. **Writing into a depth-one extent hole.** Allocation is append-only, now at
   depth 0 and depth 1 (a full inline tree is promoted to a leaf), and a write
   into a depth-0 hole inserts a new in-line record in logical order. A hole in
   a depth-1 tree still needs a leaf split and reports `EOPNOTSUPP`; most
   Debian files are small or indirect-mapped, which is why installation works
   today.
2. **`metadata_csum` images.** Any image produced by default `mke2fs` on a
   modern distribution is rejected at mount unless the builder disables the
   feature. `64BIT` and `FLEX_BG` themselves are accepted.
3. **Xattrs larger than one inode.** Values that need an external value block
   are not implemented; POSIX ACLs themselves are.
4. **Journal edge cases:** revoke-only transactions, deleted tags, journal
   checksums, and non-contiguous journal inodes.
5. **Default ACL inheritance from directories.**
6. **Depth-two extent trees.**

The next storage milestone is therefore not "more fsck coverage" but extending
extent allocation to depth-1 trees (existing and new holes), which is the main
remaining blocker for arbitrary package upgrades and file rewrites.

## Validation

- 57 in-kernel ktests in `ext2/` cover superblock validation, block-pointer
  trees, directory entries, truncation, xattr layout, timestamp extras and
  sync ordering.
- Host gates: `test_x86_debian_storage` and `test_x86_debian_apt`.
- `fs_ext4_features_check.sh` (`make test_x86_debian_fs_ext4_features`)
  converts the Debian image to 64-byte descriptors and `FLEX_BG`, and mounts a
  fresh `mke2fs -O 64bit,flex_bg` image as a second disk. It checks the feature
  words and descriptor high words before and after every boot, requires at
  least one cross-group metadata pointer in the fresh image, re-reads every
  written file after a remount, and runs `e2fsck -fn` on both images.
- Every gate checks `e2fsck -fn` after a clean shutdown; crash tests replay the
  journal from a fault-injected boot.

### Crash and durability matrices

Two host matrices exercise the persistence contract directly. Both boot a
fresh copy of the Debian image per cell, so a failure never contaminates the
next cell, and both end with `e2fsck -fn`.

`fs_crash_matrix.sh` (`make test_x86_debian_fs_crash`) crashes *inside* a
metadata commit. The first boot uses Stage1's `--root-init-path` to run a
deterministic probe as `init` (no systemd, so no unrelated commit can consume
the fault) and arms `asterinas.ext4_fault_stage=N`:

| Stage | Crash point |
|---|---|
| 1 | after the journal commit record, before the home blocks |
| 2 | halfway through the home blocks |
| 3 | after the home blocks, before the checkpoint |

The second boot is a normal systemd boot; a verifier checks the operation's
invariant. An operation may be present or absent after recovery (only the
commit record decides), but it must never be half-applied and the image must
need no repair:

| Operation | Accepted states |
|---|---|
| create | file absent, or complete with the exact contents |
| unlink | file absent, or intact |
| rename | exactly one of the two names exists, with the exact contents |
| truncate | size 0, or the original 65536 bytes intact |

Result (`create unlink rename truncate` × stages 1–3, 12 cells): **all pass**,
`e2fsck -fn` clean in every cell. Every cell recovered to the *applied* state
because the fault fires after the commit record, which is the correct JBD2
outcome.

`fs_durability_matrix.sh` (`make test_x86_debian_fs_durability`) crashes
*after* fsync returned. Each cell writes, fsyncs, and — for operations that
change a directory — fsyncs the parent directory as well, prints a READY
marker, and the host `kill -9`s QEMU two seconds later. After a normal
recovery boot the operation must be present in full:

| Scenario | Invariant after the power cut |
|---|---|
| write | file exists with the exact 4096 bytes |
| rename | old name gone, new name present with the contents |
| unlink | file gone |
| truncate | size 0 |
| dir-entry | file exists with the exact 4096 bytes and its entry survived |
| syncfs | a plain write followed by one `syncfs` survives |
| o-sync | a write through an `O_SYNC` handle survives without an explicit fsync |

Result: **7/7 pass**, `e2fsck -fn` clean.

`fs_extent_alloc_check.sh` (`make test_x86_debian_fs_extents`) appends
five pages to `/etc/hostname`, which the image creates with one inline
extent. The first three appends fill the inline array, the fourth must
promote the tree to depth one and the fifth allocates inside the leaf; the
host then requires `debugfs dump_extents` to report `0/ 1`, the contents to
survive a remount, and `e2fsck -fn` to pass. Result: **PASS**, with the
on-disk tree reporting `0/ 1 1/ 1 0-5 <leaf> 6`.

`fs_fallocate_check.sh` (`make test_x86_debian_fs_fallocate`) covers
`ZERO_RANGE` over an existing range, past EOF, and with `KEEP_SIZE`, plus
plain allocation with and without `KEEP_SIZE`; it also pins the punch-hole
contract (`EOPNOTSUPP` and an unchanged file).  The image is remounted before
the contents are re-checked.  Result: **PASS**, `e2fsck -fn` clean.

`fs_acl_check.sh` (`make test_x86_debian_fs_acl`) stores an access ACL on two
files whose mode bits alone deny every non-root access, then runs reads and
writes as the owner, a named user, a named group and unrelated users. It
checks that named users are masked, the group class applies, other matches
nothing and root bypasses, and that `chmod` rewrites the mask (or the group
entry of a minimal ACL) consistently with the new mode; it re-runs every
decision after a remount and requires `e2fsck -fn`.  Result: **PASS**.

`fs_punch_hole_check.sh` (`make test_x86_debian_fs_punch_hole`) covers holes
inside the direct blocks, across the direct/single-indirect boundary, up to
EOF (size unchanged), and a punch covering a whole double-indirect file,
which must free every data block and both indirect levels.  It also appends
several pages to the image's inline-extent `/etc/hostname`: the first punch
splits a two-block extent, three further appends overflow the in-line array
and promote the tree to depth one, and a final punch removes an extent from
the leaf.  `debugfs dump_extents` must still report `0/ 1`, the gaps read as
zeros after a remount while untouched blocks keep their patterns, and a
misaligned offset returns `EINVAL`.  Result: **PASS**, `e2fsck -fn` clean;
the measured block count went from 520 to 392 sectors for a 16-block hole.

The first depth-one attempt failed with `EUCLEAN`: the extent allocator
required logical records to be contiguous, but a punched hole legitimately
leaves a gap.  The validator now requires strictly increasing records and
rejects only overlaps.

Writing into a hole now allocates there for depth-zero trees: a new record
is inserted in logical order, bounded by the next extent and by the free
in-line slots.  The gate writes into the freshly punched block and checks
the result after the remount.  Depth-one hole writes still need leaf splits
and report `EOPNOTSUPP`.

The gate found that `AllocateKeepSize` had been allocating blocks beyond
`i_size`.  An ext2 file uses the legacy indirect mapping, which has no way to
mark such blocks as unwritten, so `e2fsck` reports `i_size is 0, should be
8192`.  Keep-size allocation is now clamped to the part of the range the file
already covers: the range is allocated and zeroed where it is visible, and a
range that lies entirely past EOF is a no-op.  This is a deliberate
difference from ext4's preallocation; the legacy layout cannot express it
without corrupting the accounting.

`fs_renameat2_check.sh` (`make test_x86_debian_fs_renameat2`) covers the
rename flags: `RENAME_EXCHANGE` within one directory, across directories,
with a directory on either side (including a directory and a file), and the
resulting `..` entries and parent link counts; `RENAME_NOREPLACE` success and
`EEXIST`; `ENOENT` for an exchange with a missing target and `EINVAL` for
`NOREPLACE|EXCHANGE`.  The image is remounted and the tree re-checked, so the
gate proves the result was persisted.  Result: **PASS**, `e2fsck -fn` clean.

The crash matrix is what exposed the replay cache-coherence bug fixed in this
change: see the journal table above. Before the fix, a replayed unlink leaked
the inode and its block because the next commit re-staged the pre-replay
bitmap.

`fs_concurrency_stress.sh` (`make test_x86_debian_fs_concurrency`) checks the
interleaving contract. Eight guest workers churn separate directories and one
shared directory with create, rewrite, unlink, rename, truncate and xattr
operations, each keeping an on-disk manifest of the files it expects and their
SHA-256 sums; every 17 operations a worker reads a random owned file back to
catch inode-reuse cross-talk. When the workers finish, the driver compares
every directory against its manifest, re-hashes every file, and checks that
the shared directory contains exactly the files the workers left behind.

- Clean run: 8 workers, 4,242 operations, 624 surviving files, **ok=True**,
  `e2fsck -fn` clean.
- Crash run (`--crash-after 20`): the host kills QEMU 20 seconds into the
  churn. The same image must replay its journal, reach multi-user and power
  off, with `e2fsck -fn` clean. **PASS**.

The crash run deliberately does not check contents: the manifests are only
finalised when a worker finishes, so an interrupted worker's files have no
recorded expectation. It checks the structural contract instead — no
deadlock, no half-applied directory operation after replay, no leaked blocks
or inodes.
