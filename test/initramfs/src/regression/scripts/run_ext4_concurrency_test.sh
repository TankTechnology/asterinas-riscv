#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if [ "$(cat /tmp/asterinas_requested_fs_type 2>/dev/null || true)" != ext4 ]; then
    echo 'ASTERINAS_EXT4_CONCURRENCY_ERROR ext4_is_not_mounted'
    exit 1
fi

base=/ext2/asterinas_ext4_concurrency
rm -rf "$base"
mkdir -p "$base/shared" "$base/left" "$base/right"

worker() {
    worker_id=$1
    i=0
    while [ "$i" -lt 64 ]; do
        stem="$base/shared/w${worker_id}-${i}"
        printf 'worker=%s iteration=%s\n' "$worker_id" "$i" > "$stem.tmp"
        mv "$stem.tmp" "$stem"
        if [ $((i % 2)) -eq 0 ]; then
            mv "$stem" "$stem.renamed"
        fi
        if [ $((i % 4)) -eq 0 ]; then
            rm "$stem.renamed"
        fi
        i=$((i + 1))
    done

    i=0
    while [ "$i" -lt 32 ]; do
        mkdir "$base/left/w${worker_id}-${i}"
        mv "$base/left/w${worker_id}-${i}" "$base/right/w${worker_id}-${i}"
        rmdir "$base/right/w${worker_id}-${i}"
        i=$((i + 1))
    done
}

worker 0 & p0=$!
worker 1 & p1=$!
worker 2 & p2=$!
worker 3 & p3=$!
wait "$p0"
wait "$p1"
wait "$p2"
wait "$p3"

remaining=$(ls "$base/shared" | wc -l)
test "$remaining" -eq 192
/test/fs/ext2/fsync_smoke "$base/fsync-file"
/test/fs/sync/sync
sync

rm -rf "$base"
sync
echo 'ASTERINAS_EXT4_CONCURRENCY_OK workers=4 iterations=64 rename=rmdir fsync=1 syncfs=1'
