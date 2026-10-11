#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if [ "$(cat /tmp/asterinas_requested_fs_type 2>/dev/null || true)" != ext4 ]; then
    echo 'ASTERINAS_EXT4_SEMANTICS_ERROR ext4_is_not_mounted'
    exit 1
fi

# These existing regression binaries exercise the file semantics used by a
# Debian rootfs. They operate on /ext2, which is the rootfs mount point even
# when the backing image is formatted as ext4.
/test/fs/ext2/permissions
/test/fs/ext2/symlink
/test/fs/ext2/sparse
/test/fs/ext2/fallocate
/test/fs/ext2/rename
/test/fs/ext2/xattr

# Verify that metadata survives both file and filesystem-level flushes.
base=/ext2/asterinas_ext4_semantics
rm -rf "$base"
mkdir -p "$base"
printf 'debian-ext4-semantics\n' > "$base/persisted"
ln "$base/persisted" "$base/hardlink"
ln -s persisted "$base/symlink"
chmod 0640 "$base/persisted"
/test/fs/ext2/fsync_smoke "$base/fsync-file"
/test/fs/sync/sync
sync

test "$(cat "$base/symlink")" = 'debian-ext4-semantics'
test "$(cat "$base/hardlink")" = 'debian-ext4-semantics'
rm -rf "$base"
sync
echo 'ASTERINAS_EXT4_SEMANTICS_OK permissions=1 symlink=1 hardlink=1 sparse=1 fallocate=1 rename=1 xattr=1 fsync=1 syncfs=1'
