#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail

usage() {
    echo "usage: $0 BASE_INITRAMFS OUTPUT_INITRAMFS BENCHMARK [--no-block|--block] [MODULE_ROOT] [FS_TYPE]" >&2
    exit 2
}

[[ $# -ge 3 && $# -le 6 ]] || usage
base=$1
output=$2
benchmark=$3
mount_block=1
if [[ ${4:-} == --no-block ]]; then
    mount_block=0
elif [[ ${4:-} == --block ]]; then
    mount_block=1
elif [[ $# -eq 4 ]]; then
    usage
fi
module_root=${5:-}
if [[ $# -ge 5 && ${4:-} != --no-block && ${4:-} != --block ]]; then
    usage
fi
fs_type=${6:-ext2}
case $fs_type in
    ext2|ext4) ;;
    *) echo "unsupported filesystem type: $fs_type" >&2; exit 1 ;;
esac
[[ -r $base ]] || { echo "base initramfs is not readable: $base" >&2; exit 1; }
[[ $benchmark = /* ]] || { echo "benchmark must be an absolute path" >&2; exit 1; }
command -v cpio >/dev/null || { echo "cpio is required" >&2; exit 1; }
command -v gzip >/dev/null || { echo "gzip is required" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
gzip -dc "$base" | (cd "$work" && cpio -idm --quiet)
if [[ -n $module_root ]]; then
    [[ -d $module_root/usr/lib/modules ]] || {
        echo "module root has no usr/lib/modules: $module_root" >&2
        exit 1
    }
    mkdir -p "$work/lib"
    cp -a "$module_root/usr/lib/modules" "$work/lib/"
fi

cat >"$work/init" <<EOF
#!/bin/sh
set -eu
mount -t proc proc /proc
mount -t sysfs sysfs /sys
if [ "$mount_block" -eq 1 ]; then
    mount -t devtmpfs devtmpfs /dev
    if [ -d /lib/modules ]; then
        modprobe virtio_mmio 2>/dev/null || true
        modprobe virtio_pci 2>/dev/null || true
        modprobe virtio_blk 2>/dev/null || true
        modprobe crc32c_generic 2>/dev/null || true
        modprobe ext4 2>/dev/null || true
        modprobe ext2 2>/dev/null || true
    fi
    mkdir -p /ext2
    mount -t "$fs_type" /dev/vda /ext2
fi
echo "Linux initramfs ready"
"$benchmark"
status=\$?
echo "Linux benchmark exit status: \$status"
poweroff -f
halt -f
sleep 3600
EOF
chmod 0755 "$work/init"

mkdir -p "$(dirname "$output")"
tmp_output="$output.part"
(cd "$work" && find . -print0 | cpio -o -H newc --null --quiet | gzip -n >"$tmp_output")
mv "$tmp_output" "$output"
