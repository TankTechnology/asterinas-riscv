#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_BROWSER_M6_CONSOLE:-/dev/console}"
readonly PROC_ROOT="${ASTERINAS_BROWSER_M6_PROC_ROOT:-/proc}"
readonly TIMEOUT_SECONDS="${ASTERINAS_BROWSER_M6_TIMEOUT_SECONDS:-60}"
readonly COMMAND_TIMEOUT_SECONDS="${ASTERINAS_BROWSER_M6_COMMAND_TIMEOUT_SECONDS:-10}"
readonly CAPTURE_DELAY_SECONDS="${ASTERINAS_BROWSER_M6_CAPTURE_DELAY_SECONDS:-5}"
readonly POLL_DELAY_SECONDS="${ASTERINAS_BROWSER_M6_POLL_DELAY_SECONDS:-1}"
readonly LOCAL_URL="file:///usr/share/asterinas/desktop-m6-javascript.html"
readonly USER_ID=1000
export DISPLAY=:0
export XAUTHORITY=/home/asterinas/.Xauthority

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

fail() {
    emit "DEBIAN_BROWSER_M6_FAIL reason=$1"
    exit 1
}

[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-timeout
[[ "$COMMAND_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-command-timeout
[[ "$CAPTURE_DELAY_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-capture-delay
[[ "$POLL_DELAY_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-poll-delay

timeout "$COMMAND_TIMEOUT_SECONDS" xdotool set_desktop 1 || fail workspace-select
window_output="$(
    timeout "$COMMAND_TIMEOUT_SECONDS" \
        xdotool search --onlyvisible --class NetSurf
)" || fail window-search
((${#window_output} <= 4096)) || fail window-search-output-too-long
mapfile -t windows <<<"$window_output"
((${#windows[@]} == 1)) || fail ambiguous-window
readonly window_id="${windows[0]}"
[[ "$window_id" =~ ^[1-9][0-9]*$ ]] || fail invalid-window
window_title() {
    local title
    title="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" xdotool getwindowname "$window_id"
    )" || fail window-title
    ((${#title} <= 2048)) || fail window-title-too-long
    [[ "$title" != *$'\n'* ]] || fail invalid-window-title
    printf '%s' "$title"
}

emit_title_diagnostic() {
    local title_hex
    title_hex="$(
        printf '%s' "$title" | od -An -v -tx1 | tr -d ' \n'
    )" || fail title-diagnostic
    emit "DEBIAN_BROWSER_M6_DIAGNOSTIC title_hex=$title_hex"
}

deadline=$((SECONDS + TIMEOUT_SECONDS))
while true; do
    title="$(window_title)"
    title_lower="${title,,}"
    if [[ "$title_lower" == *baidu* || "$title_lower" == *result.png* || \
        "$title" == *百度* ]]; then
        break
    fi
    if ((SECONDS >= deadline)); then
        emit_title_diagnostic
        fail remote-title-timeout
    fi
    sleep "$POLL_DELAY_SECONDS"
done
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool windowactivate --sync "$window_id" || fail window-activate
emit "DEBIAN_BROWSER_M6_REMOTE host=www.baidu.com resource=logo-png foreground=active"
sleep "$CAPTURE_DELAY_SECONDS"

process_output="$(
    timeout "$COMMAND_TIMEOUT_SECONDS" pgrep -u "$USER_ID" -x netsurf-gtk
)" || fail process-search
((${#process_output} <= 128)) || fail process-search-output-too-long
mapfile -t processes <<<"$process_output"
((${#processes[@]} == 1)) || fail ambiguous-process
readonly process_id="${processes[0]}"
[[ "$process_id" =~ ^[1-9][0-9]*$ ]] || fail invalid-process
readonly command_line_path="$PROC_ROOT/$process_id/cmdline"
[[ -f "$command_line_path" && ! -L "$command_line_path" ]] || fail process-cmdline
javascript_requested=false
while IFS= read -r argument; do
    if [[ "$argument" == --enable_javascript=1 ]]; then
        javascript_requested=true
    fi
done < <(tr '\0' '\n' <"$command_line_path")

timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key ctrl+l || fail navigation-focus
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool type --delay 0 -- "$LOCAL_URL" || fail navigation-type
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key Return || fail navigation-submit

deadline=$((SECONDS + TIMEOUT_SECONDS))
javascript_status=""
while [[ -z "$javascript_status" ]]; do
    title="$(window_title)"
    if [[ "$title" == *ASTERINAS_JS_PASS* ]]; then
        javascript_status=limited-pass
    elif [[ "$title" == *ASTERINAS_JS_PENDING* ]] && ! $javascript_requested; then
        javascript_status=disabled
    elif ((SECONDS >= deadline)); then
        javascript_status=failed
    else
        sleep "$POLL_DELAY_SECONDS"
    fi
done

emit "DEBIAN_BROWSER_M6_JAVASCRIPT status=$javascript_status"
emit "DEBIAN_BROWSER_M6_READY remote=baidu javascript=$javascript_status"
