#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if ! grep -q '^/dev/vda /ext2 ext2 ' /proc/mounts; then
    echo 'ASTERINAS_EXT2_RENAME_SAME_INODE_ERROR ext2_is_not_mounted'
    exit 1
fi

/test/fs/ext2/rename_same_inode
sync
umount /ext2
echo 'ASTERINAS_EXT2_RENAME_SAME_INODE_OK'
