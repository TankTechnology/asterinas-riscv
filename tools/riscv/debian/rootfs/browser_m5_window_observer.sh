#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Low-frequency X11 observer for Firefox's unusually slow Asterinas startup.
# A large Firefox window is only a diagnostic readiness hint.  The exact probe
# title remains a separate marker and neither marker replaces the content gate.

set -euo pipefail

readonly DISPLAY_NAME="${ASTERINAS_BROWSER_M5_DISPLAY:-:0}"
readonly XAUTHORITY_FILE="${ASTERINAS_BROWSER_M5_XAUTHORITY:-/home/asterinas/.Xauthority}"
readonly CONSOLE="${ASTERINAS_BROWSER_M5_WINDOW_CONSOLE:-/dev/console}"
readonly WINDOW_LOG="${ASTERINAS_BROWSER_M5_WINDOW_LOG:-/home/asterinas/firefox-m5-window-progress.log}"
readonly NAVIGATOR_READY_FILE="${ASTERINAS_BROWSER_M5_NAVIGATOR_READY_FILE:-/home/asterinas/.mozilla/asterinas-browser-m5/NavigatorWindowReady}"
readonly PARENT_PID="${ASTERINAS_BROWSER_M5_PARENT_PID:-0}"
readonly SAMPLE_SECONDS="${ASTERINAS_BROWSER_M5_WINDOW_SAMPLE_SECONDS:-30}"
readonly SAMPLE_LIMIT="${ASTERINAS_BROWSER_M5_WINDOW_SAMPLE_LIMIT:-240}"
readonly SLEEP_COMMAND="${ASTERINAS_BROWSER_M5_SLEEP_COMMAND:-/usr/bin/sleep}"
readonly SYNC_COMMAND="${ASTERINAS_BROWSER_M5_SYNC_COMMAND:-/usr/bin/sync}"
readonly XWININFO_COMMAND="${ASTERINAS_BROWSER_M5_XWININFO_COMMAND:-/usr/bin/xwininfo}"
readonly MINIMUM_WIDTH=640
readonly MINIMUM_HEIGHT=480

[[ "$PARENT_PID" =~ ^(0|[1-9][0-9]*)$ ]] || exit 2
[[ "$SAMPLE_SECONDS" =~ ^[1-9][0-9]*$ ]] || exit 2
[[ "$SAMPLE_LIMIT" =~ ^[1-9][0-9]*$ ]] || exit 2

emit() { printf '%s\n' "$1" >>"$CONSOLE"; }

/usr/bin/rm -f -- "$NAVIGATOR_READY_FILE"
navigator_emitted=false
exec >>"$WINDOW_LOG" 2>&1

for ((sequence = 1; sequence <= SAMPLE_LIMIT; sequence++)); do
    "$SLEEP_COMMAND" "$SAMPLE_SECONDS"
    if ((PARENT_PID > 1)) && ! kill -0 "$PARENT_PID" 2>/dev/null; then
        emit "ASTERINAS_FIREFOX_X11_OBSERVER_EXIT reason=parent-gone sequence=$sequence seconds=$SECONDS"
        exit 0
    fi

    printf '=== sample=%s seconds=%s ===\n' "$sequence" "$SECONDS"
    window_tree="$(DISPLAY="$DISPLAY_NAME" XAUTHORITY="$XAUTHORITY_FILE" \
        "$XWININFO_COMMAND" -root -tree 2>&1 || true)"
    printf '%s\n' "$window_tree"
    lower_tree="${window_tree,,}"

    if [[ "$navigator_emitted" == false ]]; then
        while IFS= read -r window_line; do
            lower_line="${window_line,,}"
            [[ "$lower_line" == *'("firefox-esr"'* ]] || continue
            geometry_pattern=' ([0-9]+)x([0-9]+)[+-]'
            [[ "$window_line" =~ $geometry_pattern ]] || continue
            width="${BASH_REMATCH[1]}"
            height="${BASH_REMATCH[2]}"
            if ((width >= MINIMUM_WIDTH && height >= MINIMUM_HEIGHT)); then
                printf 'browser_pid=%s sequence=%s seconds=%s\n' \
                    "$PARENT_PID" "$sequence" "$SECONDS" >"$NAVIGATOR_READY_FILE"
                "$SYNC_COMMAND"
                emit "ASTERINAS_FIREFOX_X11_NAVIGATOR_VISIBLE browser_pid=$PARENT_PID sequence=$sequence seconds=$SECONDS geometry=${width}x${height}"
                navigator_emitted=true
                break
            fi
        done <<<"$window_tree"
    fi

    if [[ "$lower_tree" == *"asterinas offline browser m5 probe"* ]]; then
        "$SYNC_COMMAND"
        emit "ASTERINAS_FIREFOX_X11_WINDOW_READY browser_pid=$PARENT_PID sequence=$sequence seconds=$SECONDS"
        exit 0
    fi
    "$SYNC_COMMAND"
done

emit "ASTERINAS_FIREFOX_X11_WINDOW_TIMEOUT samples=$SAMPLE_LIMIT seconds=$SECONDS"
