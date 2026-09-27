#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if ! grep -q '^/dev/vda /ext2 ext2 ' /proc/mounts; then
    echo 'ASTERINAS_EXT2_CUT_ERROR ext2_is_not_mounted'
    cat /proc/mounts
    exit 1
fi

exec /test/fs/ext2/durability_cut "$1"
