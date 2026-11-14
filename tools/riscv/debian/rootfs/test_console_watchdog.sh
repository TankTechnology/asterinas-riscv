#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Exercise the console watchdog without a board.
#
#   tools/riscv/debian/rootfs/test_console_watchdog.sh
#
# The script under test reads the console's state through `systemctl show`
# and a fake /proc, so a directory of stubs is enough to drive every case the
# 2026-11-11 accident and its recovery depend on.
#
# Cases, all with a 0 s interval so the loop spins while the test watches:
#   healthy      state R for a few ticks -- nothing may be restarted
#   stopped      state T (the SIGTTIN wedge) -- restarted, never rebooted
#   gone         no main pid -- restarted
#   hopeless     every restart fails -- after max_restarts the board reboots
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SCRIPT=${SCRIPT:-$HERE/asterinas_console_watchdog.sh}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
failures=0

# stub_dir: writes systemctl/reboot/sync stubs and prints their state dir.
stub_dir() {
    local dir=$1
    mkdir -p "$dir/bin" "$dir/proc/4242" "$dir/log"
    cat >"$dir/bin/systemctl" <<EOF
#!/bin/sh
echo "systemctl \$*" >>"$dir/log/calls"
case "\$*" in
    *show*) echo 4242 ;;
    *restart*)
        # A successful restart brings the console back: model that, so the
        # watchdog can be seen stopping at one restart instead of escalating.
        echo "4242 (bash) R 1 1 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 123 456 789" \\
            >"$dir/proc/4242/stat"
        ;;
esac
exit 0
EOF
    cat >"$dir/bin/reboot" <<EOF
#!/bin/sh
echo "reboot \$*" >>"$dir/log/calls"
exit 0
EOF
    cat >"$dir/bin/sync" <<EOF
#!/bin/sh
exit 0
EOF
    chmod +x "$dir/bin/systemctl" "$dir/bin/reboot" "$dir/bin/sync"
    echo "$dir"
}

# stat_line <pid> <state>: a plausible /proc/<pid>/stat.
stat_line() {
    echo "$1 (bash) $2 1 1 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 123 456 789"
}

run_case() { # name, state-char-or-'none', expect(restart|reboot|quiet), recover(yes|no)
    local name=$1 state=$2 expect=$3 recover=${4:-yes}
    local dir; dir=$(stub_dir "$WORK/$name")
    if [ "$state" = none ]; then
        rm -f "$dir/proc/4242/stat"
    else
        stat_line 4242 "$state" >"$dir/proc/4242/stat"
    fi
    if [ "$recover" = no ]; then
        # Take the console back out again right after every restart, so the
        # escalation path is exercised.
        sed -i "s|^exit 0\$|rm -f '$dir/proc/4242/stat'; exit 0|" "$dir/bin/systemctl"
    fi
    PATH="$dir/bin:$PATH" \
    ASTERINAS_CONSOLE_WATCHDOG_INTERVAL=1 \
    ASTERINAS_CONSOLE_WATCHDOG_FAILURES=1 \
    ASTERINAS_CONSOLE_WATCHDOG_MAX_RESTARTS=2 \
    ASTERINAS_CONSOLE_PROC_ROOT="$dir/proc" \
    timeout 8 sh "$SCRIPT" >"$dir/log/console" 2>&1

    local got=quiet
    grep -q "^reboot " "$dir/log/calls" 2>/dev/null && got=reboot
    [ "$got" = quiet ] && grep -q "systemctl restart" "$dir/log/calls" 2>/dev/null && got=restart

    if [ "$got" = "$expect" ]; then
        echo "pass $name: $got"
    else
        echo "FAIL $name: expected $expect, got $got"
        sed 's/^/    /' "$dir/log/console" | tail -6
        failures=$((failures + 1))
    fi
}

run_case healthy   R     quiet
run_case healthy-s S     quiet
run_case stopped   T     restart
run_case gone      none  restart
run_case hopeless  T     reboot no

echo
if [ "$failures" -eq 0 ]; then
    echo "console watchdog: OK"
    exit 0
fi
echo "console watchdog: $failures case(s) failed"
exit 1
