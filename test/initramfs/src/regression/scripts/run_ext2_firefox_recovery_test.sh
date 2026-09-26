#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

echo "ASTERINAS_EXT2_FIREFOX_RECOVERY_STAGE stage=workload-start"
/test/fs/ext2/firefox_state
echo "ASTERINAS_EXT2_FIREFOX_RECOVERY_STAGE stage=sync-start"
sync
echo "ASTERINAS_EXT2_FIREFOX_RECOVERY_STAGE stage=unmount-start"
umount /ext2
echo "ASTERINAS_EXT2_FIREFOX_RECOVERY_OK cycles=16"
