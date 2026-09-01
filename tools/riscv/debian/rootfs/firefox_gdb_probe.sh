#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Run a bounded RISC-V Firefox startup probe under qemu-user's GDB stub.
#
# This deliberately invokes qemu-riscv64-static explicitly inside a bwrap
# rootfs namespace.  It never consults or modifies the host binfmt_misc
# registry.  The rootfs directory must be writable because Firefox creates a
# small profile during startup.

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
usage: firefox_gdb_probe.sh ROOTFS OUTPUT_DIR [TIMEOUT_SECONDS]

ROOTFS must be an extracted Debian riscv64 rootfs directory.  OUTPUT_DIR is
created outside /tmp and receives gdb.txt, qemu.log, and metadata.txt.

The qemu, namespace, and debugger binaries can be overridden with
QEMU_RISCV64_STATIC, BWRAP, and RISCV_GDB. Defaults are qemu-riscv64-static,
bwrap, and riscv64-linux-gnu-gdb.
EOF
    exit 2
}

[[ $# -ge 2 && $# -le 3 ]] || usage
readonly ROOTFS="$(realpath -e -- "$1")"
readonly OUTPUT_DIR="$(realpath -m -- "$2")"
readonly TIMEOUT_SECONDS="${3:-45}"
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
    echo "TIMEOUT_SECONDS must be a positive integer" >&2
    exit 2
}

readonly QEMU_RISCV64_STATIC="${QEMU_RISCV64_STATIC:-$(command -v qemu-riscv64-static || true)}"
readonly RISCV_GDB="${RISCV_GDB:-$(command -v riscv64-linux-gnu-gdb || true)}"
[[ -n "$QEMU_RISCV64_STATIC" && -x "$QEMU_RISCV64_STATIC" ]] || {
    echo "missing executable qemu-riscv64-static (set QEMU_RISCV64_STATIC)" >&2
    exit 2
}
[[ -n "$RISCV_GDB" && -x "$RISCV_GDB" ]] || {
    echo "missing executable riscv64-linux-gnu-gdb (set RISCV_GDB)" >&2
    exit 2
}
[[ -x "$ROOTFS/usr/bin/firefox-esr" || -x "$ROOTFS/usr/lib/firefox-esr/firefox-esr" ]] || {
    echo "rootfs does not contain Firefox ESR" >&2
    exit 2
}
[[ -e "$ROOTFS/lib/ld-linux-riscv64-lp64d.so.1" ]] || {
    echo "rootfs does not contain the RISC-V dynamic loader" >&2
    exit 2
}
[[ -w "$ROOTFS" ]] || {
    echo "rootfs must be writable for the temporary Firefox profile" >&2
    exit 2
}

mkdir -p -- "$OUTPUT_DIR"
readonly GDB_COMMANDS="$OUTPUT_DIR/gdb-commands.txt"
readonly GDB_LOG="$OUTPUT_DIR/gdb.txt"
readonly QEMU_LOG="$OUTPUT_DIR/qemu.log"
readonly META="$OUTPUT_DIR/metadata.txt"

# Pick a free TCP port without opening a long-lived listener ourselves.
readonly PORT="$(python3 - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
readonly PROFILE_GUEST="/tmp/asterinas-firefox-gdb-${PORT}"
readonly PROFILE="$ROOTFS$PROFILE_GUEST"
mkdir -p -- "$PROFILE"

# qemu's -L option selects the guest loader prefix; it does not chroot guest
# absolute paths.  Use an explicit mount namespace so Firefox, /proc, and its
# libraries all resolve inside ROOTFS.  The qemu binary is bound read-only into
# the namespace; no binfmt handler is needed or changed.
readonly BWRAP="${BWRAP:-$(command -v bwrap || true)}"
[[ -n "$BWRAP" && -x "$BWRAP" ]] || {
    echo "missing executable bwrap (set BWRAP)" >&2
    exit 2
}
readonly QEMU_IN_ROOT="/usr/bin/qemu-riscv64-static"

qemu_pid=""
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$PROFILE"
}
trap cleanup EXIT INT TERM

cat >"$GDB_COMMANDS" <<EOF
set pagination off
set confirm off
set debuginfod enabled off
# qemu-riscv64's remote target description uses the generic RISC-V name.
# Forcing the rv64 alias makes some GDB 17 builds reject the XML before the
# first stop, so leave the width negotiation to the remote stub.
set architecture riscv
set sysroot $ROOTFS
target remote 127.0.0.1:$PORT
printf "GDB_ENTRY\\n"
info registers pc sp ra a0 a1 a2 a3
x/16i \$pc
break __libc_start_main@plt
continue
printf "GDB_LIBC_START_MAIN_PLT\\n"
info registers pc sp ra a0 a1 a2 a3
thread apply all bt 6
detach
quit
EOF

{
    printf 'rootfs=%s\noutput=%s\ntimeout_seconds=%s\nport=%s\n' \
        "$ROOTFS" "$OUTPUT_DIR" "$TIMEOUT_SECONDS" "$PORT"
    printf 'qemu=%s\nbwrap=%s\ngdb=%s\n' "$QEMU_RISCV64_STATIC" "$BWRAP" "$RISCV_GDB"
    if [[ -e /proc/sys/fs/binfmt_misc/qemu-riscv64 ]]; then
        printf 'binfmt_qemu_riscv64=present (probe still uses explicit qemu)\n'
    else
        printf 'binfmt_qemu_riscv64=absent\n'
    fi
} >"$META"

timeout --foreground "$TIMEOUT_SECONDS" "$BWRAP" \
    --die-with-parent --share-net --bind "$ROOTFS" / \
    --ro-bind "$QEMU_RISCV64_STATIC" "$QEMU_IN_ROOT" \
    --proc /proc --dev /dev \
    "$QEMU_IN_ROOT" -L / -g "$PORT" \
    /usr/lib/firefox-esr/firefox-esr \
    --headless --no-remote --new-instance --marionette \
    --profile "$PROFILE_GUEST" about:blank \
    >"$QEMU_LOG" 2>&1 &
qemu_pid=$!

# GDB itself is bounded slightly below QEMU's bound, leaving cleanup time.
gdb_timeout="$((TIMEOUT_SECONDS > 5 ? TIMEOUT_SECONDS - 3 : TIMEOUT_SECONDS))"
if timeout --foreground "$gdb_timeout" "$RISCV_GDB" -q -batch \
    -x "$GDB_COMMANDS" >"$GDB_LOG" 2>&1; then
    printf 'GDB_PROBE PASS\n'
else
    gdb_rc=$?
    printf 'GDB_PROBE FAIL rc=%s\n' "$gdb_rc" >&2
    exit "$gdb_rc"
fi
