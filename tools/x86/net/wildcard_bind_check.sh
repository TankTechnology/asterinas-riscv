#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Reproducer for the wildcard-bind bug (INADDR_ANY pinned to loopback).
#
# Boots the Debian guest, compiles tools/x86/net/wildcard_bind_check.c inside
# it, and runs both cases:
#
#   bind     socket -> bind(0.0.0.0:0) -> sendto(DNS) -> recvfrom
#   nobind   socket -> sendto(DNS) -> recvfrom
#
# Expected before the fix: bind FAILS (timeout), nobind PASSES.
# Expected after the fix:  both PASS.
#
# Exit status: 0 when both cases PASS, 1 otherwise.
#
# Usage:
#   tools/x86/net/wildcard_bind_check.sh \
#     --kernel target/osdk/asterinas-osdk-bin.elf \
#     --initramfs target/debian-x86/stage1/initramfs.cpio \
#     --base-root target/systemd-health/daily.ext2 \
#     --accel kvm [--workspace target/wildcard-bind] [--timeout 900]

set -eu

REPO_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
CASE_SRC="$REPO_ROOT/tools/x86/net/wildcard_bind_check.c"
GATE="$REPO_ROOT/tools/x86/debian/daily_use_gate.sh"

KERNEL=""
INITRAMFS=""
BASE_ROOT=""
ACCEL="${X86_DEBIAN_XORG_ACCEL:-kvm}"
WORKSPACE="$REPO_ROOT/target/wildcard-bind"
TIMEOUT=900

usage() {
    sed -n '4,24p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --kernel) KERNEL=$2; shift 2 ;;
        --initramfs) INITRAMFS=$2; shift 2 ;;
        --base-root) BASE_ROOT=$2; shift 2 ;;
        --accel) ACCEL=$2; shift 2 ;;
        --workspace) WORKSPACE=$2; shift 2 ;;
        --timeout) TIMEOUT=$2; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

[ -n "$KERNEL" ] || { echo "--kernel is required" >&2; exit 2; }
[ -n "$INITRAMFS" ] || { echo "--initramfs is required" >&2; exit 2; }
[ -n "$BASE_ROOT" ] || { echo "--base-root is required" >&2; exit 2; }
[ -f "$CASE_SRC" ] || { echo "missing $CASE_SRC" >&2; exit 2; }

mkdir -p "$WORKSPACE"
CHECKLIST="$WORKSPACE/guest-checklist.sh"
CONSOLE="$WORKSPACE/console.txt"

# The guest half: install gcc if the image has none, build the case, run both
# modes, and print one greppable line per case.
{
    cat <<'EOF'
#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive
W=/var/tmp/wb
rm -rf "$W"
mkdir -p "$W"
if ! command -v gcc >/dev/null 2>&1; then
    timeout 240 apt-get update >"$W/apt.log" 2>&1 || true
    timeout 300 apt-get install -y gcc >>"$W/apt.log" 2>&1 || true
fi
if ! command -v gcc >/dev/null 2>&1; then
    echo "WB compile=no-gcc"
else
    cat >"$W/wb.c" <<'CEOF'
EOF
    cat "$CASE_SRC"
    cat <<'EOF'
CEOF
    if gcc -O2 -Wall -o "$W/wb" "$W/wb.c" 2>"$W/cc.err"; then
        echo "WB compile=ok"
        echo "WB bind: $(timeout 12 "$W/wb" bind 2>&1 | tr '\n' '|')"
        echo "WB nobind: $(timeout 12 "$W/wb" nobind 2>&1 | tr '\n' '|')"
        echo "WB udp: $(grep -av '^  sl' /proc/net/udp | head -3 | tr '\n' '|')"
    else
        echo "WB compile=fail $(tr '\n' '|' <"$W/cc.err" | cut -c1-200)"
    fi
fi
echo "DAILY_WB=ok wildcard-bind"
echo "DAILY_DONE=ok"
EOF
} > "$CHECKLIST"

echo "workspace: $WORKSPACE"
DAILY_USE_CHECKLIST="$CHECKLIST" "$GATE" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --base-root "$BASE_ROOT" \
    --accel "$ACCEL" \
    --workspace "$WORKSPACE" \
    --timeout "$TIMEOUT" >"$WORKSPACE/gate.log" 2>&1 || true

echo "--- guest results ---"
grep -a '^WB ' "$CONSOLE" | sed 's/\r$//' || true

bind_line=$(grep -a '^WB bind:' "$CONSOLE" | sed 's/\r$//' | tail -1)
nobind_line=$(grep -a '^WB nobind:' "$CONSOLE" | sed 's/\r$//' | tail -1)

bind_ok=0
nobind_ok=0
case "$bind_line" in *"RESULT bind PASS"*) bind_ok=1 ;; esac
case "$nobind_line" in *"RESULT nobind PASS"*) nobind_ok=1 ;; esac

echo "--- verdict ---"
echo "bind=$([ $bind_ok -eq 1 ] && echo PASS || echo FAIL) nobind=$([ $nobind_ok -eq 1 ] && echo PASS || echo FAIL)"
if [ $bind_ok -eq 1 ] && [ $nobind_ok -eq 1 ]; then
    echo "wildcard bind: OK"
    exit 0
fi
echo "wildcard bind: BROKEN (console: $CONSOLE)"
exit 1
