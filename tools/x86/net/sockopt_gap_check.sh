#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Reproducer / acceptance test for SOL_IP/IP_MTU_DISCOVER and
# SOL_NETLINK/NETLINK_EXT_ACK (both returned ENOPROTOOPT before the fix).
#
# Boots the Debian guest, compiles tools/x86/net/sockopt_gap_check.c inside it
# and runs the checks:
#
#   mtu_discover_default     fresh v4 UDP socket reads back 1 (Linux WANT)
#   mtu_discover_0..5        set/get round-trip for every value Linux accepts
#   mtu_discover_reject_*    6 and -1 fail with EINVAL
#   v6_sol_ip_accepted_*     SOL_IP on an AF_INET6 socket is accepted (Linux)
#   ext_ack_default          netlink socket reads back 0
#   ext_ack_set_*            1/0/2/-1 round-trip with boolean semantics
#
# Before the fix every set/get returns ENOPROTOOPT.
# Exit status: 0 when all checks pass, 1 otherwise.
#
# Usage:
#   tools/x86/net/sockopt_gap_check.sh \
#     --kernel target/osdk/asterinas-osdk-bin.elf \
#     --initramfs target/debian-x86/stage1/initramfs.cpio \
#     --base-root target/systemd-health/daily.ext2 \
#     --accel kvm [--workspace target/sockopt-gap] [--timeout 900]

set -eu

REPO_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
CASE_SRC="$REPO_ROOT/tools/x86/net/sockopt_gap_check.c"
GATE="$REPO_ROOT/tools/x86/debian/daily_use_gate.sh"

KERNEL=""
INITRAMFS=""
BASE_ROOT=""
ACCEL="${X86_DEBIAN_XORG_ACCEL:-kvm}"
WORKSPACE="$REPO_ROOT/target/sockopt-gap"
TIMEOUT=900

usage() {
    sed -n '4,27p' "$0" | sed 's/^# \{0,1\}//'
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

{
    cat <<'EOF'
#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive
W=/var/tmp/sockopt
rm -rf "$W"
mkdir -p "$W"
if ! command -v gcc >/dev/null 2>&1; then
    timeout 240 apt-get update >"$W/apt.log" 2>&1 || true
    timeout 300 apt-get install -y gcc >>"$W/apt.log" 2>&1 || true
fi
if ! command -v gcc >/dev/null 2>&1; then
    echo "SG compile=no-gcc"
else
    cat >"$W/sockopt.c" <<'CEOF'
EOF
    cat "$CASE_SRC"
    cat <<'EOF'
CEOF
    if gcc -O2 -Wall -o "$W/sockopt" "$W/sockopt.c" 2>"$W/cc.err"; then
        echo "SG compile=ok"
        timeout 15 "$W/sockopt" 2>&1
        echo "SG exit=$?"
    else
        echo "SG compile=fail $(tr '\n' '|' <"$W/cc.err" | cut -c1-200)"
    fi
fi
echo "DAILY_SG=ok"; echo "DAILY_DONE=ok"
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
grep -a '^SG ' "$CONSOLE" | sed 's/\r$//' || true

line=$(grep -a '^SG RESULT all ' "$CONSOLE" | sed 's/\r$//' | tail -1)
case "$line" in
    *"RESULT all PASS"*) echo "sockopt_gap: OK"; exit 0 ;;
esac
echo "sockopt_gap: FAIL (console: $CONSOLE)"
exit 1
