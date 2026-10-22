#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# DRM conformance matrix.
#
# Runs the shared-DRM probes for one architecture and prints one line per
# probe. The graphical/Xorg gates are separate targets because they need a
# Debian image.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: conformance_matrix.sh --arch ARCH --kernel PATH --workspace DIR [OPTIONS]

Options:
  --arch ARCH       amd64 or riscv64 (required).
  --kernel PATH     Kernel for that architecture (required).
  --workspace DIR   Per-probe working directories (required).
  --probes LIST     Space-separated subset of:
                    legacy prime atomic sync flip
                    (default: all five).
  --timeout SECS    Per-probe deadline (default: 180).
  --accel MODE      x86_64 only: kvm or tcg (default: kvm).
  --cc PATH         Cross compiler.
  --qemu PATH       QEMU binary.

Exit status is 0 only if every selected probe prints its PASS marker.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

ARCH=""
KERNEL=""
WORKSPACE=""
PROBES="legacy prime atomic sync flip"
TIMEOUT=180
ACCEL="kvm"
CC=""
QEMU=""

while (( $# > 0 )); do
    case "$1" in
        --arch | --kernel | --workspace | --probes | --timeout | --accel | --cc | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --arch) ARCH="$2" ;;
                --kernel) KERNEL="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --probes) PROBES="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --accel) ACCEL="$2" ;;
                --cc) CC="$2" ;;
                --qemu) QEMU="$2" ;;
            esac
            shift 2
            ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

[[ "$ARCH" == "amd64" || "$ARCH" == "riscv64" ]] ||
    die_usage "--arch must be 'amd64' or 'riscv64'"
[[ -n "$KERNEL" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel and --workspace are required"
[[ -f "$KERNEL" ]] || die_usage "the kernel image does not exist: $KERNEL"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CHECK="$SCRIPT_DIR/initramfs_probe_check.sh"
[[ -x "$CHECK" ]] || die_usage "the probe runner is missing: $CHECK"

probe_source() {
    case "$1" in
        legacy) echo "$SCRIPT_DIR/virtio_gpu_probe.c" ;;
        prime) echo "$SCRIPT_DIR/prime_probe.c" ;;
        atomic) echo "$SCRIPT_DIR/atomic_probe.c" ;;
        sync) echo "$SCRIPT_DIR/sync_probe.c" ;;
        flip) echo "$SCRIPT_DIR/flip_probe.c" ;;
        *) return 1 ;;
    esac
}

mkdir -p -- "$WORKSPACE"
FAILED=0
for probe in $PROBES; do
    source_file="$(probe_source "$probe")" ||
        die_usage "unknown probe: $probe"
    args=(--arch "$ARCH" --kernel "$KERNEL" --probe "$source_file" \
        --workspace "$WORKSPACE/$probe" --timeout "$TIMEOUT" --accel "$ACCEL")
    [[ -n "$CC" ]] && args+=(--cc "$CC")
    [[ -n "$QEMU" ]] && args+=(--qemu "$QEMU")
    if "$CHECK" "${args[@]}" >"$WORKSPACE/$probe.log" 2>&1; then
        printf 'PROBE %-8s PASS\n' "$probe"
    else
        printf 'PROBE %-8s FAIL (see %s/%s.log)\n' "$probe" "$WORKSPACE" "$probe"
        FAILED=1
    fi
done

exit "$FAILED"
