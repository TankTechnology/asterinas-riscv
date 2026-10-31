# SPDX-License-Identifier: MPL-2.0

# The ext4 compatibility profile deliberately supports extents and an internal
# journal, but not metadata checksums, 64-bit high block numbers, flex_bg, or
# orphan-file replay.  Keep the xfstests images inside that profile; otherwise
# mount failures would describe a mkfs mismatch rather than a kernel bug.
XFSTESTS_NEEDS_BLOCK_DEVICES := true
XFSTESTS_MKFS := mke2fs -q -F -t ext4 -O ^metadata_csum,^64bit,^flex_bg,^orphan_file,^huge_file,^dir_nlink,^extra_isize
