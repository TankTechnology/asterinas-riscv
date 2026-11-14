#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Keep the serial console usable.
#
# The console is the last channel that still works when the portal has blocked
# the network and everything else is running fine. It has wedged twice, both
# times in the same way: a binary pushed through `cat` on a canonical-mode tty
# left the shell stopped (SIGTTIN) or without a reader, while the port went on
# echoing. The board looked alive on the wire and nobody could type at it, and
# by then both the kernel deadline and the userspace timer had been disarmed.
#
# `Restart=always` cannot help with that: the process is alive, it is just not
# reading. So this checks the console's main process directly -- gone, stopped
# (T) or a zombie means the channel is unusable regardless of what systemd
# thinks -- restarts the unit, and reboots if the restarts keep failing. A
# console that survives three restarts is not going to fix itself, and every
# other recovery layer is off by then.
#
# Markers go to the console as well as the journal, so the wire shows what
# happened even when nobody can log in to read the journal.
set -u

unit="${ASTERINAS_CONSOLE_UNIT:-asterinas-debug-console.service}"
interval="${ASTERINAS_CONSOLE_WATCHDOG_INTERVAL:-60}"
failures_before_restart="${ASTERINAS_CONSOLE_WATCHDOG_FAILURES:-2}"
max_restarts="${ASTERINAS_CONSOLE_WATCHDOG_MAX_RESTARTS:-3}"
# Overridden by the host-side test, which has no board to inspect.
proc_root="${ASTERINAS_CONSOLE_PROC_ROOT:-/proc}"

# One of: ok, stopped, zombie, gone, unknown.
console_state() {
    pid=$(systemctl show -p MainPID --value "$unit" 2>/dev/null)
    case "$pid" in
        ''|*[!0-9]*) echo gone; return ;;
    esac
    [ "$pid" -gt 0 ] || { echo gone; return; }
    [ -r "$proc_root/$pid/stat" ] || { echo gone; return; }
    # Field 3 of /proc/<pid>/stat, found after the last ')': the command name
    # may itself contain spaces and parentheses.
    state=$(sed -n 's/^[^)]*) \(.\).*$/\1/p' "$proc_root/$pid/stat" 2>/dev/null)
    case "$state" in
        T|t) echo stopped ;;
        Z) echo zombie ;;
        R|S|D|I) echo ok ;;
        '') echo gone ;;
        *) echo unknown ;;
    esac
}

announce() {
    printf 'ASTERINAS_CONSOLE_WATCHDOG %s\n' "$*"
}

failures=0
restarts=0
reported=0

while :; do
    state=$(console_state)
    if [ "$state" = ok ]; then
        if [ "$reported" -eq 1 ]; then
            announce "state=recovered restarts=$restarts"
        fi
        failures=0
        restarts=0
        reported=0
        sleep "$interval"
        continue
    fi

    failures=$((failures + 1))
    if [ "$reported" -eq 0 ]; then
        announce "state=unusable detail=$state failures=$failures"
        reported=1
    fi

    if [ "$failures" -ge "$failures_before_restart" ]; then
        restarts=$((restarts + 1))
        if [ "$restarts" -gt "$max_restarts" ]; then
            # Nothing else is left: the kernel deadline and the userspace
            # timer were both disarmed once the desktop came up.
            announce "state=reboot restarts=$restarts"
            sync
            reboot -f
            # If reboot(8) is missing or refuses, the hardware watchdog is
            # still the backstop; keep the marker readable first.
            sleep "$interval"
        else
            announce "state=restart attempt=$restarts detail=$state"
            systemctl restart "$unit" 2>&1
        fi
        failures=0
    fi
    sleep "$interval"
done
