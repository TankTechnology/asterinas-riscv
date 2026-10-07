#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail

[[ $# -ge 6 && $# -le 7 ]] || {
    echo "usage: $0 CONTAINER BASE_ROOTFS SYSTEM KERNEL INITRAMFS TEMP_ROOTFS [LABEL]" >&2
    exit 2
}
container=$1
base_rootfs=$2
system=$3
kernel=$4
initramfs=$5
temp_rootfs=$6
label=${7:-Simple stat}

docker exec "$container" cp --reflink=auto "$base_rootfs" "$temp_rootfs"
exec "$(dirname "$0")/run_lmbench_sample.sh" "$container" "$system" \
    "$kernel" "$initramfs" "$temp_rootfs" "$label"
