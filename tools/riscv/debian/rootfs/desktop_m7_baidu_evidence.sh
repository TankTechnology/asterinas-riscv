#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_BROWSER_M7_CONSOLE:-/dev/console}"
readonly PROC_ROOT="${ASTERINAS_BROWSER_M7_PROC_ROOT:-/proc}"
readonly TIMEOUT_SECONDS="${ASTERINAS_BROWSER_M7_TIMEOUT_SECONDS:-60}"
readonly COMMAND_TIMEOUT_SECONDS="${ASTERINAS_BROWSER_M7_COMMAND_TIMEOUT_SECONDS:-10}"
readonly CAPTURE_DELAY_SECONDS="${ASTERINAS_BROWSER_M7_CAPTURE_DELAY_SECONDS:-10}"
readonly FOCUS_DELAY_SECONDS="${ASTERINAS_BROWSER_M7_FOCUS_DELAY_SECONDS:-1}"
readonly POLL_DELAY_SECONDS="${ASTERINAS_BROWSER_M7_POLL_DELAY_SECONDS:-1}"
readonly HOME_URL='https://www.baidu.com/'
readonly SEARCH_QUERY='asterinas-riscv'
readonly USER_ID=1000
export DISPLAY=:0
export XAUTHORITY=/home/asterinas/.Xauthority

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

fail() {
    emit "DEBIAN_BROWSER_M7_FAIL reason=$1"
    exit 1
}

[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-timeout
[[ "$COMMAND_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-command-timeout
[[ "$CAPTURE_DELAY_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-capture-delay
[[ "$FOCUS_DELAY_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-focus-delay
[[ "$POLL_DELAY_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-poll-delay

window_output="$(
    timeout "$COMMAND_TIMEOUT_SECONDS" \
        xdotool search --onlyvisible --class netsurf
)" || fail window-search
((${#window_output} <= 4096)) || fail window-search-output-too-long
mapfile -t windows <<<"$window_output"
((${#windows[@]} == 1)) || fail ambiguous-window
readonly window_id="${windows[0]}"
[[ "$window_id" =~ ^[1-9][0-9]*$ ]] || fail invalid-window

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
tr '\0' '\n' <"$command_line_path" | grep -Fxq /usr/bin/netsurf-gtk ||
    fail process-command

window_title() {
    local candidate
    candidate="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" xdotool getwindowname "$window_id"
    )" || fail window-title
    ((${#candidate} <= 2048)) || fail window-title-too-long
    [[ "$candidate" != *$'\n'* ]] || fail invalid-window-title
    printf '%s' "$candidate"
}

emit_title_diagnostic() {
    local phase="$1"
    local title_hex
    title_hex="$(
        printf '%s' "$title" | od -An -v -tx1 | tr -d ' \n'
    )" || fail title-diagnostic
    emit "DEBIAN_BROWSER_M7_DIAGNOSTIC phase=$phase title_hex=$title_hex"
}

wait_for_home_title() {
    local deadline=$((SECONDS + TIMEOUT_SECONDS))
    local title_lower
    while true; do
        title="$(window_title)"
        title_lower="${title,,}"
        if [[ "$title_lower" != *result.png* ]] &&
            { [[ "$title_lower" == *baidu* ]] || [[ "$title" == *百度* ]]; }; then
            return
        fi
        if ((SECONDS >= deadline)); then
            emit_title_diagnostic home
            fail home-title-timeout
        fi
        sleep "$POLL_DELAY_SECONDS"
    done
}

wait_for_search_title() {
    local deadline=$((SECONDS + TIMEOUT_SECONDS))
    local title_lower
    while true; do
        title="$(window_title)"
        title_lower="${title,,}"
        if [[ "$title_lower" == *"$SEARCH_QUERY"* || "$title" == *百度搜索* ]]; then
            return
        fi
        if ((SECONDS >= deadline)); then
            emit_title_diagnostic search
            fail search-title-timeout
        fi
        sleep "$POLL_DELAY_SECONDS"
    done
}

timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool windowactivate --sync "$window_id" || fail window-activate
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool windowfocus --sync "$window_id" || fail window-focus
sleep "$FOCUS_DELAY_SECONDS"
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool mousemove --sync 500 42 || fail home-focus
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool click 1 || fail home-click
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key ctrl+a || fail home-select
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool type --delay 0 -- "$HOME_URL" || fail home-type
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key Return || fail home-submit
wait_for_home_title
emit "DEBIAN_BROWSER_M7_HOME url=https://www.baidu.com/ title=baidu process=netsurf"
sleep "$CAPTURE_DELAY_SECONDS"

timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool mousemove --sync 560 310 || fail search-focus
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool click 1 || fail search-click
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key ctrl+a || fail search-select
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool type --delay 40 -- "$SEARCH_QUERY" || fail search-type
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key Return || fail search-submit
wait_for_search_title
emit "DEBIAN_BROWSER_M7_SEARCH query=asterinas-riscv result=loaded"
emit "DEBIAN_BROWSER_M7_READY page=baidu search=pass"
