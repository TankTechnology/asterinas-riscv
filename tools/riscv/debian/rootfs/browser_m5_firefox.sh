#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly DISPLAY=:0
readonly FIREFOX_HOME=/home/asterinas
readonly XAUTHORITY="$FIREFOX_HOME/.Xauthority"
readonly PROFILE="$FIREFOX_HOME/.mozilla/asterinas-browser-m5"
readonly PROBE_URL="file:///usr/share/asterinas/browser-m5/index.html"
readonly STDERR_LOG="$FIREFOX_HOME/firefox-m5-plain-stderr.log"
readonly MOZILLA_LOG="$FIREFOX_HOME/firefox-m5-plain-mozilla.log"
readonly WINDOW_OBSERVER=/usr/lib/asterinas/browser-m5-window-observer
readonly TIMEOUT_SECONDS=30
export DISPLAY XAUTHORITY

exec 3>&2
exec >>"$STDERR_LOG" 2>&1
printf 'ASTERINAS_FIREFOX_PLAIN wrapper-start pid=%s\n' "$$"

deadline=$((SECONDS + TIMEOUT_SECONDS))
while [[ ! -S /tmp/.X11-unix/X0 ]]; do
    ((SECONDS < deadline)) || exit 1
    /usr/bin/sleep 1
done
/usr/bin/mkdir -p -- "$PROFILE"
export MOZ_LOG='timestamp,Widget:2,Marionette:2'
export MOZ_LOG_FILE="$MOZILLA_LOG"
export MOZ_SANDBOX_LOGGING=1
ASTERINAS_BROWSER_M5_PARENT_PID="$$" \
ASTERINAS_BROWSER_M5_WINDOW_CONSOLE=/proc/self/fd/3 \
    "$WINDOW_OBSERVER" &
printf 'ASTERINAS_FIREFOX_PLAIN_EXEC pid=%s\n' "$$" >&3
exec /usr/bin/firefox-esr --no-remote --new-instance --offline --marionette \
    --profile "$PROFILE" "$PROBE_URL"
