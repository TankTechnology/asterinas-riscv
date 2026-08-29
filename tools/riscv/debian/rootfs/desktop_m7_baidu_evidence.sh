#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_BROWSER_M7_CONSOLE:-/dev/console}"
readonly PROC_ROOT="${ASTERINAS_BROWSER_M7_PROC_ROOT:-/proc}"
readonly TIMEOUT_SECONDS="${ASTERINAS_BROWSER_M7_TIMEOUT_SECONDS:-60}"
readonly COMMAND_TIMEOUT_SECONDS="${ASTERINAS_BROWSER_M7_COMMAND_TIMEOUT_SECONDS:-10}"
readonly CAPTURE_DELAY_SECONDS="${ASTERINAS_BROWSER_M7_CAPTURE_DELAY_SECONDS:-30}"
readonly FOCUS_DELAY_SECONDS="${ASTERINAS_BROWSER_M7_FOCUS_DELAY_SECONDS:-1}"
readonly POLL_DELAY_SECONDS="${ASTERINAS_BROWSER_M7_POLL_DELAY_SECONDS:-1}"
readonly NETSURF_LOG="${ASTERINAS_BROWSER_M7_NETSURF_LOG:-/home/asterinas/netsurf-m7.log}"
readonly HOME_URL='https://m.baidu.com/'
readonly SEARCH_QUERY='asterinas'
readonly SEARCH_URL="https://m.baidu.com/s?word=$SEARCH_QUERY"
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

window_id=""
process_id=""

find_single_window() {
    local window_output
    local -a windows=()
    window_output="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" \
            xdotool search --onlyvisible --class NetSurf
    )" || return 1
    ((${#window_output} <= 4096)) || fail window-search-output-too-long
    mapfile -t windows <<<"$window_output"
    ((${#windows[@]} == 1)) || return 1
    [[ "${windows[0]}" =~ ^[1-9][0-9]*$ ]] || fail invalid-window
    window_id="${windows[0]}"
}

find_single_process() {
    local process_output
    local command_line_path
    local -a processes=()
    process_output="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" pgrep -u "$USER_ID" -x netsurf-gtk
    )" || return 1
    ((${#process_output} <= 128)) || fail process-search-output-too-long
    mapfile -t processes <<<"$process_output"
    ((${#processes[@]} == 1)) || return 1
    [[ "${processes[0]}" =~ ^[1-9][0-9]*$ ]] || fail invalid-process
    process_id="${processes[0]}"
    command_line_path="$PROC_ROOT/$process_id/cmdline"
    [[ -f "$command_line_path" && ! -L "$command_line_path" ]] ||
        fail process-cmdline
    tr '\0' '\n' <"$command_line_path" | grep -Fxq /usr/bin/netsurf-gtk ||
        fail process-command
}

wait_for_browser_exit() {
    local deadline=$((SECONDS + TIMEOUT_SECONDS))
    while timeout "$COMMAND_TIMEOUT_SECONDS" \
        pgrep -u "$USER_ID" -x netsurf-gtk >/dev/null 2>&1; do
        ((SECONDS < deadline)) || fail browser-exit-timeout
        sleep "$POLL_DELAY_SECONDS"
    done
}

wait_for_browser_start() {
    local deadline=$((SECONDS + TIMEOUT_SECONDS))
    while true; do
        if find_single_process && find_single_window; then
            return
        fi
        ((SECONDS < deadline)) || fail browser-start-timeout
        sleep "$POLL_DELAY_SECONDS"
    done
}

find_single_process || fail process-search
find_single_window || fail window-search

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

emit_netsurf_log_diagnostic() {
    local tail_hex
    if [[ ! -f "$NETSURF_LOG" || -L "$NETSURF_LOG" ]]; then
        emit "DEBIAN_BROWSER_M7_NETSURF_LOG unavailable=missing-or-unsafe"
        return
    fi
    tail_hex="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" tail -c 2048 -- "$NETSURF_LOG" |
            od -An -v -tx1 | tr -d ' \n'
    )" || {
        emit "DEBIAN_BROWSER_M7_NETSURF_LOG unavailable=read-failed"
        return
    }
    emit "DEBIAN_BROWSER_M7_NETSURF_LOG tail_hex=$tail_hex"
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
            emit_netsurf_log_diagnostic
            emit_title_diagnostic home
            fail home-title-timeout
        fi
        sleep "$POLL_DELAY_SECONDS"
    done
}

timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool windowactivate --sync "$window_id" || fail old-window-activate
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key ctrl+q || fail old-browser-quit
wait_for_browser_exit
[[ ! -L "$NETSURF_LOG" ]] || fail unsafe-netsurf-log
: >"$NETSURF_LOG"
chmod 0600 "$NETSURF_LOG"
runuser -u asterinas -- /usr/bin/env \
    DISPLAY=:0 \
    XAUTHORITY=/home/asterinas/.Xauthority \
    HOME=/home/asterinas \
    /usr/bin/netsurf-gtk -v --enable_javascript=0 "$HOME_URL" \
    >>"$NETSURF_LOG" 2>&1 &
wait_for_browser_start
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool windowactivate --sync "$window_id" || fail window-activate
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool windowfocus --sync "$window_id" || fail window-focus
sleep "$FOCUS_DELAY_SECONDS"
wait_for_home_title
sleep "$CAPTURE_DELAY_SECONDS"
find_single_process || fail home-process
find_single_window || fail home-window
emit "DEBIAN_BROWSER_M7_HOME url=https://m.baidu.com/ variant=mobile title=baidu process=netsurf"

timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool mousemove --sync --window "$window_id" 500 17 || fail search-focus
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool click 1 || fail search-click
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key ctrl+a || fail search-select
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool type --delay 0 -- "$SEARCH_URL" || fail search-type
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key Return || fail search-submit
find_single_process || fail search-process
find_single_window || fail search-window
emit "DEBIAN_BROWSER_M7_SEARCH query=asterinas state=submitted"
emit "DEBIAN_BROWSER_M7_READY page=baidu capture=pending"
