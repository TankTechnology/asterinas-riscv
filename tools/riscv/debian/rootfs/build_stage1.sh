#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: build_stage1.sh [OUTPUT]
       build_stage1.sh --print-tools
       build_stage1.sh --print-entries

Build the static RISC-V Debian root-handoff initramfs.
Set STAGE1_BUSYBOX to a cached RISC-V BusyBox to include the Basic shell.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

if (( $# > 1 )); then
    die_usage "expected at most one output path"
fi

if (( $# == 1 )); then
    case "$1" in
        --print-tools)
            printf 'riscv64-linux-gnu-gcc\ncpio\npython3\n'
            exit 0
            ;;
        --print-entries)
            printf '%s\n' \
                . \
                init \
                usr \
                usr/lib \
                usr/lib/asterinas \
                usr/lib/asterinas/browser_interaction_perf.py \
                usr/lib/asterinas/browser_system_time.py \
                usr/lib/asterinas/browser_latency_contract.py \
                usr/lib/asterinas/browser_perf_capture.py \
                usr/lib/asterinas/browser-web-marionette-gate \
                usr/lib/asterinas/browser_m5_marionette_gate.py \
                usr/lib/asterinas/megrez-clock-sync \
                usr/lib/asterinas/physical-external-services-quiesce \
                usr/lib/asterinas/desktop-input-identity \
                usr/lib/asterinas/physical-graphics-control \
                usr/lib/asterinas/physical-graphics-gate \
                usr/lib/asterinas/physical-graphics-interaction.html \
                usr/lib/asterinas/physical-system-probe \
                usr/lib/asterinas/g \
                usr/lib/asterinas/q \
                usr/lib/asterinas/s
            exit 0
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        -*)
            die_usage "unknown option: $1"
            ;;
    esac
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/../../../.." && pwd)"
SOURCE="$SCRIPT_DIR/stage1_init.c"
DEBUG_CONSOLE_SOURCE="$SCRIPT_DIR/stage1_debug_console.c"
PROBE_SOURCE="$SCRIPT_DIR/stage1_probe.c"
BROWSER_GATE_SOURCE="$SCRIPT_DIR/browser_web_marionette_gate.py"
BROWSER_INTERACTION_PERF_SOURCE="$SCRIPT_DIR/browser_interaction_perf.py"
BROWSER_SYSTEM_TIME_SOURCE="$SCRIPT_DIR/browser_system_time.py"
BROWSER_LATENCY_CONTRACT_SOURCE="$SCRIPT_DIR/browser_latency_contract.py"
BROWSER_PERF_CAPTURE_SOURCE="$SCRIPT_DIR/browser_perf_capture.py"
BROWSER_M5_MARIONETTE_GATE_SOURCE="$SCRIPT_DIR/browser_m5_marionette_gate.py"
CLOCK_SYNC_SOURCE="$SCRIPT_DIR/megrez_clock_sync.py"
PHYSICAL_EXTERNAL_SOURCE="$SCRIPT_DIR/physical_external_services_quiesce.sh"
DESKTOP_INPUT_IDENTITY_SOURCE="$SCRIPT_DIR/desktop_input_identity.py"
PHYSICAL_GRAPHICS_CONTROL_SOURCE="$SCRIPT_DIR/physical_graphics_control.sh"
PHYSICAL_GRAPHICS_GATE_SOURCE="$SCRIPT_DIR/physical_graphics_gate.py"
PHYSICAL_GRAPHICS_PAGE_SOURCE="$SCRIPT_DIR/physical_graphics_interaction.html"
PHYSICAL_SYSTEM_PROBE_SOURCE="$SCRIPT_DIR/physical_system_probe.sh"
OUTPUT="${1:-$REPOSITORY_ROOT/target/debian-riscv/stage1/initramfs.cpio}"
COMPILER="${RISC_V_CC:-riscv64-linux-gnu-gcc}"
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
install -D -m 0755 -- "$BROWSER_GATE_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser-web-marionette-gate"
install -D -m 0755 -- "$BROWSER_INTERACTION_PERF_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_interaction_perf.py"
install -D -m 0755 -- "$BROWSER_SYSTEM_TIME_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_system_time.py"
install -D -m 0644 -- "$BROWSER_LATENCY_CONTRACT_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_latency_contract.py"
install -D -m 0755 -- "$BROWSER_PERF_CAPTURE_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_perf_capture.py"
install -D -m 0755 -- "$BROWSER_M5_MARIONETTE_GATE_SOURCE" \
    "$STAGE/usr/lib/asterinas/browser_m5_marionette_gate.py"
install -D -m 0755 -- "$CLOCK_SYNC_SOURCE" \
    "$STAGE/usr/lib/asterinas/megrez-clock-sync"
install -D -m 0755 -- "$PHYSICAL_EXTERNAL_SOURCE" \
    "$STAGE/usr/lib/asterinas/physical-external-services-quiesce"
install -D -m 0755 -- "$DESKTOP_INPUT_IDENTITY_SOURCE" \
    "$STAGE/usr/lib/asterinas/desktop-input-identity"
install -D -m 0755 -- "$PHYSICAL_GRAPHICS_CONTROL_SOURCE" \
    "$STAGE/usr/lib/asterinas/physical-graphics-control"
install -D -m 0755 -- "$PHYSICAL_GRAPHICS_GATE_SOURCE" \
    "$STAGE/usr/lib/asterinas/physical-graphics-gate"
install -D -m 0644 -- "$PHYSICAL_GRAPHICS_PAGE_SOURCE" \
    "$STAGE/usr/lib/asterinas/physical-graphics-interaction.html"
install -D -m 0755 -- "$PHYSICAL_SYSTEM_PROBE_SOURCE" \
    "$STAGE/usr/lib/asterinas/physical-system-probe"
ln -s physical-external-services-quiesce "$STAGE/usr/lib/asterinas/q"
ln -s physical-system-probe "$STAGE/usr/lib/asterinas/s"
ln -s physical-graphics-control "$STAGE/usr/lib/asterinas/g"
if [[ -n "${STAGE1_BUSYBOX:-}" ]]; then
    PYTHONPATH="$REPOSITORY_ROOT" python3 -m tools.riscv.debian.rootfs.stage1_basic \
        --stage "$STAGE" --busybox "$STAGE1_BUSYBOX"
    find "$STAGE" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
fi
touch -d "@$SOURCE_DATE_EPOCH" \
    "$STAGE" \
    "$STAGE/init" \
    "$STAGE/usr" \
    "$STAGE/usr/lib" \
    "$STAGE/usr/lib/asterinas" \
    "$STAGE/usr/lib/asterinas/browser_interaction_perf.py" \
    "$STAGE/usr/lib/asterinas/browser_system_time.py" \
    "$STAGE/usr/lib/asterinas/browser_latency_contract.py" \
    "$STAGE/usr/lib/asterinas/browser_perf_capture.py" \
    "$STAGE/usr/lib/asterinas/browser-web-marionette-gate" \
    "$STAGE/usr/lib/asterinas/browser_m5_marionette_gate.py" \
    "$STAGE/usr/lib/asterinas/megrez-clock-sync" \
    "$STAGE/usr/lib/asterinas/physical-external-services-quiesce" \
    "$STAGE/usr/lib/asterinas/desktop-input-identity" \
    "$STAGE/usr/lib/asterinas/physical-graphics-control" \
    "$STAGE/usr/lib/asterinas/physical-graphics-gate" \
    "$STAGE/usr/lib/asterinas/physical-graphics-interaction.html" \
    "$STAGE/usr/lib/asterinas/physical-system-probe"
touch -h -d "@$SOURCE_DATE_EPOCH" \
    "$STAGE/usr/lib/asterinas/g" \
    "$STAGE/usr/lib/asterinas/q" \
    "$STAGE/usr/lib/asterinas/s"

ARCHIVE="$STAGE/initramfs.cpio"
: >"$ARCHIVE"
touch -d "@$SOURCE_DATE_EPOCH" "$STAGE"
printf '%s\n' \
    . \
    init \
    usr \
    usr/lib \
    usr/lib/asterinas \
    usr/lib/asterinas/browser_interaction_perf.py \
    usr/lib/asterinas/browser_system_time.py \
    usr/lib/asterinas/browser_latency_contract.py \
    usr/lib/asterinas/browser_perf_capture.py \
    usr/lib/asterinas/browser-web-marionette-gate \
    usr/lib/asterinas/browser_m5_marionette_gate.py \
    usr/lib/asterinas/megrez-clock-sync \
    usr/lib/asterinas/physical-external-services-quiesce \
    usr/lib/asterinas/desktop-input-identity \
    usr/lib/asterinas/physical-graphics-control \
    usr/lib/asterinas/physical-graphics-gate \
    usr/lib/asterinas/physical-graphics-interaction.html \
    usr/lib/asterinas/physical-system-probe \
    usr/lib/asterinas/g \
    usr/lib/asterinas/q \
    usr/lib/asterinas/s |
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

ARCHIVE_ENTRIES="$(cpio --quiet --list <"$ARCHIVE")"
EXPECTED_ARCHIVE_ENTRIES=$'.\ninit\nusr\nusr/lib\nusr/lib/asterinas\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/browser_interaction_perf.py\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/browser_system_time.py\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/browser_latency_contract.py\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/browser_perf_capture.py\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/browser-web-marionette-gate\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/browser_m5_marionette_gate.py\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/megrez-clock-sync\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/physical-external-services-quiesce\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/desktop-input-identity\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/physical-graphics-control\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/physical-graphics-gate\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/physical-graphics-interaction.html\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/physical-system-probe\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/g\n'
EXPECTED_ARCHIVE_ENTRIES+=$'usr/lib/asterinas/q\n'
EXPECTED_ARCHIVE_ENTRIES+='usr/lib/asterinas/s'
if [[ -z "${STAGE1_BUSYBOX:-}" &&
    "$ARCHIVE_ENTRIES" != "$EXPECTED_ARCHIVE_ENTRIES" ]]; then
    printf 'error: generated initramfs has unexpected entries\n' >&2
    exit 1
fi
chmod 0644 "$ARCHIVE"

PYTHONPATH="$REPOSITORY_ROOT" python3 -m tools.riscv.debian.rootfs.fsops \
    publish-stage1 \
    --output-dir "$OUTPUT_DIRECTORY" \
    --init-source "$STAGE/init" \
    --archive-source "$ARCHIVE" \
    --archive-name "$OUTPUT_BASENAME"
