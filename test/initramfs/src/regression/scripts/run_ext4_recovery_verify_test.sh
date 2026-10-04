#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if [ "$(cat /tmp/asterinas_requested_fs_type 2>/dev/null || true)" != ext4 ]; then
    echo 'ASTERINAS_EXT4_RECOVERY_ERROR ext4_is_not_mounted'
    exit 1
fi

if [ -e /ext2/asterinas_ext4_recovery/uncommitted.tmp ]; then
    echo 'ASTERINAS_EXT4_RECOVERY_ERROR uncommitted_file_survived'
    exit 1
fi

echo 'ASTERINAS_EXT4_RECOVERY_OK uncommitted_transaction_discarded=1'
