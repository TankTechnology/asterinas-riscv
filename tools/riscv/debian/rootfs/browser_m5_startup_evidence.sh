#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Cheap readiness probe for Firefox M5. It deliberately does not create a
# Marionette session or inspect page content; the full content gate owns that.

set -euo pipefail

readonly CONSOLE="${ASTERINAS_BROWSER_M5_STARTUP_CONSOLE:-/dev/console}"
readonly LOG="${ASTERINAS_BROWSER_M5_STARTUP_LOG:-/home/asterinas/firefox-m5-startup.log}"
readonly PROFILE="${ASTERINAS_BROWSER_M5_PROFILE:-/home/asterinas/.mozilla/asterinas-browser-m5}"
readonly XORG_LOG="${ASTERINAS_BROWSER_M5_XORG_LOG:-/home/asterinas/Xorg.0.log}"
readonly PROC_ROOT="${ASTERINAS_BROWSER_M5_PROC_ROOT:-/proc}"
readonly INTERVAL_SECONDS="${ASTERINAS_BROWSER_M5_STARTUP_INTERVAL_SECONDS:-5}"
readonly TIMEOUT_SECONDS="${ASTERINAS_BROWSER_M5_STARTUP_TIMEOUT_SECONDS:-600}"

[[ "$INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]] || exit 2
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || exit 2
((TIMEOUT_SECONDS <= 600)) || exit 2

emit() { printf '%s\n' "$1" >>"$CONSOLE"; }
fail() {
    emit "DEBIAN_BROWSER_M5_STARTUP_FAIL reason=$1"
    exit 1
}

exec >>"$LOG" 2>&1
deadline=$((SECONDS + TIMEOUT_SECONDS))
while ((SECONDS < deadline)); do
    browser_pid="$(systemctl show --property MainPID --value asterinas-browser-m5.service 2>/dev/null || true)"
    if [[ "$browser_pid" =~ ^[1-9][0-9]*$ ]] && [[ -r "$PROC_ROOT/$browser_pid/comm" ]]; then
        comm="$(cat "$PROC_ROOT/$browser_pid/comm" 2>/dev/null || true)"
        command_line="$(tr '\0' ' ' <"$PROC_ROOT/$browser_pid/cmdline" 2>/dev/null || true)"
        if [[ "$comm" != firefox-esr ]]; then
            # systemd reports the ExecStart wrapper as MainPID until it execs
            # Firefox.  Treat that short transition as pending, not as a
            # browser failure; an explicit failed unit is handled below.
            if systemctl is-failed --quiet asterinas-browser-m5.service 2>/dev/null; then
                fail firefox-process-exit
            fi
            sleep "$INTERVAL_SECONDS"
            continue
        fi
        [[ "$command_line" == *" --marionette "* ]] || {
            sleep "$INTERVAL_SECONDS"
            continue
        }
        [[ "$command_line" != *"--no-sandbox"* ]] || fail sandbox-disabled
        grep -Eq '^NoNewPrivs:[[:space:]]+1$' "$PROC_ROOT/$browser_pid/status" || {
            sleep "$INTERVAL_SECONDS"
            continue
        }
        grep -q 'FBDEV(0)' "$XORG_LOG" 2>/dev/null || {
            sleep "$INTERVAL_SECONDS"
            continue
        }
        grep -q 'Adding extended input device.*Asterinas keyboard' "$XORG_LOG" 2>/dev/null || {
            sleep "$INTERVAL_SECONDS"
            continue
        }
        grep -q 'Adding extended input device.*Asterinas pointer' "$XORG_LOG" 2>/dev/null || {
            sleep "$INTERVAL_SECONDS"
            continue
        }
        [[ "$(cat "$PROFILE/MarionetteActivePort" 2>/dev/null || true)" == 2828 ]] || {
            sleep "$INTERVAL_SECONDS"
            continue
        }
        [[ -s "$PROFILE/NavigatorWindowReady" ]] || {
            sleep "$INTERVAL_SECONDS"
            continue
        }
        emit "DEBIAN_BROWSER_M5_STARTUP_READY firefox=esr xorg=fbdev marionette=loopback sandbox=normal"
        exit 0
    fi
    if systemctl is-failed --quiet asterinas-browser-m5.service 2>/dev/null; then
        fail firefox-process-exit
    fi
    sleep "$INTERVAL_SECONDS"
done

fail timeout
