#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: build_stage1.sh [OPTIONS] [OUTPUT]
       build_stage1.sh --print-tools [OPTIONS]
       build_stage1.sh --print-entries [OPTIONS]

Build the static Debian root-handoff initramfs. `stage1_init.c` finds the
root disk by its ext2/ext4 volume label, mounts it, and chroots into it, so
the same program serves every architecture it compiles for.

Options:
  --architecture NAME  Target architecture (default: riscv64).
  --minimal            Stage only the init program. Use this when nothing
                       consumes the RISC-V desktop assets (browser gates,
                       Megrez clock sync, physical-host probes).

Set STAGE1_BUSYBOX to a cached target BusyBox to include the Basic shell.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

ARCHITECTURE="riscv64"
MINIMAL=0
OUTPUT=""
PRINT_MODE=""

while (( $# > 0 )); do
    case "$1" in
        --architecture)
            (( $# >= 2 )) || die_usage "missing value for --architecture"
            [[ -n "$2" ]] || die_usage "missing value for --architecture"
            ARCHITECTURE="$2"
            shift 2
            ;;
        --minimal)
            MINIMAL=1
            shift
            ;;
        --print-tools | --print-entries)
            [[ -z "$PRINT_MODE" ]] || die_usage "duplicate print argument: $1"
            PRINT_MODE="$1"
            shift
            ;;
        -*)
            die_usage "unknown option: $1"
            ;;
        *)
            [[ -z "$OUTPUT" ]] || die_usage "expected at most one output path"
            OUTPUT="$1"
            shift
            ;;
    esac
done

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIRECTORY/../../../.." && pwd -P)"

# The default target is spelled out here as well as in the architecture table,
# so that the default path needs no interpreter. A test asserts the two agree.
ARCH_SLUG_DEFAULT="riscv"
ARCH_CROSS_CC_DEFAULT="riscv64-linux-gnu-gcc"
if [[ "$ARCHITECTURE" == "riscv64" ]]; then
    ARCH_SLUG="$ARCH_SLUG_DEFAULT"
    ARCH_CROSS_CC="$ARCH_CROSS_CC_DEFAULT"
else
    mapfile -t ARCHITECTURE_FIELDS < <(
        PYTHONPATH="$REPOSITORY_ROOT" python3 -m \
            tools.debian.rootfs.arch --architecture "$ARCHITECTURE"
    )
    ((${#ARCHITECTURE_FIELDS[@]} == 7)) ||
        die_usage "invalid architecture data: $ARCHITECTURE"
    ARCH_SLUG="${ARCHITECTURE_FIELDS[1]}"
    ARCH_CROSS_CC="${ARCHITECTURE_FIELDS[6]}"
fi

if [[ -n "$PRINT_MODE" ]]; then
    if [[ "$PRINT_MODE" == "--print-tools" ]]; then
        printf '%s\n' "$ARCH_CROSS_CC"
        printf '%s\n' cpio python3
    elif ((MINIMAL == 1)); then
        printf '%s\n' . init
    else
        printf '%s\n' \
            . \
            init \
            usr \
            usr/lib \
            usr/lib/asterinas \
            usr/lib/asterinas/browser_interaction_perf.py \
            usr/lib/asterinas/browser_system_time.py \
            usr/lib/asterinas/browser_latency_contract.py \
            usr/lib/asterinas/browser_daily_use_contract.py \
            usr/lib/asterinas/browser_perf_capture.py \
            usr/lib/asterinas/browser_workload_contract.py \
            usr/lib/asterinas/browser_composite_capture.py \
            usr/lib/asterinas/browser-web-marionette-gate \
            usr/lib/asterinas/browser_web_marionette_gate.py \
            usr/lib/asterinas/browser_m5_marionette_gate.py \
            usr/lib/asterinas/browser-daily-use-gate \
            usr/lib/asterinas/browser-daily-use-upload \
            usr/lib/asterinas/megrez-clock-sync \
            usr/lib/asterinas/physical-external-services-quiesce \
            usr/lib/asterinas/desktop-input-identity \
            usr/lib/asterinas/physical-graphics-control \
            usr/lib/asterinas/megrez-safe-reboot \
            usr/lib/asterinas/physical-graphics-gate \
            usr/lib/asterinas/physical-graphics-interaction.html \
            usr/lib/asterinas/physical-system-probe \
            usr/lib/asterinas/g \
            usr/lib/asterinas/q \
            usr/lib/asterinas/s
    fi
    exit 0
fi

SOURCE="$SCRIPT_DIRECTORY/stage1_init.c"
DEBUG_CONSOLE_SOURCE="$SCRIPT_DIRECTORY/stage1_debug_console.c"
PROBE_SOURCE="$SCRIPT_DIRECTORY/stage1_probe.c"
BROWSER_GATE_SOURCE="$SCRIPT_DIRECTORY/browser_web_marionette_gate.py"
BROWSER_INTERACTION_PERF_SOURCE="$SCRIPT_DIRECTORY/browser_interaction_perf.py"
BROWSER_SYSTEM_TIME_SOURCE="$SCRIPT_DIRECTORY/browser_system_time.py"
BROWSER_LATENCY_CONTRACT_SOURCE="$SCRIPT_DIRECTORY/browser_latency_contract.py"
BROWSER_DAILY_USE_CONTRACT_SOURCE="$SCRIPT_DIRECTORY/browser_daily_use_contract.py"
BROWSER_PERF_CAPTURE_SOURCE="$SCRIPT_DIRECTORY/browser_perf_capture.py"
BROWSER_WORKLOAD_CONTRACT_SOURCE="$SCRIPT_DIRECTORY/browser_workload_contract.py"
BROWSER_COMPOSITE_CAPTURE_SOURCE="$SCRIPT_DIRECTORY/browser_composite_capture.py"
BROWSER_M5_MARIONETTE_GATE_SOURCE="$SCRIPT_DIRECTORY/browser_m5_marionette_gate.py"
BROWSER_DAILY_USE_GATE_SOURCE="$SCRIPT_DIRECTORY/browser_daily_use_gate.py"
BROWSER_DAILY_USE_UPLOAD_SOURCE="$SCRIPT_DIRECTORY/browser_daily_use_upload.py"
CLOCK_SYNC_SOURCE="$SCRIPT_DIRECTORY/megrez_clock_sync.py"
PHYSICAL_EXTERNAL_SOURCE="$SCRIPT_DIRECTORY/physical_external_services_quiesce.sh"
DESKTOP_INPUT_IDENTITY_SOURCE="$SCRIPT_DIRECTORY/desktop_input_identity.py"
PHYSICAL_GRAPHICS_CONTROL_SOURCE="$SCRIPT_DIRECTORY/physical_graphics_control.sh"
SAFE_REBOOT_SOURCE="$SCRIPT_DIRECTORY/megrez_safe_reboot.sh"
PHYSICAL_GRAPHICS_GATE_SOURCE="$SCRIPT_DIRECTORY/physical_graphics_gate.py"
PHYSICAL_GRAPHICS_PAGE_SOURCE="$SCRIPT_DIRECTORY/physical_graphics_interaction.html"
PHYSICAL_SYSTEM_PROBE_SOURCE="$SCRIPT_DIRECTORY/physical_system_probe.sh"
OUTPUT="${OUTPUT:-$REPOSITORY_ROOT/target/debian-$ARCH_SLUG/stage1/initramfs.cpio}"
COMPILER="${RISC_V_CC:-$ARCH_CROSS_CC}"
read -r -a EXTRA_LINK_FLAGS <<< "${RISC_V_LDFLAGS:-}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH-0}"
MAX_NEWC_TIMESTAMP=4294967295

if [[ ! "$SOURCE_DATE_EPOCH" =~ ^(0|[1-9][0-9]*)$ ]] ||
    [[ ${#SOURCE_DATE_EPOCH} -gt ${#MAX_NEWC_TIMESTAMP} ]] ||
    { [[ ${#SOURCE_DATE_EPOCH} -eq ${#MAX_NEWC_TIMESTAMP} ]] &&
        [[ "$SOURCE_DATE_EPOCH" > "$MAX_NEWC_TIMESTAMP" ]]; }; then
    printf 'error: SOURCE_DATE_EPOCH must be a canonical decimal newc u32\n' >&2
    exit 2
fi

if ! command -v "$COMPILER" >/dev/null 2>&1; then
    printf 'error: required compiler not found: %s\n' "$COMPILER" >&2
    exit 1
fi
if ! command -v cpio >/dev/null 2>&1; then
    printf 'error: required tool not found: cpio\n' >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    printf 'error: required tool not found: python3\n' >&2
    exit 1
fi

case "$OUTPUT" in
    '' | */ | *$'\n'* | *$'\r'* | *$'\t'*)
        printf 'error: unsafe output path: %s\n' "$OUTPUT" >&2
        exit 2
        ;;
esac

OUTPUT_DIRECTORY="$(dirname -- "$OUTPUT")"
OUTPUT_BASENAME="$(basename -- "$OUTPUT")"
INIT_OUTPUT="$OUTPUT_DIRECTORY/init"
if [[ "$OUTPUT_BASENAME" == "init" || "$OUTPUT" == "$SOURCE" ||
    -L "$OUTPUT" || -d "$OUTPUT" || -L "$INIT_OUTPUT" ||
    -d "$INIT_OUTPUT" ]]; then
    printf 'error: unsafe output path: %s\n' "$OUTPUT" >&2
    exit 2
fi

require_safe_directory_chain() {
    local directory="$1"
    local current=""
    local component
    local -a components

    if [[ "$directory" == /* ]]; then
        current="/"
    fi
    IFS='/' read -r -a components <<<"$directory"
    for component in "${components[@]}"; do
        [[ -n "$component" ]] || continue
        if [[ "$component" == "." || "$component" == ".." ]]; then
            printf 'error: unsafe output path: %s\n' "$OUTPUT" >&2
            exit 2
        fi
        if [[ "$current" == "/" ]]; then
            current="/$component"
        elif [[ -n "$current" ]]; then
            current="$current/$component"
        else
            current="$component"
        fi
        if [[ -L "$current" || ( -e "$current" && ! -d "$current" ) ]]; then
            printf 'error: unsafe output path: %s\n' "$OUTPUT" >&2
            exit 2
        fi
    done
}

require_safe_directory_chain "$OUTPUT_DIRECTORY"
umask 022
mkdir -p -- "$OUTPUT_DIRECTORY"
require_safe_directory_chain "$OUTPUT_DIRECTORY"

STAGE="$(mktemp -d)"
cleanup() {
    rm -rf -- "$STAGE"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

"$COMPILER" -std=c11 -O2 -static -no-pie -Wall -Wextra -Werror \
    "$SOURCE" "$DEBUG_CONSOLE_SOURCE" "$PROBE_SOURCE" \
    "${EXTRA_LINK_FLAGS[@]}" -o "$STAGE/init"
chmod 0755 "$STAGE" "$STAGE/init"
if ((MINIMAL == 0)); then
install -D -m 0755 -- "$BROWSER_GATE_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser-web-marionette-gate"
install -D -m 0644 -- "$BROWSER_GATE_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_web_marionette_gate.py"
install -D -m 0755 -- "$BROWSER_INTERACTION_PERF_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_interaction_perf.py"
install -D -m 0755 -- "$BROWSER_SYSTEM_TIME_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_system_time.py"
install -D -m 0644 -- "$BROWSER_LATENCY_CONTRACT_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_latency_contract.py"
install -D -m 0644 -- "$BROWSER_DAILY_USE_CONTRACT_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_daily_use_contract.py"
install -D -m 0755 -- "$BROWSER_PERF_CAPTURE_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_perf_capture.py"
install -D -m 0755 -- "$BROWSER_WORKLOAD_CONTRACT_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_workload_contract.py"
install -D -m 0755 -- "$BROWSER_COMPOSITE_CAPTURE_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_composite_capture.py"
install -D -m 0755 -- "$BROWSER_M5_MARIONETTE_GATE_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_m5_marionette_gate.py"
install -D -m 0755 -- "$BROWSER_DAILY_USE_GATE_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser-daily-use-gate"
install -D -m 0755 -- "$BROWSER_DAILY_USE_UPLOAD_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser-daily-use-upload"
install -D -m 0755 -- "$CLOCK_SYNC_SOURCE" \
    "$STAGE/usr/lib/asterinas/megrez-clock-sync"
install -D -m 0755 -- "$PHYSICAL_EXTERNAL_SOURCE" \
    "$STAGE/usr/lib/asterinas/physical-external-services-quiesce"
install -D -m 0755 -- "$DESKTOP_INPUT_IDENTITY_SOURCE" \
    "$STAGE/usr/lib/asterinas/desktop-input-identity"
install -D -m 0755 -- "$PHYSICAL_GRAPHICS_CONTROL_SOURCE" \
    "$STAGE/usr/lib/asterinas/physical-graphics-control"
install -D -m 0755 -- "$SAFE_REBOOT_SOURCE" \
    "$STAGE/usr/lib/asterinas/megrez-safe-reboot"
install -D -m 0755 -- "$PHYSICAL_GRAPHICS_GATE_SOURCE" \
    "$STAGE/usr/lib/asterinas/physical-graphics-gate"
install -D -m 0644 -- "$PHYSICAL_GRAPHICS_PAGE_SOURCE" \
    "$STAGE/usr/lib/asterinas/physical-graphics-interaction.html"
install -D -m 0755 -- "$PHYSICAL_SYSTEM_PROBE_SOURCE" \
    "$STAGE/usr/lib/asterinas/physical-system-probe"
ln -s physical-external-services-quiesce "$STAGE/usr/lib/asterinas/q"
ln -s physical-system-probe "$STAGE/usr/lib/asterinas/s"
ln -s physical-graphics-control "$STAGE/usr/lib/asterinas/g"
fi
if [[ -n "${STAGE1_BUSYBOX:-}" ]]; then
    PYTHONPATH="$REPOSITORY_ROOT" python3 -m tools.riscv.debian.rootfs.stage1_basic \
        --stage "$STAGE" --busybox "$STAGE1_BUSYBOX"
    find "$STAGE" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
fi
# One list drives both the archive and the entry audit below, so the two can
# never disagree about what this variant is supposed to contain.
ARCHIVE_ENTRIES=(
    .
    init
)
if ((MINIMAL == 0)); then
    ARCHIVE_ENTRIES+=(
        usr
        usr/lib
        usr/lib/asterinas
        usr/lib/asterinas/browser_interaction_perf.py
        usr/lib/asterinas/browser_system_time.py
        usr/lib/asterinas/browser_latency_contract.py
        usr/lib/asterinas/browser_daily_use_contract.py
        usr/lib/asterinas/browser_perf_capture.py
        usr/lib/asterinas/browser_workload_contract.py
        usr/lib/asterinas/browser_composite_capture.py
        usr/lib/asterinas/browser-web-marionette-gate
        usr/lib/asterinas/browser_web_marionette_gate.py
        usr/lib/asterinas/browser_m5_marionette_gate.py
        usr/lib/asterinas/browser-daily-use-gate
        usr/lib/asterinas/browser-daily-use-upload
        usr/lib/asterinas/megrez-clock-sync
        usr/lib/asterinas/physical-external-services-quiesce
        usr/lib/asterinas/desktop-input-identity
        usr/lib/asterinas/physical-graphics-control
        usr/lib/asterinas/megrez-safe-reboot
        usr/lib/asterinas/physical-graphics-gate
        usr/lib/asterinas/physical-graphics-interaction.html
        usr/lib/asterinas/physical-system-probe
        usr/lib/asterinas/g
        usr/lib/asterinas/q
        usr/lib/asterinas/s
    )
fi

touch -d "@$SOURCE_DATE_EPOCH" "$STAGE" "$STAGE/init"
if ((MINIMAL == 0)); then
touch -d "@$SOURCE_DATE_EPOCH" \
    "$STAGE/usr" \
    "$STAGE/usr/lib" \
    "$STAGE/usr/lib/asterinas" \
    "$STAGE/usr/lib/asterinas/browser_interaction_perf.py" \
    "$STAGE/usr/lib/asterinas/browser_system_time.py" \
    "$STAGE/usr/lib/asterinas/browser_latency_contract.py" \
    "$STAGE/usr/lib/asterinas/browser_daily_use_contract.py" \
    "$STAGE/usr/lib/asterinas/browser_perf_capture.py" \
    "$STAGE/usr/lib/asterinas/browser_workload_contract.py" \
    "$STAGE/usr/lib/asterinas/browser_composite_capture.py" \
    "$STAGE/usr/lib/asterinas/browser-web-marionette-gate" \
    "$STAGE/usr/lib/asterinas/browser_web_marionette_gate.py" \
    "$STAGE/usr/lib/asterinas/browser_m5_marionette_gate.py" \
    "$STAGE/usr/lib/asterinas/browser-daily-use-gate" \
    "$STAGE/usr/lib/asterinas/browser-daily-use-upload" \
    "$STAGE/usr/lib/asterinas/megrez-clock-sync" \
    "$STAGE/usr/lib/asterinas/physical-external-services-quiesce" \
    "$STAGE/usr/lib/asterinas/desktop-input-identity" \
    "$STAGE/usr/lib/asterinas/physical-graphics-control" \
    "$STAGE/usr/lib/asterinas/megrez-safe-reboot" \
    "$STAGE/usr/lib/asterinas/physical-graphics-gate" \
    "$STAGE/usr/lib/asterinas/physical-graphics-interaction.html" \
    "$STAGE/usr/lib/asterinas/physical-system-probe"
touch -h -d "@$SOURCE_DATE_EPOCH" \
    "$STAGE/usr/lib/asterinas/g" \
    "$STAGE/usr/lib/asterinas/q" \
    "$STAGE/usr/lib/asterinas/s"
fi

ARCHIVE="$STAGE/initramfs.cpio"
: >"$ARCHIVE"
touch -d "@$SOURCE_DATE_EPOCH" "$STAGE"
printf '%s\n' "${ARCHIVE_ENTRIES[@]}" |
    cpio --quiet --reproducible --owner=0:0 --create --format=newc \
        --directory="$STAGE" >"$ARCHIVE"
if [[ -n "${STAGE1_BUSYBOX:-}" ]]; then
    # Keep the legacy archive byte-for-byte stable when Basic is not requested.
    (cd "$STAGE" && find . -mindepth 1 ! -name initramfs.cpio -printf '%P\n' |
        LC_ALL=C sort | cpio --quiet --reproducible --owner=0:0 \
            --create --format=newc) >"$ARCHIVE"
fi
if [[ ! -s "$ARCHIVE" ]]; then
    printf 'error: generated initramfs is empty\n' >&2
    exit 1
fi

ACTUAL_ARCHIVE_ENTRIES="$(cpio --quiet --list <"$ARCHIVE")"
printf -v EXPECTED_ARCHIVE_ENTRIES '%s\n' "${ARCHIVE_ENTRIES[@]}"
EXPECTED_ARCHIVE_ENTRIES="${EXPECTED_ARCHIVE_ENTRIES%$'\n'}"
if [[ -z "${STAGE1_BUSYBOX:-}" &&
    "$ACTUAL_ARCHIVE_ENTRIES" != "$EXPECTED_ARCHIVE_ENTRIES" ]]; then
    printf 'error: generated initramfs has unexpected entries\n' >&2
    exit 1
fi
chmod 0644 "$ARCHIVE"

PYTHONPATH="$REPOSITORY_ROOT" python3 -m tools.debian.rootfs.fsops \
    publish-stage1 \
    --output-dir "$OUTPUT_DIRECTORY" \
    --init-source "$STAGE/init" \
    --archive-source "$ARCHIVE" \
    --archive-name "$OUTPUT_BASENAME"
