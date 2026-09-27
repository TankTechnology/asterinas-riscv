#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if ! grep -q '^/dev/vda /ext2 ext2 ' /proc/mounts; then
    echo 'ASTERINAS_EXT2_FIREFOX_RECOVERY_ERROR ext2_is_not_mounted'
    cat /proc/mounts
    exit 1
fi

echo "ASTERINAS_EXT2_FIREFOX_RECOVERY_STAGE stage=workload-start"
/test/fs/ext2/firefox_state
printf 'asterinas-ext2-recovery-v1\n' > /ext2/asterinas_recovery_target
echo "ASTERINAS_EXT2_FIREFOX_RECOVERY_STAGE stage=sync-start"
sync
echo "ASTERINAS_EXT2_FIREFOX_RECOVERY_STAGE stage=unmount-start"
umount /ext2
echo "ASTERINAS_EXT2_FIREFOX_RECOVERY_OK cycles=16"
