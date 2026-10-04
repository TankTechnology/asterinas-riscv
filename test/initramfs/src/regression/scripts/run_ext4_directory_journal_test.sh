#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if [ "$(cat /tmp/asterinas_requested_fs_type 2>/dev/null || true)" != ext4 ]; then
    echo 'ASTERINAS_EXT4_DIRECTORY_JOURNAL_ERROR ext4_is_not_mounted'
    exit 1
fi

base=/ext2/asterinas_ext4_directory_journal
rm -rf "$base"
mkdir "$base" "$base/a" "$base/b"

# Exercise directory-block reuse, growth, same-directory rename, cross-directory
# rename, and replacement while inode and allocation metadata are dirty.
i=0
while [ "$i" -lt 96 ]; do
    printf 'payload-%s\n' "$i" > "$base/a/file-$i"
    i=$((i + 1))
done
mv "$base/a/file-0" "$base/a/renamed-0"
mv "$base/a/file-1" "$base/b/moved-1"
printf 'replacement\n' > "$base/b/replaced"
mv -f "$base/a/file-2" "$base/b/replaced"

test -f "$base/a/renamed-0"
test -f "$base/b/moved-1"
test "$(cat "$base/b/replaced")" = "payload-2"
sync

rm -rf "$base"
sync
echo 'ASTERINAS_EXT4_DIRECTORY_JOURNAL_OK operations=create,delete,rename,growth,sync'
