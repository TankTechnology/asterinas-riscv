#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if ! grep -q '^/dev/vda /ext2 ext2 ' /proc/mounts; then
    echo 'ASTERINAS_EXT2_CUT_ERROR ext2_is_not_mounted'
    cat /proc/mounts
    exit 1
fi
echo 'ASTERINAS_EXT2_CUT_MOUNT_OK source=/dev/vda target=/ext2'
mkdir /ext2/asterinas_after_sync_cut
printf 'renamed file survived sync\n' > /ext2/asterinas_after_sync_cut/payload.tmp
mv /ext2/asterinas_after_sync_cut/payload.tmp /ext2/asterinas_after_sync_cut/payload
sync
echo 'ASTERINAS_EXT2_CUT_READY stage=after_sync'
# The host stops QEMU at this marker, before the init process can unmount.
sleep 600
