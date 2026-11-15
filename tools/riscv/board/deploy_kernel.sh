#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Put a kernel image on the board, or refuse to.
#
#   tools/riscv/board/deploy_kernel.sh [image] [--skip-preflight]
#
# The board has no physical reset button in reach of this workflow, so every
# step here is about never leaving it unbootable:
#
#   1. preflight on the host (tools/riscv/board/preflight_kernel.sh) -- the
#      image is the right kind, fits the load region, and reaches userspace in
#      QEMU;
#   2. the board must still hold a rollback image and the repair initramfs
#      before anything is overwritten, and enough free space for the copy;
#   3. the copy lands as kernel-rel.img.new and is only renamed into place
#      after its checksum matches, so an interrupted transfer changes nothing
#      (the serial and network links have both died mid-transfer before).
#
# On any refusal the board keeps running its current kernel. What was deployed
# last is printed at the end, along with the one-line rollback.
#
# Environment: BOARD_HOST (default 58.206.232.122), BOARD_PORT (2222),
# BOARD_KEY (~/.ssh/asterinas_megrez).
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)

HOST=${BOARD_HOST:-58.206.232.122}
PORT=${BOARD_PORT:-22}
KEY=${BOARD_KEY:-$HOME/.ssh/asterinas_megrez}
REMOTE=/root/kernel-rel.img

SSH=(ssh -i "$KEY" -p "$PORT" -o StrictHostKeyChecking=no
     -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=8)
SCP=(scp -i "$KEY" -P "$PORT" -o StrictHostKeyChecking=no
     -o UserKnownHostsFile=/dev/null -o BatchMode=yes)

IMAGE=
SKIP_PREFLIGHT=0
CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --skip-preflight) SKIP_PREFLIGHT=1 ;;
        --check-only) CHECK_ONLY=1 ;;
        -*) echo "unknown option: $arg" >&2; exit 1 ;;
        *) IMAGE=$arg ;;
    esac
done
IMAGE=${IMAGE:-$REPO/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image}

echo "== preflight"
if [ "$SKIP_PREFLIGHT" -eq 1 ]; then
    echo "SKIPPED by request -- the board keeps its rollback image either way"
else
    if ! "$HERE/preflight_kernel.sh" "$IMAGE"; then
        echo
        echo "deploy: REFUSED (preflight). The board was not touched."
        exit 1
    fi
fi

SUM=$(sha256sum "$IMAGE" | cut -d' ' -f1)
SIZE=$(/usr/bin/stat -c %s "$IMAGE")

echo
echo "== board checks"
if ! board_state=$("${SSH[@]}" "root@$HOST" "
    free=\$(df -P / | awk 'NR==2 {print \$4}')
    echo \"free_kb=\$free\"
    for f in /root/kernel-rel.img.bak /root/repair.cpio.gz /root/boot.scr; do
        if [ -s \$f ]; then echo \"present=\$f\"; else echo \"missing=\$f\"; fi
    done
    echo \"current=\$(sha256sum /root/kernel-rel.img 2>/dev/null | cut -d' ' -f1)\"
" 2>&1); then
    echo "$board_state"
    echo "deploy: REFUSED (board unreachable; nothing was written)"
    exit 1
fi

echo "$board_state" | sed 's/^/     /'
need_kb=$(( SIZE * 3 / 1024 ))
free_kb=$(echo "$board_state" | sed -n 's/^free_kb=//p')
missing=$(echo "$board_state" | sed -n 's/^missing=//p')

if [ -n "$missing" ]; then
    echo "deploy: REFUSED (rollback chain incomplete: $missing)"
    exit 1
fi
if [ -z "$free_kb" ] || [ "$free_kb" -lt "$need_kb" ]; then
    echo "deploy: REFUSED (free space ${free_kb:-?} KiB < ${need_kb} KiB needed)"
    exit 1
fi

echo
echo "== transfer"
echo "image  $IMAGE"
echo "sha256 $SUM ($SIZE bytes)"
if [ "$CHECK_ONLY" -eq 1 ]; then
    if [ "$(echo "$board_state" | sed -n 's/^current=//p')" = "$SUM" ]; then
        echo "check-only: the board already runs this exact image"
    else
        echo "check-only: the board runs a different image; rerun without --check-only to deploy"
    fi
    exit 0
fi
if ! "${SCP[@]}" "$IMAGE" "root@$HOST:$REMOTE.new"; then
    "${SSH[@]}" "root@$HOST" "rm -f $REMOTE.new" 2>/dev/null
    echo "deploy: FAILED (transfer). The board still runs its current kernel."
    exit 1
fi

if ! "${SSH[@]}" "root@$HOST" "
    remote=\$(sha256sum $REMOTE.new | cut -d' ' -f1)
    if [ \"\$remote\" != \"$SUM\" ]; then
        echo \"CHECKSUM MISMATCH remote=\$remote\"
        rm -f $REMOTE.new
        exit 1
    fi
    cp -f $REMOTE $REMOTE.bak
    cp -f $REMOTE.new $REMOTE
    rm -f $REMOTE.new
    sync
    # The transfer was verified above, but the write onto the running
    # filesystem was not: this board's ext2 has left a truncated primary image
    # behind before, and the boot then hung with the panel showing nothing.
    # Compare what is actually on disk, and keep the backup as the way out.
    deployed=\$(sha256sum $REMOTE | cut -d' ' -f1)
    if [ \"\$deployed\" != \"$SUM\" ]; then
        echo \"INSTALL CORRUPTED deployed=\$deployed\"
        exit 1
    fi
    echo \"deployed=\$deployed\"
"; then
    echo "deploy: FAILED (checksum or install). The board still runs its current kernel."
    exit 1
fi

echo
echo "deploy: OK"
echo "The new kernel takes effect on the next boot. Rollback, from the board or over SSH:"
echo "    cp $REMOTE.bak $REMOTE && sync && reboot -f"
echo "If the board does not come back at all, the console still answers:"
echo "    tools/riscv/board/megrez_console.py sysrq b   # or power-cycle"
echo "and U-Boot falls back to $REMOTE.bak if the primary image cannot be loaded."
