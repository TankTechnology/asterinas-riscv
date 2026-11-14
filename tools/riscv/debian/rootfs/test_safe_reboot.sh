#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Exercise megrez-safe-reboot's failure path without a board.
#
#   tools/riscv/debian/rootfs/test_safe_reboot.sh
#
# The script under test reads everything through ASTERINAS_SAFE_REBOOT_*
# overrides and calls systemctl/pgrep/pkill/sync/reboot through PATH, so a
# directory of stubs plus a fake /proc is enough to drive it.
#
# Two cases, both past the deadline with the desktop never ready:
#   quiesce-fails   `systemctl stop` keeps failing; the script must end with a
#                   forced restart instead of leaving the board up unguarded
#                   (the 2026-11-14 boot did exactly that and stayed
#                   unreachable for hours).
#   clean-reboot    the clean path works; no forced restart is used.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SCRIPT=${SCRIPT:-$HERE/megrez_safe_reboot.sh}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
failures=0

make_stubs() { # dir, systemctl-exit-code
    local dir=$1 code=$2
    mkdir -p "$dir"
    cat >"$dir/systemctl" <<EOF
#!/bin/sh
echo "systemctl \$*" >>"$dir/calls.log"
exit $code
EOF
    cat >"$dir/reboot" <<EOF
#!/bin/sh
echo "reboot \$*" >>"$dir/calls.log"
exit 0
EOF
    cat >"$dir/pgrep" <<EOF
#!/bin/sh
exit 1
EOF
    cat >"$dir/pkill" <<EOF
#!/bin/sh
echo "pkill \$*" >>"$dir/calls.log"
exit 0
EOF
    cat >"$dir/sync" <<EOF
#!/bin/sh
echo "sync" >>"$dir/calls.log"
exit 0
EOF
    chmod +x "$dir"/systemctl "$dir"/reboot "$dir"/pgrep "$dir"/pkill "$dir"/sync
}

setup_env() { # dir
    local dir=$1
    mkdir -p "$dir/units" "$dir/state"
    : >"$dir/units/asterinas-desktop-m5.service"
    : >"$dir/units/asterinas-browser-web.service"
    echo "console=ttyS0 asterinas.reboot_after=900" >"$dir/cmdline"
    # Past the 720 s userspace deadline.
    echo "800.00 1.00" >"$dir/uptime"
    : >"$dir/console.log"
    echo "00000000-0000-0000-0000-000000000000" >"$dir/boot_id"
    echo "1" >"$dir/watchdog"
}

run_case() { # name, systemctl-code, expect-force(yes/no)
    local name=$1
    local code=$2
    local expect=$3
    local dir="$WORK/$name"
    make_stubs "$dir/bin" "$code"
    setup_env "$dir"
    PATH="$dir/bin:$PATH" \
    ASTERINAS_SAFE_REBOOT_CONSOLE="$dir/console.log" \
    ASTERINAS_SAFE_REBOOT_CMDLINE_PATH="$dir/cmdline" \
    ASTERINAS_SAFE_REBOOT_UPTIME_FILE="$dir/uptime" \
    ASTERINAS_SAFE_REBOOT_UNIT_DIR="$dir/units" \
    ASTERINAS_SAFE_REBOOT_DESKTOP_READY_FILE="$dir/state/desktop-ready" \
    ASTERINAS_SAFE_REBOOT_BOOT_ID_FILE="$dir/boot_id" \
    ASTERINAS_SAFE_REBOOT_WATCHDOG_FILE="$dir/watchdog" \
    ASTERINAS_SAFE_REBOOT_TERM_WAIT_SECONDS=1 \
    ASTERINAS_SAFE_REBOOT_KILL_WAIT_SECONDS=1 \
    timeout 120 bash "$SCRIPT" >/dev/null 2>&1

    local forced=no
    grep -q ASTERINAS_USERSPACE_REBOOT_FORCED "$dir/console.log" && forced=yes
    local rebooted=no
    grep -q '^reboot ' "$dir/bin/calls.log" 2>/dev/null && rebooted=yes

    if [ "$forced" = "$expect" ] && { [ "$expect" = no ] || [ "$rebooted" = yes ]; }; then
        echo "pass $name: forced=$forced rebooted=$rebooted"
    else
        echo "FAIL $name: expected forced=$expect, got $forced (rebooted=$rebooted)"
        sed 's/^/    /' "$dir/console.log" | tail -8
        failures=$((failures + 1))
    fi
}

run_case quiesce-fails 1 yes
run_case clean-reboot  0 no

echo
if [ "$failures" -eq 0 ]; then
    echo "safe-reboot failure path: OK"
    exit 0
fi
echo "safe-reboot failure path: $failures case(s) failed"
exit 1
