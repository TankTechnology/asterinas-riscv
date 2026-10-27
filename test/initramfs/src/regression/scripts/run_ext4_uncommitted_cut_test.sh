#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if [ "$(cat /tmp/asterinas_requested_fs_type 2>/dev/null || true)" != ext4 ]; then
    echo 'ASTERINAS_EXT4_RECOVERY_ERROR ext4_is_not_mounted'
    exit 1
fi

base=/ext2/asterinas_ext4_recovery
mkdir -p "$base"
printf 'uncommitted-ext4-v1\n' > "$base/uncommitted.tmp"
echo 'ASTERINAS_EXT4_RECOVERY_CUT_READY stage=before_fsync'
sleep 600
