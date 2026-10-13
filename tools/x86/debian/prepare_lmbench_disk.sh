#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Build a scratch Debian root disk that runs the pinned native LMBench suite.
#
# The guest root filesystem does not durably keep what the guest writes under
# this kernel, so everything the run produces is also echoed to the serial
# console by the workload. The runtime is extracted here, on the host, so the
# guest does no unpacking work.

set -euo pipefail

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIRECTORY/../../.." && pwd -P)"
GUEST_ASSETS="$SCRIPT_DIRECTORY/lmbench_guest"

usage() {
    cat <<'EOF'
Usage: prepare_lmbench_disk.sh --base-root PATH --runtime PATH --output PATH
                               [--work-dir PATH]

Options:
  --base-root PATH  A published Debian rootfs image to copy (required).
  --runtime PATH    Runtime archive from `lmbench_native.py package` (required).
  --output PATH     Scratch root disk to write (required).
  --work-dir PATH   Scratch directory (default: alongside the output).
  --cc PATH         Compiler for the guest regression binary (default: cc).

Requires root: the image is loop-mounted to install the runtime.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

BASE_ROOT=""
RUNTIME=""
OUTPUT=""
WORK_DIR=""
COMPILER="cc"

while (( $# > 0 )); do
    case "$1" in
        --base-root | --runtime | --output | --work-dir | --cc)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --base-root) BASE_ROOT="$2" ;;
                --runtime) RUNTIME="$2" ;;
                --output) OUTPUT="$2" ;;
                --work-dir) WORK_DIR="$2" ;;
                --cc) COMPILER="$2" ;;
            esac
            shift 2
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            die_usage "unknown argument: $1"
            ;;
    esac
done

[[ -n "$BASE_ROOT" ]] || die_usage "--base-root is required"
[[ -n "$RUNTIME" ]] || die_usage "--runtime is required"
[[ -n "$OUTPUT" ]] || die_usage "--output is required"
[[ -f "$BASE_ROOT" ]] || die_usage "base root image is missing: $BASE_ROOT"
[[ -f "$RUNTIME" ]] || die_usage "runtime archive is missing: $RUNTIME"
[[ "$(id -u)" == "0" ]] || die_usage "this script must run as root to loop-mount the image"

OUTPUT="$(readlink -f -- "$OUTPUT" 2>/dev/null || printf '%s' "$OUTPUT")"
WORK_DIR="${WORK_DIR:-$(dirname -- "$OUTPUT")/prepare-lmbench}"
[[ "$WORK_DIR" != "/" ]] || die_usage "refusing to use / as a work directory"
rm -rf -- "$WORK_DIR"
mkdir -p -- "$WORK_DIR/mnt" "$WORK_DIR/stage"
STAGE="$WORK_DIR/stage"
MOUNT="$WORK_DIR/mnt"

case "$OUTPUT" in
    '' | */ | *$'\n'* | *$'\r'* | *$'\t'* | *' '*)
        die_usage "unsafe output path: $OUTPUT"
        ;;
esac
[[ -n "$(basename -- "$OUTPUT")" ]] || die_usage "unsafe output path: $OUTPUT"

for asset in lmbench-run.sh lmbench-run.service; do
    [[ -f "$GUEST_ASSETS/$asset" ]] ||
        die_usage "missing guest asset: $GUEST_ASSETS/$asset"
done
[[ -f "$REPOSITORY_ROOT/test/initramfs/src/regression/network/wildcard_bind_loopback.c" ]] ||
    die_usage "missing the wildcard-bind regression source"

printf '=== extract the runtime ===\n'
tar xzf "$RUNTIME" -C "$STAGE"

printf '=== compile the guest regression binary ===\n'
command -v "$COMPILER" >/dev/null 2>&1 || die_usage "compiler not found: $COMPILER"
"$COMPILER" -std=c11 -O2 -static -Wall -Wextra -Werror \
    "$REPOSITORY_ROOT/test/initramfs/src/regression/network/wildcard_bind_loopback.c" \
    -o "$STAGE/wildcard_bind_loopback"

printf '=== copy the base root disk ===\n'
rm -f -- "$OUTPUT"
cp --sparse=always -- "$BASE_ROOT" "$OUTPUT"

cleanup() {
    umount "$MOUNT" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
mount -o loop,rw -- "$OUTPUT" "$MOUNT"

printf '=== install the runtime tree ===\n'
if [[ -d "$STAGE/nix" ]]; then
    cp -a "$STAGE/nix" "$MOUNT/"
fi
mkdir -p "$MOUNT/opt"
cp -a "$STAGE/opt/lmbench" "$MOUNT/opt/"

printf '=== install the workload and its unit ===\n'
install -m 0755 -o root -g root "$GUEST_ASSETS/lmbench-run.sh" "$MOUNT/opt/lmbench-run.sh"
install -m 0644 -o root -g root "$GUEST_ASSETS/lmbench-run.service" \
    "$MOUNT/etc/systemd/system/lmbench-run.service"
install -m 0755 -o root -g root "$STAGE/wildcard_bind_loopback" \
    "$MOUNT/opt/wildcard_bind_loopback"

# systemd-waiting-for-dbus and the Journal Service both go through a readiness
# handshake this kernel cannot complete. Without the override the start job
# times out and every unit ordered after the journal socket waits for it.
printf '=== relax the journal service readiness handshake ===\n'
mkdir -p "$MOUNT/etc/systemd/system/systemd-journald.service.d"
cat >"$MOUNT/etc/systemd/system/systemd-journald.service.d/asterinas-notify-compat.conf" <<'CONF'
[Service]
Type=simple
NotifyAccess=none
TimeoutStartSec=120s
CONF

mkdir -p "$MOUNT/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/lmbench-run.service \
    "$MOUNT/etc/systemd/system/multi-user.target.wants/lmbench-run.service"
rm -f "$MOUNT/etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service"

printf '=== sync and release ===\n'
sync
cleanup
trap - EXIT

printf '=== verify ===\n'
for path in /opt/lmbench-run.sh /opt/lmbench/src /opt/lmbench/bin \
    /etc/systemd/system/lmbench-run.service; do
    printf '%-45s ' "$path"
    debugfs -R "stat $path" "$OUTPUT" 2>/dev/null | head -1
done
printf 'root disk: %s\n' "$OUTPUT"
