#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_M5_CONSOLE:-/dev/console}"
readonly INPUT_DIRECTORY="${ASTERINAS_DESKTOP_M5_INPUT_DIRECTORY:-/dev/input}"
readonly XORG_LOG="${ASTERINAS_DESKTOP_M5_XORG_LOG:-/home/asterinas/Xorg.0.log}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS:-300}"
readonly USER_NAME=asterinas
readonly USER_ID=1000

emit() { printf '%s\n' "$1" >>"$CONSOLE"; }
fail() { emit "DEBIAN_BROWSER_M5_FAIL reason=$1"; exit 1; }

[[ "$TIMEOUT_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-timeout
deadline=$((SECONDS + TIMEOUT_SECONDS))
ready() {
    local browser_pid command_line sessions window_tree
    systemctl is-active --quiet systemd-udevd.service || return 1
    systemctl is-active --quiet systemd-logind.service || return 1
    sessions="$(loginctl list-sessions --no-legend 2>/dev/null)" || return 1
    [[ "$sessions" =~ (^|[[:space:]])$USER_NAME([[:space:]]|$) ]] || return 1
    [[ -e "$INPUT_DIRECTORY/event0" && -e "$INPUT_DIRECTORY/event1" ]] || return 1
    [[ -f "$XORG_LOG" ]] || return 1
    grep -q 'FBDEV(0)' "$XORG_LOG" || return 1
    grep -q 'Adding extended input device.*Asterinas keyboard' "$XORG_LOG" || return 1
    grep -q 'Adding extended input device.*Asterinas pointer' "$XORG_LOG" || return 1
    pgrep -u "$USER_ID" -f '(^|/)matchbox-window-manager([[:space:]]|$)' >/dev/null || return 1
    pgrep -u "$USER_ID" -x xterm >/dev/null || return 1
    browser_pid="$(pgrep -u "$USER_ID" -o -x firefox-esr 2>/dev/null)" || return 1
    [[ -r "/proc/$browser_pid/cmdline" ]] || return 1
    command_line="$(tr '\0' ' ' <"/proc/$browser_pid/cmdline")"
    [[ "$command_line" == *" --offline "* ]] || return 1
    [[ "$command_line" == *" --marionette "* ]] || return 1
    [[ "$command_line" == *" file:///usr/share/asterinas/browser-m5/index.html"* ]] || return 1
    window_tree="$(DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority xwininfo -root -tree 2>/dev/null)" || return 1
    [[ "${window_tree,,}" == *"asterinas offline browser m5 probe"* ]] || return 1
}

while ! ready; do
    ((SECONDS < deadline)) || fail browser-timeout
    sleep 1
done

remaining=$((deadline - SECONDS))
((remaining > 0)) || fail browser-timeout
gate_timeout="$remaining"
((gate_timeout <= 30)) || gate_timeout=30
content_evidence="$(/usr/lib/asterinas/browser-m5-marionette-gate --timeout "$gate_timeout" 2>>"$CONSOLE")" ||
    fail browser-content
[[ "$content_evidence" == "DEBIAN_BROWSER_M5_CONTENT js=pass media=vp8-webm canplay=pass ended=pass network_mode=firefox-offline source=file" ]] ||
    fail browser-content-output

emit "DEBIAN_BROWSER_M5_UDEV state=active"
emit "DEBIAN_BROWSER_M5_LOGIND state=active"
emit "DEBIAN_BROWSER_M5_SESSION user=asterinas tty=tty1"
emit "DEBIAN_BROWSER_M5_INPUT keyboard=evdev pointer=evdev"
emit "DEBIAN_BROWSER_M5_XORG framebuffer=fbdev display=:0"
emit "DEBIAN_BROWSER_M5_CLIENTS window-manager=matchbox browser=firefox-esr terminal=xterm"
emit "DEBIAN_BROWSER_M5_WORKLOAD mode=offline scheme=file"
emit "$content_evidence"
emit "DEBIAN_BROWSER_M5_READY user=asterinas display=:0"
