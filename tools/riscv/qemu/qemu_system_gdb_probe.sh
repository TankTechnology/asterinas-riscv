#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Capture a bounded QEMU system-emulation GDB-stub snapshot.
#
# This stops the VM before the first guest instruction. It is intentionally a
# reset/entry probe, not a Firefox gate. No host binfmt state is read or
# modified beyond the read-only metadata check.

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
usage: qemu_system_gdb_probe.sh KERNEL_IMAGE OUTPUT_DIR [TIMEOUT_SECONDS]

The probe launches qemu-system-riscv64 with -S -gdb, records the reset PC and
the first instructions, then detaches. OUTPUT_DIR is persistent and receives
gdb.txt, qemu.log, commands.txt, and metadata.txt.

Set ASTERINAS_KERNEL_SYMBOLS to the matching unstripped ELF when symbol
loading is desired; KERNEL_IMAGE itself may be the raw .Image boot payload.
Set ASTERINAS_KERNEL_CONTINUE=1 as an explicit opt-in to continue toward
`_start`; direct `-bios none -kernel` boot may not perform the U-Boot handoff.
EOF
    exit 2
}

[[ $# -ge 2 && $# -le 3 ]] || usage
readonly KERNEL="$(realpath -e -- "$1")"
readonly OUTPUT_DIR="$(realpath -m -- "$2")"
readonly TIMEOUT_SECONDS="${3:-15}"
readonly KERNEL_CONTINUE="${ASTERINAS_KERNEL_CONTINUE:-0}"
kernel_symbols="${ASTERINAS_KERNEL_SYMBOLS:-}"
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
    echo "TIMEOUT_SECONDS must be a positive integer" >&2
    exit 2
}
[[ -s "$KERNEL" ]] || {
    echo "kernel image is empty" >&2
    exit 2
}
if [[ -n "$kernel_symbols" ]]; then
    [[ -s "$kernel_symbols" ]] || {
        echo "ASTERINAS_KERNEL_SYMBOLS is empty or missing" >&2
        exit 2
    }
    kernel_symbols="$(realpath -e -- "$kernel_symbols")"
fi
readonly KERNEL_SYMBOLS="$kernel_symbols"
[[ "$KERNEL_CONTINUE" == 0 || "$KERNEL_CONTINUE" == 1 ]] || {
    echo "ASTERINAS_KERNEL_CONTINUE must be 0 or 1" >&2
    exit 2
}

readonly QEMU="${QEMU_SYSTEM_RISCV64:-$(command -v qemu-system-riscv64 || true)}"
readonly RISCV_GDB="${RISCV_GDB:-$(command -v riscv64-linux-gnu-gdb || true)}"
[[ -n "$QEMU" && -x "$QEMU" ]] || {
    echo "missing executable qemu-system-riscv64 (set QEMU_SYSTEM_RISCV64)" >&2
    exit 2
}
[[ -n "$RISCV_GDB" && -x "$RISCV_GDB" ]] || {
    echo "missing executable riscv64-linux-gnu-gdb (set RISCV_GDB)" >&2
    exit 2
}

mkdir -p -- "$OUTPUT_DIR"
readonly COMMANDS="$OUTPUT_DIR/commands.txt"
readonly GDB_LOG="$OUTPUT_DIR/gdb.txt"
readonly QEMU_LOG="$OUTPUT_DIR/qemu.log"
readonly META="$OUTPUT_DIR/metadata.txt"
readonly PORT="$(python3 - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"

qemu_pid=""
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

cat >"$COMMANDS" <<EOF
set pagination off
set confirm off
set debuginfod enabled off
set architecture riscv:rv64
${KERNEL_SYMBOLS:+file $KERNEL_SYMBOLS}
target remote 127.0.0.1:$PORT
printf "ASTERINAS_SYSTEM_GDB_CONNECTED\\n"
info registers pc sp ra a0 a1 a2 a3
x/8i \$pc
EOF
if [[ "$KERNEL_CONTINUE" == 1 && -n "$KERNEL_SYMBOLS" ]]; then
    cat >>"$COMMANDS" <<'EOF'
break _start
continue
printf "ASTERINAS_KERNEL_START_HIT\\n"
info registers pc sp ra a0 a1 a2 a3
thread apply all bt 4
EOF
fi
cat >>"$COMMANDS" <<'EOF'
detach
quit
EOF

{
    printf 'kernel=%s\noutput=%s\ntimeout_seconds=%s\nport=%s\n' \
        "$KERNEL" "$OUTPUT_DIR" "$TIMEOUT_SECONDS" "$PORT"
    printf 'kernel_symbols=%s\nkernel_continue=%s\n' \
        "${KERNEL_SYMBOLS:-absent}" "$KERNEL_CONTINUE"
    printf 'qemu=%s\ngdb=%s\n' "$QEMU" "$RISCV_GDB"
    "$QEMU" --version | head -1
    if [[ -e /proc/sys/fs/binfmt_misc/qemu-riscv64 ]]; then
        printf 'binfmt_qemu_riscv64=present (probe uses system emulation)\n'
    else
        printf 'binfmt_qemu_riscv64=absent\n'
    fi
} >"$META"

timeout --foreground "$TIMEOUT_SECONDS" "$QEMU" \
    -machine virt -m 256M -smp 1 -nographic -bios none \
    -kernel "$KERNEL" -S -gdb "tcp:127.0.0.1:$PORT" \
    >"$QEMU_LOG" 2>&1 &
qemu_pid=$!

gdb_timeout="$((TIMEOUT_SECONDS > 5 ? TIMEOUT_SECONDS - 3 : TIMEOUT_SECONDS))"
timeout --foreground "$gdb_timeout" "$RISCV_GDB" -q -batch \
    -x "$COMMANDS" >"$GDB_LOG" 2>&1
grep -q '^ASTERINAS_SYSTEM_GDB_CONNECTED$' "$GDB_LOG" || {
    echo "GDB did not reach the remote target; inspect $GDB_LOG" >&2
    exit 1
}
printf 'SYSTEM_GDB_PROBE PASS\n'
