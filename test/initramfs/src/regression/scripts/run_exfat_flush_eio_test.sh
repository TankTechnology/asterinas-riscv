#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if ! grep -q '^/dev/vdb /exfat exfat ' /proc/mounts; then
    echo 'ASTERINAS_EXFAT_FLUSH_EIO_ERROR exfat_is_not_mounted'
    cat /proc/mounts
    exit 1
fi

/test/fs/sync/exfat_flush_eio "$1"
if [ "$1" = clean ] || [ "$1" = verify ]; then
    umount /exfat
    if [ "$1" = clean ]; then
        echo 'ASTERINAS_EXFAT_SYNC_CLEAN_OK'
    else
        echo 'ASTERINAS_EXFAT_SYNC_VERIFY_OK'
    fi
fi
