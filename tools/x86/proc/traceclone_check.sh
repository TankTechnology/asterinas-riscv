#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Reproducer / acceptance test for PTRACE_O_TRACECLONE (strace -f).
#
# Boots the Debian guest and runs three checks:
#
#   stracef_threads  strace -f on a program that starts a thread must succeed
#                    and the trace must contain clone events
#   traceclone_c     a TRACEME child creates a thread with TRACECLONE set:
#                    pthread_create must succeed and the tracer must see a
#                    stop for the new thread
#   strace_plain     strace without -f still works (control)
#
# Before the fix the first two fail (EOPNOTSUPP on the traced clone); the
# control passes both before and after.
#
# Usage:
#   tools/x86/proc/traceclone_check.sh \
#     --kernel target/osdk/asterinas-osdk-bin.elf \
#     --initramfs target/debian-x86/stage1/initramfs.cpio \
#     --base-root target/systemd-health/daily.ext2 \
#     --accel kvm [--workspace target/traceclone] [--timeout 900]

set -eu

REPO_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
CASE_SRC="$REPO_ROOT/tools/x86/proc/traceclone_check.c"
GATE="$REPO_ROOT/tools/x86/debian/daily_use_gate.sh"

KERNEL=""
INITRAMFS=""
BASE_ROOT=""
ACCEL="${X86_DEBIAN_XORG_ACCEL:-kvm}"
WORKSPACE="$REPO_ROOT/target/traceclone"
TIMEOUT=900

usage() {
    sed -n '4,26p' "$0" | sed 's/^# \{0,1\}//'
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
W=/var/tmp/tc
rm -rf "$W"
mkdir -p "$W"
if ! command -v gcc >/dev/null 2>&1 || ! command -v strace >/dev/null 2>&1; then
    timeout 240 apt-get update >"$W/apt.log" 2>&1 || true
    timeout 300 apt-get install -y gcc strace >>"$W/apt.log" 2>&1 || true
fi

cat >"$W/traceclone.c" <<'CEOF'
EOF
    cat "$CASE_SRC"
    cat <<'EOF'
CEOF
    if gcc -O2 -Wall -pthread -o "$W/traceclone" "$W/traceclone.c" 2>"$W/cc.err"; then
        echo "TC compile=ok"
        out=$(timeout 20 "$W/traceclone" 2>&1); rc=$?
        printf '%s\n' "$out"
        echo "TC harness_exit=$rc"
    else
        echo "TC compile=fail $(tr '\n' '|' <"$W/cc.err" | cut -c1-200)"
    fi

    # --- strace -f on a threaded program ---
    out=$(timeout 20 strace -f -e trace=clone,clone3 -o "$W/st.log" \
        python3 -c 'import threading; threading.Thread(target=lambda: None).start()' 2>&1)
    rc=$?
    echo "TC stracef_python_rc=$rc clones=$(grep -acE 'clone3?\(' "$W/st.log" 2>/dev/null) out=$(printf '%s' "$out" | tail -1 | cut -c1-160)"
    echo "TC stracef_log: $(grep -aE 'clone3?\(' "$W/st.log" 2>/dev/null | head -3 | tr '\n' '|' | cut -c1-300)"

    out=$(timeout 15 strace -f -e trace=clone,execve /bin/sh -c 'true' 2>&1 >/dev/null); rc=$?
    echo "TC stracef_sh_rc=$rc out=$(printf '%s' "$out" | tail -1 | cut -c1-160)"

    # --- control: strace without -f ---
    out=$(timeout 10 strace -e trace=clone -o "$W/plain.log" /bin/true 2>&1); rc=$?
    echo "TC strace_plain_rc=$rc out=$(printf '%s' "$out" | tail -1 | cut -c1-120)"

    echo "DAILY_TC=ok"
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
grep -a '^TC \|^RESULT \|^SETOPTIONS \|^CLONE ' "$CONSOLE" | sed 's/\r$//' || true

ok=0
grep -aq '^RESULT traceclone PASS' "$CONSOLE" || ok=1
line=$(grep -a '^TC stracef_python_rc=' "$CONSOLE" | sed 's/\r$//' | tail -1)
case "$line" in
    *"rc=0"*) ;;
    *) ok=1 ;;
esac
clones=$(printf '%s' "$line" | sed -n 's/.*clones=\([0-9]*\).*/\1/p')
[ -n "$clones" ] && [ "$clones" -ge 1 ] || ok=1

if [ $ok -eq 0 ]; then
    echo "traceclone: OK"
    exit 0
fi
echo "traceclone: FAIL (console: $CONSOLE)"
exit 1
