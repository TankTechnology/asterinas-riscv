#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly DISPLAY=:0
readonly HOME=/home/asterinas
readonly XAUTHORITY="$HOME/.Xauthority"
readonly PROFILE="$HOME/.mozilla/asterinas-browser-m5"
readonly PROBE_URL="file:///usr/share/asterinas/browser-m5/index.html"
readonly TIMEOUT_SECONDS=30
export DISPLAY HOME XAUTHORITY

deadline=$((SECONDS + TIMEOUT_SECONDS))
while [[ ! -S /tmp/.X11-unix/X0 ]]; do
    ((SECONDS < deadline)) || exit 1
    /usr/bin/sleep 1
done
/usr/bin/mkdir -p -- "$PROFILE"
exec /usr/bin/firefox-esr --no-remote --new-instance --offline --marionette \
    --profile "$PROFILE" "$PROBE_URL"
