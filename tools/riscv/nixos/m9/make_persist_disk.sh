#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# M9 optional bonus: create the ext2 data disk that persists /nix across
# reboots. The image is attached as a *second* virtio-blk device (/dev/vdb) by
# boot_m9_persist_smoke.py, and /init mounts it at /nix (see init_m9.c).
#
# Idempotent: leaves an existing image untouched unless --force is passed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
DISK="${REPO_ROOT}/target/nixos/m9/nix-store.ext2"
SIZE_MB=256

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ -s "${DISK}" && "${FORCE}" -eq 0 ]]; then
    echo "exists: ${DISK} (pass --force to recreate)"
    exit 0
fi

mkdir -p "$(dirname "${DISK}")"
truncate -s "${SIZE_MB}M" "${DISK}"
# The Asterinas ext2 driver requires 4096-byte blocks (fs.rs checks
# `log_block_size == 2`); mke2fs otherwise picks 1024 for a small volume.
mkfs.ext2 -q -F -b 4096 "${DISK}"
echo "created ${DISK} (${SIZE_MB}M ext2, 4096-byte blocks)"
