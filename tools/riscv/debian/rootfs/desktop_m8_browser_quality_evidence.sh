#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_BROWSER_M8_CONSOLE:-/dev/console}"
readonly PROC_ROOT="${ASTERINAS_BROWSER_M8_PROC_ROOT:-/proc}"
readonly TIMEOUT_SECONDS="${ASTERINAS_BROWSER_M8_TIMEOUT_SECONDS:-300}"
readonly COMMAND_TIMEOUT_SECONDS="${ASTERINAS_BROWSER_M8_COMMAND_TIMEOUT_SECONDS:-30}"
readonly POLL_DELAY_SECONDS="${ASTERINAS_BROWSER_M8_POLL_DELAY_SECONDS:-1}"
readonly SETTLE_DELAY_SECONDS="${ASTERINAS_BROWSER_M8_SETTLE_DELAY_SECONDS:-2}"
readonly DOWNLOAD="${ASTERINAS_BROWSER_M8_DOWNLOAD:-/home/asterinas/Downloads/asterinas-browser-quality.bin}"
readonly CAPTURE="${ASTERINAS_BROWSER_M8_CAPTURE:-/run/asterinas-browser-quality.xwd.gz}"
readonly DOWNLOAD_SIZE=262144
readonly DOWNLOAD_SHA256='2312394bd99545d9de131c24efb781e765ac1aec243f2ed9347597a793a415e9'
readonly CAPTURE_LIMIT=$((8 * 1024 * 1024))
readonly SOAK_SECONDS=120
readonly USER_ID=1000
export DISPLAY=:0
export XAUTHORITY=/home/asterinas/.Xauthority

fixture_base="${ASTERINAS_BROWSER_M8_FIXTURE_URL:-}"
if [[ -z "$fixture_base" && -n "${ASTERINAS_DESKTOP_FIXTURE_URL:-}" ]]; then
    fixture_base="${ASTERINAS_DESKTOP_FIXTURE_URL%/asterinas-network-probe.bin}"
fi
readonly FIXTURE_BASE="${fixture_base%/}"
readonly QUALITY_URL="$FIXTURE_BASE/browser-quality/index.html"
readonly DOWNLOAD_URL="$FIXTURE_BASE/browser-quality/download.bin"
readonly CAPTURE_URL="$FIXTURE_BASE/browser-quality/capture.xwd.gz"

failure_emitted=0
download_owned=0
capture_owned=0
window_id=""
process_id=""
title=""

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

cleanup() {
    if ((download_owned)); then
        rm -f -- "$DOWNLOAD"
    fi
    if ((capture_owned)); then
        rm -f -- "$CAPTURE"
    fi
}

fail() {
    if ((failure_emitted == 0)); then
        failure_emitted=1
        emit "DEBIAN_BROWSER_M8_FAIL reason=$1"
    fi
    exit 1
}

trap cleanup EXIT

[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-timeout
[[ "$COMMAND_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] ||
    fail invalid-command-timeout
[[ "$POLL_DELAY_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] ||
    fail invalid-poll-delay
[[ "$SETTLE_DELAY_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] ||
    fail invalid-settle-delay
[[ "$FIXTURE_BASE" =~ ^http://([0-9]{1,3}\.){3}[0-9]{1,3}:[1-9][0-9]{0,4}$ ]] ||
    fail invalid-fixture-url
fixture_port="${FIXTURE_BASE##*:}"
((fixture_port <= 65535)) || fail invalid-fixture-url
for path in "$DOWNLOAD" "$CAPTURE"; do
    [[ "$path" == /* && "$path" != *$'\n'* && "$path" != *$'\r'* ]] ||
        fail invalid-path
done
[[ "$DOWNLOAD" != "$CAPTURE" ]] || fail invalid-path
[[ ! -e "$DOWNLOAD" && ! -L "$DOWNLOAD" ]] || fail stale-download
[[ ! -L "$CAPTURE" ]] || fail unsafe-capture

readonly DEADLINE=$((SECONDS + TIMEOUT_SECONDS))

check_deadline() {
    ((SECONDS < DEADLINE)) || fail "$1"
}

require_single_browser() {
    local process_output
    local window_output
    local window_process
    local -a processes=()
    local -a windows=()

    process_output="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" pgrep -u "$USER_ID" -x netsurf-gtk
    )" || fail browser-exit
    ((${#process_output} <= 128)) || fail process-search-output-too-long
    mapfile -t processes <<<"$process_output"
    ((${#processes[@]} == 1)) || fail ambiguous-process
    [[ "${processes[0]}" =~ ^[1-9][0-9]*$ ]] || fail invalid-process
    process_id="${processes[0]}"
    [[ -f "$PROC_ROOT/$process_id/cmdline" &&
        ! -L "$PROC_ROOT/$process_id/cmdline" ]] || fail process-cmdline
    tr '\0' '\n' <"$PROC_ROOT/$process_id/cmdline" |
        grep -Fxq /usr/bin/netsurf-gtk || fail process-command

    window_output="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" \
            xdotool search --onlyvisible --classname '^netsurf-gtk$'
    )" || fail ambiguous-window
    ((${#window_output} <= 4096)) || fail window-search-output-too-long
    mapfile -t windows <<<"$window_output"
    ((${#windows[@]} == 1)) || fail ambiguous-window
    [[ "${windows[0]}" =~ ^[1-9][0-9]*$ ]] || fail invalid-window
    window_id="${windows[0]}"
    window_process="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" \
            xprop -id "$window_id" _NET_WM_PID
    )" || fail window-process
    [[ "$window_process" =~ ^_NET_WM_PID\(CARDINAL\)[[:space:]]*=[[:space:]]*([1-9][0-9]*)$ ]] ||
        fail window-process
    [[ "${BASH_REMATCH[1]}" == "$process_id" ]] || fail window-process
}

require_tracked_browser() {
    local window_process

    [[ "$window_id" =~ ^[1-9][0-9]*$ ]] || fail browser-exit
    window_process="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" \
            xprop -id "$window_id" _NET_WM_PID
    )" || fail browser-exit
    [[ "$window_process" =~ ^_NET_WM_PID\(CARDINAL\)[[:space:]]*=[[:space:]]*([1-9][0-9]*)$ ]] ||
        fail browser-exit
    [[ "${BASH_REMATCH[1]}" == "$process_id" ]] || fail browser-exit
    timeout "$COMMAND_TIMEOUT_SECONDS" \
        xdotool getwindowname "$window_id" >/dev/null || fail browser-exit
}

wait_for_title() {
    local expected="$1"

    while true; do
        require_single_browser
        title="$(
            timeout "$COMMAND_TIMEOUT_SECONDS" \
                xdotool getwindowname "$window_id"
        )" || fail window-title
        ((${#title} <= 2048)) || fail window-title-too-long
        [[ "$title" != *$'\n'* ]] || fail invalid-window-title
        if [[ "$title" == *"$expected"* ]]; then
            return
        fi
        check_deadline title-timeout
        sleep "$POLL_DELAY_SECONDS"
    done
}

activate_browser() {
    timeout "$COMMAND_TIMEOUT_SECONDS" \
        xdotool windowactivate --sync "$window_id" || fail window-activate
    timeout "$COMMAND_TIMEOUT_SECONDS" \
        xdotool windowfocus --sync "$window_id" || fail window-focus
}

type_address() {
    local address="$1"

    timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key ctrl+l || fail address-focus
    timeout "$COMMAND_TIMEOUT_SECONDS" \
        xdotool type --delay 0 -- "$address" || fail address-type
    timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key Return || fail address-submit
}

require_single_browser
activate_browser
type_address "$QUALITY_URL"
wait_for_title "Asterinas Browser Quality"
sleep "$SETTLE_DELAY_SECONDS"
emit "DEBIAN_BROWSER_M8_FIXTURE text=cjk-latin image=png form=query"

timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key End || fail scroll-end
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key Home || fail scroll-home
emit "DEBIAN_BROWSER_M8_SCROLL direction=end-home"

# NetSurf's Tab order includes browser chrome. Use coordinates relative to the
# client area, which is the coordinate space selected by xdotool --window.
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool mousemove --window "$window_id" 100 135 click 1 || fail form-focus
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool type --delay 0 -- asterinas || fail form-type
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool mousemove --window "$window_id" 230 135 click 1 || fail form-submit
wait_for_title "asterinas - Asterinas Browser Quality"
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool mousemove --window "$window_id" 90 284 click 1 || fail second-click
wait_for_title "Second - Asterinas Browser Quality"
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key alt+Left || fail navigation-back
wait_for_title "asterinas - Asterinas Browser Quality"
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key alt+Right || fail navigation-forward
wait_for_title "Second - Asterinas Browser Quality"
emit "DEBIAN_BROWSER_M8_NAVIGATION second=loaded back=loaded forward=loaded"

type_address "$DOWNLOAD_URL"
download_prompt=""
while [[ -z "$download_prompt" ]]; do
    active_window="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" xdotool getactivewindow
    )" || fail download-dialog
    [[ "$active_window" =~ ^[1-9][0-9]*$ ]] || fail download-dialog
    if [[ "$active_window" != "$window_id" ]]; then
        download_prompt="$active_window"
        break
    fi
    check_deadline download-dialog
    sleep "$POLL_DELAY_SECONDS"
done
download_prompt_process="$(
    timeout "$COMMAND_TIMEOUT_SECONDS" \
        xprop -id "$download_prompt" _NET_WM_PID
)" || fail download-dialog
[[ "$download_prompt_process" =~ ^_NET_WM_PID\(CARDINAL\)[[:space:]]*=[[:space:]]*([1-9][0-9]*)$ ]] ||
    fail download-dialog
[[ "${BASH_REMATCH[1]}" == "$process_id" ]] || fail download-dialog
# NetSurf first presents its own three-button "Download file?" window. Select
# the rightmost "Save as" action before waiting for GTK's file chooser.
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool mousemove --window "$download_prompt" 390 95 click 1 ||
    fail download-dialog
save_windows=()
while true; do
    save_output=""
    if save_output="$(
        timeout "$COMMAND_TIMEOUT_SECONDS" \
            # GTK themes/locales may append punctuation or a format hint to
            # the chooser title. Restrict by the stable prefix, then verify
            # the window's PID below before interacting with it.
            xdotool search --onlyvisible --name '^Save file as'
    )"; then
        mapfile -t save_windows <<<"$save_output"
        if ((${#save_windows[@]} > 1)); then
            fail download-dialog
        fi
        if ((${#save_windows[@]} == 1)) &&
            [[ "${save_windows[0]}" =~ ^[1-9][0-9]*$ ]]; then
            break
        fi
    fi
    check_deadline download-dialog
    sleep "$POLL_DELAY_SECONDS"
done
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool windowactivate --sync "${save_windows[0]}" || fail download-dialog
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key ctrl+a || fail download-path-focus
timeout "$COMMAND_TIMEOUT_SECONDS" \
    xdotool type --delay 0 -- "$DOWNLOAD" || fail download-path-type
download_owned=1
timeout "$COMMAND_TIMEOUT_SECONDS" xdotool key Return || fail download-accept
download_last_size=""
download_stable_since="$SECONDS"
while true; do
    if [[ ! -e "$DOWNLOAD" ]]; then
        check_deadline download-timeout
        sleep "$POLL_DELAY_SECONDS"
        continue
    fi
    [[ -f "$DOWNLOAD" && ! -L "$DOWNLOAD" ]] || fail unsafe-download
    download_size="$(stat -c %s -- "$DOWNLOAD")" || fail download-stat
    if [[ "$download_size" == "$DOWNLOAD_SIZE" ]]; then
        # Wait one stable poll before hashing so a producer that has just
        # extended the file cannot be observed between its final write and
        # close.
        if [[ "$download_size" == "$download_last_size" ]]; then
            download_sha256="$(sha256sum -- "$DOWNLOAD")" || fail download-hash
            download_sha256="${download_sha256%% *}"
            [[ "$download_sha256" == "$DOWNLOAD_SHA256" ]] || fail download-mismatch
            break
        fi
    elif [[ "$download_size" -gt "$DOWNLOAD_SIZE" ]]; then
        fail download-mismatch
    fi
    if [[ "$download_size" == "$download_last_size" ]]; then
        ((SECONDS - download_stable_since >= 3)) && fail download-mismatch
    else
        download_last_size="$download_size"
        download_stable_since="$SECONDS"
    fi
    check_deadline download-timeout
    sleep "$POLL_DELAY_SECONDS"
done
emit "DEBIAN_BROWSER_M8_DOWNLOAD bytes=$DOWNLOAD_SIZE sha256=$DOWNLOAD_SHA256"

check_deadline soak-timeout
remaining_seconds=$((DEADLINE - SECONDS))
timeout "$remaining_seconds" sleep "$SOAK_SECONDS" || fail soak-timeout
# NetSurf opens a separate Downloads manager after a successful save. Keep
# validating the original page window without rejecting that legitimate
# auxiliary window as an ambiguous browser process.
require_tracked_browser
emit "DEBIAN_BROWSER_M8_SOAK seconds=120 process=alive"

capture_owned=1
if ! timeout "$COMMAND_TIMEOUT_SECONDS" xwd -display :0 -root -silent |
    timeout "$COMMAND_TIMEOUT_SECONDS" gzip -n >"$CAPTURE"; then
    fail xwd-failure
fi
[[ -f "$CAPTURE" && ! -L "$CAPTURE" ]] || fail unsafe-capture
capture_size="$(stat -c %s -- "$CAPTURE")" || fail capture-stat
[[ "$capture_size" =~ ^[1-9][0-9]*$ ]] || fail capture-size
((capture_size <= CAPTURE_LIMIT)) || fail capture-size
capture_sha256="$(sha256sum -- "$CAPTURE")" || fail capture-hash
capture_sha256="${capture_sha256%% *}"
[[ "$capture_sha256" =~ ^[0-9a-f]{64}$ ]] || fail capture-hash
timeout "$COMMAND_TIMEOUT_SECONDS" curl \
    --fail \
    --silent \
    --show-error \
    --max-time 30 \
    -H "Content-Type: application/x-xwd+gzip" \
    --data-binary "@$CAPTURE" \
    "$CAPTURE_URL" || fail upload-rejected
emit "DEBIAN_BROWSER_M8_CAPTURE bytes=$capture_size sha256=$capture_sha256"
emit "DEBIAN_BROWSER_M8_READY quality=lightweight"
