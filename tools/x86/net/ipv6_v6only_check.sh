#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Reproducer / acceptance test for the SOL_IPV6 IPV6_V6ONLY sockopt.
#
# Boots the Debian guest, compiles tools/x86/net/ipv6_v6only_check.c inside it
# and runs the checks:
#
#   v6only_set            setsockopt(IPPROTO_IPV6, IPV6_V6ONLY, 1) succeeds
#   v6only_get            getsockopt reads the value back as 1
#   v6only_default0       a fresh socket reads back 0 (Linux default)
#   v4_ctrl               SOL_SOCKET/SO_REUSEADDR still works
#   v4_sol6_enoprotoopt   SOL_IPV6 on a v4 socket is rejected with ENOPROTOOPT
#
# Before the fix every IPV6_V6ONLY check fails with EOPNOTSUPP.
# Exit status: 0 when all five checks pass, 1 otherwise.
#
# Usage:
#   tools/x86/net/ipv6_v6only_check.sh \
#     --kernel target/osdk/asterinas-osdk-bin.elf \
#     --initramfs target/debian-x86/stage1/initramfs.cpio \
#     --base-root target/systemd-health/daily.ext2 \
#     --accel kvm [--workspace target/ipv6-v6only] [--timeout 900]

set -eu

REPO_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
CASE_SRC="$REPO_ROOT/tools/x86/net/ipv6_v6only_check.c"
GATE="$REPO_ROOT/tools/x86/debian/daily_use_gate.sh"

KERNEL=""
INITRAMFS=""
BASE_ROOT=""
ACCEL="${X86_DEBIAN_XORG_ACCEL:-kvm}"
WORKSPACE="$REPO_ROOT/target/ipv6-v6only"
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

{
    cat <<'EOF'
#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive
W=/var/tmp/v6only
rm -rf "$W"
mkdir -p "$W"
if ! command -v gcc >/dev/null 2>&1; then
    timeout 240 apt-get update >"$W/apt.log" 2>&1 || true
    timeout 300 apt-get install -y gcc >>"$W/apt.log" 2>&1 || true
fi
if ! command -v gcc >/dev/null 2>&1; then
    echo "V6S compile=no-gcc"
else
    cat >"$W/v6only.c" <<'CEOF'
EOF
    cat "$CASE_SRC"
    cat <<'EOF'
CEOF
    if gcc -O2 -Wall -o "$W/v6only" "$W/v6only.c" 2>"$W/cc.err"; then
        echo "V6S compile=ok"
        out=$(timeout 15 "$W/v6only" 2>&1)
        rc=$?
        printf '%s\n' "$out" | sed 's/^/V6S /'
        echo "V6S exit=$rc"
    else
        echo "V6S compile=fail $(tr '\n' '|' <"$W/cc.err" | cut -c1-200)"
    fi
fi
echo "DAILY_V6S=ok"; echo "DAILY_DONE=ok"
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
grep -a '^V6S ' "$CONSOLE" | sed 's/\r$//' || true

line=$(grep -a '^V6S RESULT all ' "$CONSOLE" | sed 's/\r$//' | tail -1)
case "$line" in
    *"RESULT all PASS"*) echo "v6only: OK"; exit 0 ;;
esac
echo "v6only: FAIL (console: $CONSOLE)"
exit 1
