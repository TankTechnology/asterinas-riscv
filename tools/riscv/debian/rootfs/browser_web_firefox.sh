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
    printf '%s\n' "$line" >>"$CONSOLE"
}

exec >>"$STDERR_LOG" 2>&1
printf 'ASTERINAS_FIREFOX_WEB wrapper-start pid=%s\n' "$$"
marker BOOT_FIREFOX_WRAPPER_START
deadline=$((SECONDS + TIMEOUT_SECONDS))
while [[ ! -S /tmp/.X11-unix/X0 ]]; do
    ((SECONDS < deadline)) || exit 1
    /usr/bin/sleep 1
done
/usr/bin/mkdir -p -- "$PROFILE"
export MOZ_LOG='timestamp,Widget:2,Marionette:2,nsHttp:3,nsHostResolver:3'
export MOZ_LOG_FILE="$MOZILLA_LOG"
export MOZ_SANDBOX_LOGGING=1
export MOZ_AVOID_OPENGL_ALTOGETHER=1
printf 'ASTERINAS_FIREFOX_WEB_EXEC pid=%s\n' "$$"
marker BOOT_FIREFOX_EXEC
exec /usr/bin/firefox-esr --no-remote --new-instance --marionette \
    --profile "$PROFILE" "$START_URL"
