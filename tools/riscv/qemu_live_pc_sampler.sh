#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Sample a running QEMU system-emulation GDB stub without installing gdb in the
# guest.  Each connection pauses the VM only long enough to read the four hart
# register sets, then detaches so execution resumes between samples.

set -euo pipefail

usage() {
    printf '%s\n' \
        'usage: qemu_live_pc_sampler.sh PORT OUTPUT_DIR [SAMPLES] [INTERVAL_SECONDS]' \
        '' \
        'PORT must be the loopback ASTERINAS_QEMU_GDB_PORT used by the gate.' \
        'OUTPUT_DIR must be persistent and outside /tmp and target/.' >&2
    exit 2
}

[[ $# -ge 2 && $# -le 4 ]] || usage
readonly PORT="$1"
readonly OUTPUT_DIR="$(realpath -m -- "$2")"
readonly SAMPLES="${3:-12}"
readonly INTERVAL_SECONDS="${4:-2}"
readonly RISCV_GDB="${RISCV_GDB:-$(command -v riscv64-linux-gnu-gdb || true)}"
kernel_symbols="${ASTERINAS_KERNEL_SYMBOLS:-}"

[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1024 && PORT <= 65535)) || {
    echo 'PORT must be between 1024 and 65535' >&2
    exit 2
}
[[ "$SAMPLES" =~ ^[1-9][0-9]*$ ]] && ((SAMPLES <= 100)) || {
    echo 'SAMPLES must be between 1 and 100' >&2
    exit 2
}
[[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]] && ((INTERVAL_SECONDS <= 60)) || {
    echo 'INTERVAL_SECONDS must be between 0 and 60' >&2
    exit 2
}
[[ "$OUTPUT_DIR" != /tmp && "$OUTPUT_DIR" != /tmp/* && "$OUTPUT_DIR" != */target && "$OUTPUT_DIR" != */target/* ]] || {
    echo 'OUTPUT_DIR must be persistent and outside /tmp and target/' >&2
    exit 2
}
[[ -n "$RISCV_GDB" && -x "$RISCV_GDB" ]] || {
    echo 'missing riscv64-linux-gnu-gdb (set RISCV_GDB)' >&2
    exit 2
}
if [[ -n "$kernel_symbols" ]]; then
    [[ -s "$kernel_symbols" ]] || {
        echo 'ASTERINAS_KERNEL_SYMBOLS is empty or missing' >&2
        exit 2
    }
    kernel_symbols="$(realpath -e -- "$kernel_symbols")"
fi
readonly KERNEL_SYMBOLS="$kernel_symbols"

mkdir -p -- "$OUTPUT_DIR"
readonly COMMANDS="$OUTPUT_DIR/commands.gdb"
readonly META="$OUTPUT_DIR/metadata.txt"

cat >"$COMMANDS" <<EOF
set pagination off
set confirm off
set debuginfod enabled off
set architecture riscv:rv64
set remotetimeout 5
${KERNEL_SYMBOLS:+file $KERNEL_SYMBOLS}
target remote 127.0.0.1:$PORT
printf "ASTERINAS_LIVE_PC_SAMPLE_CONNECTED\\n"
info threads
thread apply all info registers pc ra sp
detach
quit
EOF

{
    printf 'port=%s\noutput=%s\nsamples=%s\ninterval_seconds=%s\n' \
        "$PORT" "$OUTPUT_DIR" "$SAMPLES" "$INTERVAL_SECONDS"
    printf 'gdb=%s\nkernel_symbols=%s\n' "$RISCV_GDB" "${KERNEL_SYMBOLS:-absent}"
    "$RISCV_GDB" --version | /usr/bin/head -1
    if [[ -e /proc/sys/fs/binfmt_misc/qemu-riscv64 ]]; then
        printf 'binfmt_qemu_riscv64=present (sampler does not use it)\n'
    else
        printf 'binfmt_qemu_riscv64=absent\n'
    fi
} >"$META"

connected=0
consecutive_failures=0
for ((sample = 1; sample <= SAMPLES; sample += 1)); do
    log="$OUTPUT_DIR/pc-sample-$(printf '%03d' "$sample").gdb.txt"
    started="$(date --iso-8601=ns)"
    if timeout --foreground 10 "$RISCV_GDB" -q -batch -x "$COMMANDS" \
        >"$log" 2>&1; then
        if grep -q '^ASTERINAS_LIVE_PC_SAMPLE_CONNECTED$' "$log"; then
            connected=$((connected + 1))
            consecutive_failures=0
            status=connected
        else
            consecutive_failures=$((consecutive_failures + 1))
            status=no-marker
        fi
    else
        consecutive_failures=$((consecutive_failures + 1))
        status="gdb-rc-$?"
    fi
    printf 'sample=%s started=%s status=%s log=%s\n' \
        "$sample" "$started" "$status" "${log##*/}" >>"$META"
    # A completed gate closes the QEMU stub.  Three consecutive failures are
    # enough to distinguish that condition from one transient attach race and
    # avoid spending the remaining sample budget on guaranteed timeouts.
    if ((consecutive_failures >= 3)); then
        printf 'stopped_early=consecutive-connect-failures count=%s\n' \
            "$consecutive_failures" >>"$META"
        break
    fi
    if ((sample < SAMPLES && INTERVAL_SECONDS > 0)); then
        /usr/bin/timeout "$((INTERVAL_SECONDS + 2))" \
            /usr/bin/sleep "$INTERVAL_SECONDS" || true
    fi
done

((connected > 0)) || {
    echo "no GDB sample connected; inspect $OUTPUT_DIR" >&2
    exit 1
}
printf 'QEMU_LIVE_PC_SAMPLER PASS connected=%s requested=%s\n' \
    "$connected" "$SAMPLES"
