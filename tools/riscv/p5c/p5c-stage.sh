#!/bin/bash
# SPDX-License-Identifier: MPL-2.0
#
# Stage the P5c PVR/glamor increment bundle onto the Megrez Debian rootfs.
# Does not touch the Asterinas initramfs or the kernel.
#
# Bundle (vendor binaries, kept outside the repository):
#   /mnt/shared/xaj/vendor/p5c-rootfs-20261023/
#     opt/pvr/{bin,lib,dri}
#     etc/powervr.ini
#     etc/X11/xorg.conf.d/{10-pvr.conf,11-pvr-glamor.conf}

set -euo pipefail

PVR_ROOTFS=${PVR_ROOTFS:-/mnt/shared/xaj/vendor/p5c-rootfs-20261023}
BOARD=${BOARD:-debian@10.100.19.200}
DEST=${DEST:-/home/debian/pvr-p5c}
HOST_IP=${HOST_IP:-10.100.19.216}
HTTP_PORT=${HTTP_PORT:-8000}
HTTP_DIR=${HTTP_DIR:-/tmp/p5c-http}

if [[ ! -x "$PVR_ROOTFS/opt/pvr/bin/eglconfigs" ]]; then
    echo "missing P5c bundle at $PVR_ROOTFS" >&2
    exit 2
fi
if [[ ! -f "$PVR_ROOTFS/etc/X11/xorg.conf.d/11-pvr-glamor.conf" ]] ||
   [[ ! -f "$PVR_ROOTFS/etc/powervr.ini" ]]; then
    echo "missing P5c config under $PVR_ROOTFS" >&2
    exit 2
fi
(
    cd "$PVR_ROOTFS"
    timeout 60 sha256sum -c SHA256SUMS
)

if [[ "${1:-}" == "--http" ]]; then
    mkdir -p "$HTTP_DIR"
    timeout 120 tar -C "$PVR_ROOTFS" -czf "$HTTP_DIR/pvr-p5c-rootfs.tar.gz" opt etc
    echo "serving $HTTP_DIR/pvr-p5c-rootfs.tar.gz on http://$HOST_IP:$HTTP_PORT/"
    echo "guest commands:"
    echo "  mkdir -p /tmp/pvr-p5c && cd /tmp/pvr-p5c"
    echo "  curl --noproxy '*' -o pvr-p5c-rootfs.tar.gz http://$HOST_IP:$HTTP_PORT/pvr-p5c-rootfs.tar.gz"
    echo "  tar -xzf pvr-p5c-rootfs.tar.gz"
    echo "  sh /path/to/p5c-guest.sh /tmp/pvr-p5c"
    exit 0
fi

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)
echo "staging $PVR_ROOTFS -> $BOARD:$DEST"
timeout 30 ssh "${SSH_OPTS[@]}" "$BOARD" "mkdir -p '$DEST'"
timeout 300 scp -r "${SSH_OPTS[@]}" "$PVR_ROOTFS/opt" "$PVR_ROOTFS/etc" "$PVR_ROOTFS/SHA256SUMS" "$BOARD:$DEST/"
timeout 30 ssh "${SSH_OPTS[@]}" "$BOARD" "sha256sum '$DEST'/opt/pvr/bin/eglconfigs '$DEST'/opt/pvr/lib/*.so '$DEST'/opt/pvr/dri/pvr_dri.so"

cat <<EOF

Staged.  In the Asterinas guest run (serial console) the commands from
tools/riscv/p5c/p5c-guest.sh, with SRC=/home/debian/pvr-p5c.

No initramfs file and no kernel file was modified.
EOF