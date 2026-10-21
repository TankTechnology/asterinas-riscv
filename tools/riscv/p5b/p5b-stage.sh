#!/bin/bash
# SPDX-License-Identifier: MPL-2.0
#
# Stage the P5b vendor UMD bundle onto the Megrez Debian rootfs without
# touching the Asterinas initramfs.
#
# The bundle is intentionally kept outside the repository (vendor binaries):
#   /mnt/shared/xaj/vendor/p5b-rootfs-20261023/opt/pvr/{bin,lib,dri}
#
# RockOS and the Asterinas guest do NOT share a root filesystem on Megrez:
# RockOS is /dev/mmcblk1p3 and the guest is /dev/mmcblk1p2 (ASTER_BROWSERWEB).
# After this script stages /home/debian/pvr, inject it from RockOS:
#   mount /dev/mmcblk1p2 /mnt/aster_guest
#   mkdir -p /mnt/aster_guest/opt/pvr
#   cp -r /home/debian/pvr/. /mnt/aster_guest/opt/pvr/
#   umount /mnt/aster_guest
# Then run the guest script with SRC=/opt/pvr.
# If guest networking is configured, the HTTP fallback is also available:
#   tools/riscv/p5b/p5b-stage.sh --http

set -euo pipefail

PVR_ROOTFS=${PVR_ROOTFS:-/mnt/shared/xaj/vendor/p5b-rootfs-20261023}
BOARD=${BOARD:-debian@10.100.19.200}
DEST=${DEST:-/home/debian/pvr}
HOST_IP=${HOST_IP:-10.100.19.216}
HTTP_PORT=${HTTP_PORT:-8000}
HTTP_DIR=${HTTP_DIR:-/tmp/p5b-http}
BUNDLE="$PVR_ROOTFS/opt/pvr"

if [[ ! -x "$BUNDLE/bin/eglconfigs" ]]; then
    echo "missing bundle at $BUNDLE" >&2
    exit 2
fi
(
    cd "$PVR_ROOTFS"
    timeout 60 sha256sum -c SHA256SUMS
)

if [[ "${1:-}" == "--http" ]]; then
    mkdir -p "$HTTP_DIR"
    timeout 120 tar -C "$PVR_ROOTFS/opt" -czf "$HTTP_DIR/pvr-rootfs.tar.gz" pvr
    echo "serving $HTTP_DIR/pvr-rootfs.tar.gz on http://$HOST_IP:$HTTP_PORT/"
    echo "guest commands:"
    echo "  mkdir -p /tmp/pvr && cd /tmp/pvr"
    echo "  curl --noproxy '*' -o pvr-rootfs.tar.gz http://$HOST_IP:$HTTP_PORT/pvr-rootfs.tar.gz"
    echo "  tar -xzf pvr-rootfs.tar.gz"
    echo "  sh /path/to/p5b-guest.sh /tmp/pvr/pvr"
    echo "host server: python3 -m http.server $HTTP_PORT --bind $HOST_IP --directory $HTTP_DIR"
    exit 0
fi

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)
echo "staging $BUNDLE -> $BOARD:$DEST"
timeout 30 ssh "${SSH_OPTS[@]}" "$BOARD" "mkdir -p '$DEST'"
timeout 300 scp -r "${SSH_OPTS[@]}" "$BUNDLE"/* "$BOARD:$DEST/"
timeout 30 ssh "${SSH_OPTS[@]}" "$BOARD" "sha256sum '$DEST'/bin/eglconfigs '$DEST'/lib/*.so '$DEST'/dri/pvr_dri.so"

cat <<EOF

Staged to RockOS.  RockOS and the Asterinas guest do not share /home:
inject the files into the guest root partition (/dev/mmcblk1p2) at /opt/pvr
before running tools/riscv/p5b/p5b-guest.sh with SRC=/opt/pvr.

No initramfs file was modified.
EOF