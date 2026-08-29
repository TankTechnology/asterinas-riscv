#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly DISPLAY=:0
readonly FIREFOX_HOME=/home/asterinas
readonly XAUTHORITY="$FIREFOX_HOME/.Xauthority"
readonly PROFILE="$FIREFOX_HOME/.mozilla/asterinas-browser-web"
readonly START_URL="https://www.baidu.com/"
readonly STDERR_LOG="$FIREFOX_HOME/firefox-web-stderr.log"
readonly MOZILLA_LOG="$FIREFOX_HOME/firefox-web-mozilla.log"
readonly CONSOLE="${ASTERINAS_BROWSER_WEB_CONSOLE:-/dev/console}"
readonly TIMELINE="$FIREFOX_HOME/browser-web-timeline.log"
readonly TIMEOUT_SECONDS=30
export DISPLAY XAUTHORITY

guest_monotonic_ns() {
    local raw="${EPOCHREALTIME-}"
    if [[ "$raw" =~ ^[0-9]+\.[0-9]{6}$ ]]; then
        raw="${raw/./}"
        printf '%s000' "$raw"
    else
        printf '%s000000000' "${EPOCHSECONDS:-0}"
    fi
}

marker() {
    local name="$1" line guest_ns
    guest_ns="$(guest_monotonic_ns)"
    line="A_WEB_TIMELINE marker=$name guest_monotonic_ns=$guest_ns firefox_pid=$$"
    printf '%s\n' "$line" >>"$TIMELINE"
    # Console access is best-effort for the unprivileged browser user.  Keep
    # the failed redirection itself out of stderr so it cannot obscure the
    # Firefox diagnostics we are trying to capture.
    (printf '%s\n' "$line" >>"$CONSOLE") 2>/dev/null || true
}

printf 'ASTERINAS_FIREFOX_WEB wrapper-start pid=%s\n' "$$"
if [[ ! -S /tmp/.X11-unix/X0 ]]; then
    /usr/bin/Xorg :0 -noreset -nolisten tcp -extension GLX \
        -extension MIT-SHM -logfile "$FIREFOX_HOME/Xorg.0.log" \
        >>"$FIREFOX_HOME/desktop-m5-session.log" 2>&1 &
fi
/usr/lib/asterinas/browser-web-timeline wait-x
marker BOOT_FIREFOX_WRAPPER_START
/usr/bin/mkdir -p -- "$PROFILE"
export MOZ_LOG='timestamp,Widget:2,Marionette:2,nsHttp:3,nsHostResolver:3'
export MOZ_LOG_FILE="$MOZILLA_LOG"
export MOZ_SANDBOX_LOGGING=1
export MOZ_AVOID_OPENGL_ALTOGETHER=1
printf 'ASTERINAS_FIREFOX_WEB_EXEC pid=%s\n' "$$"
marker BOOT_FIREFOX_EXEC
# Keep Firefox's stderr in its persistent evidence file while mirroring it to
# the service journal.  The Firefox process remains the systemd MainPID
# because the final exec below is unchanged; tail is merely a diagnostic
# child and is killed with the service on restart.
/usr/bin/tail -n 0 -f "$STDERR_LOG" >&2 &
exec /usr/bin/firefox-esr --no-remote --new-instance --marionette \
    --profile "$PROFILE" "$START_URL" >>"$STDERR_LOG" 2>&1
