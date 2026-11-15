#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

export DISPLAY=:0
export HOME=/home/asterinas

window_id="$(/usr/bin/timeout 3 /usr/bin/xdotool search --class '[Ff]irefox' 2>/dev/null | /usr/bin/tail -n 1 || true)"
if [[ -n "$window_id" ]]; then
    exec /usr/bin/timeout 3 /usr/bin/xdotool windowactivate "$window_id"
fi

# The browser service may still be creating its first window.  Do not launch a
# second expensive instance during that interval.
if /usr/bin/systemctl is-active --quiet asterinas-browser-web.service; then
    exit 0
fi

if [[ -x /usr/bin/firefox ]]; then
    browser=/usr/bin/firefox
else
    browser=/usr/bin/firefox-esr
fi
exec "$browser" --no-remote --new-instance \
    --profile "$HOME/.mozilla/asterinas-browser-web" about:blank
