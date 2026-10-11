#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if [ "$(cat /tmp/asterinas_requested_fs_type 2>/dev/null || true)" != ext4 ]; then
    echo 'ASTERINAS_EXT4_REGRESSION_ERROR ext4_is_not_mounted'
    cat /proc/mounts
    exit 1
fi
echo 'ASTERINAS_EXT4_REGRESSION_STAGE mounted-request=ext4'

echo 'ASTERINAS_EXT4_REGRESSION_STAGE basic-operations'
base=/ext2/asterinas_ext4_gate
mkdir "$base"
printf 'one\n' > "$base/file"
printf 'two\n' >> "$base/file"
test "$(wc -c < "$base/file")" -eq 8
truncate -s 8192 "$base/file"
test "$(stat -c %s "$base/file")" -eq 8192
mv "$base/file" "$base/renamed"
chmod 640 "$base/renamed"
test "$(stat -c %a "$base/renamed")" = 640
test -f "$base/renamed"

echo 'ASTERINAS_EXT4_REGRESSION_STAGE extent-growth'
extent="$base/extent-growth"
truncate -s 0 "$extent"
for offset in 0 1048576 2097152 4194304 8388608 16777216 33554432; do
    dd if=/dev/zero of="$extent" bs=4096 count=1 seek=$((offset / 4096)) conv=notrunc >/dev/null 2>&1
done
test "$(stat -c %s "$extent")" -eq 33558528
dd if=/dev/zero of="$extent" bs=4096 count=1 seek=3 conv=notrunc >/dev/null 2>&1
test "$(stat -c %s "$extent")" -eq 33558528
rm -f "$extent"

echo 'ASTERINAS_EXT4_REGRESSION_STAGE syscall-suite'
cd /test/fs
./ext2/file_io
./ext2/namei
./ext2/open_dir
./ext2/open_unlink
./ext2/permissions
./ext2/readdir
./ext2/rename
./ext2/rmdir
./ext2/short_rw
./ext2/sparse
./ext2/symlink
./ext2/xattr

sync
echo 'ASTERINAS_EXT4_REGRESSION_OK operations=basic,extent,syscalls'
