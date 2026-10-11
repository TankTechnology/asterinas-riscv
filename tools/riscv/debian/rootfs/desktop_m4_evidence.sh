#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

CONSOLE="${ASTERINAS_DESKTOP_M4_CONSOLE:-/dev/console}"
readonly INPUT_DIRECTORY="${ASTERINAS_DESKTOP_M4_INPUT_DIRECTORY:-/dev/input}"
XORG_LOG="${ASTERINAS_DESKTOP_M4_XORG_LOG:-/home/asterinas/Xorg.0.log}"
SESSION_LOG="${ASTERINAS_DESKTOP_M4_SESSION_LOG:-/home/asterinas/desktop-m4-session.log}"
readonly NETSURF_LOG="${ASTERINAS_DESKTOP_M4_NETSURF_LOG:-/home/asterinas/netsurf-m7.log}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M4_TIMEOUT_SECONDS:-240}"
readonly PROBE_TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M4_PROBE_TIMEOUT_SECONDS:-30}"
readonly BROWSER_ENABLED="${ASTERINAS_DESKTOP_BROWSER_ENABLED:-1}"
readonly INPUT_REQUIRED="${ASTERINAS_DESKTOP_INPUT_REQUIRED:-1}"
readonly FRAMEBUFFER_PROBE="${ASTERINAS_DESKTOP_M4_FRAMEBUFFER_PROBE:-/usr/lib/asterinas/desktop-m4-framebuffer-probe}"
readonly FRAMEBUFFER_SCREENSHOT="${ASTERINAS_DESKTOP_M4_FRAMEBUFFER_SCREENSHOT:-/run/asterinas-desktop-m4-framebuffer.png}"
readonly SCANOUT_DIAGNOSTIC="${ASTERINAS_DESKTOP_M4_SCANOUT_DIAGNOSTIC:-0}"
readonly X11_PAINT_TEST="${ASTERINAS_DESKTOP_M4_X11_PAINT_TEST:-0}"
readonly USER_NAME="asterinas"
readonly USER_ID="1000"

if [[ -r /proc/cmdline ]] &&
    [[ " $(</proc/cmdline) " == *" systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered "* ]]; then
    CONSOLE=/dev/ttyS0
    XORG_LOG=/run/asterinas-desktop-m4-Xorg.0.log
    SESSION_LOG=/run/asterinas-desktop-m4-session.log
fi
XAUTHORITY_PATH=/home/asterinas/.Xauthority
if [[ "$CONSOLE" == /dev/ttyS0 ]]; then
    XAUTHORITY_PATH=/run/asterinas.Xauthority
fi
readonly CONSOLE XORG_LOG SESSION_LOG XAUTHORITY_PATH
not_ready_reason="not-evaluated"

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

bounded() {
    timeout --signal=KILL "${PROBE_TIMEOUT_SECONDS}s" "$@"
}

record_probe_failure() {
    local reason="$1"
    local status="$2"

    if ((status == 124 || status == 137)); then
        not_ready_reason="$reason-probe-timeout"
    else
        not_ready_reason="$reason"
    fi
}

probe() {
    local reason="$1"
    local status
    shift

    if bounded "$@"; then
        return 0
    else
        status=$?
    fi
    record_probe_failure "$reason" "$status"
    return 1
}

has_desktop_session() {
    local session_file
    local sessions
    local -a session_files=()

    # Asterinas' systemd-logind can create the session scope while its D-Bus
    # query path is still unavailable.  Prefer logind's durable runtime
    # records so a blocked loginctl call cannot hold the whole desktop gate.
    shopt -s nullglob
    session_files=(/run/systemd/sessions/*)
    if ((${#session_files[@]} > 0)); then
        for session_file in "${session_files[@]}"; do
            if grep -q '^USER=asterinas$' "$session_file" 2>/dev/null; then
                return 0
            fi
        done
        # A stale session record from an earlier boot must not hide the
        # bounded loginctl fallback.  This also keeps the handoff observable
        # when logind has created a record between the glob and the query.
    fi

    sessions="$(timeout --signal=KILL 3s loginctl list-sessions --no-legend 2>/dev/null)" || return 1
    [[ "$sessions" =~ (^|[[:space:]])$USER_NAME([[:space:]]|$) ]]
}

fail() {
    local reason="$1"
    bounded systemctl --no-pager --full status \
        dbus.service \
        systemd-logind.service \
        asterinas-desktop-m4.service \
        >>"$CONSOLE" 2>&1 || true
    if [[ -f "$SESSION_LOG" ]]; then
        printf '%s\n' '--- desktop session log ---' >>"$CONSOLE"
        cat -- "$SESSION_LOG" >>"$CONSOLE" 2>&1 || true
    fi
    if [[ "$BROWSER_ENABLED" == 1 && -f "$NETSURF_LOG" && ! -L "$NETSURF_LOG" ]]; then
        printf '%s\n' '--- NetSurf log tail ---' >>"$CONSOLE"
        tail -c 8192 -- "$NETSURF_LOG" >>"$CONSOLE" 2>&1 || true
    fi
    printf '%s\n' '--- X window tree tail ---' >>"$CONSOLE"
    bounded env DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority \
        xwininfo -root -tree 2>&1 | tail -c 16384 >>"$CONSOLE" || true
    printf '\n' >>"$CONSOLE"
    if [[ -f "$XORG_LOG" ]]; then
        printf '%s\n' '--- Xorg log ---' >>"$CONSOLE"
        cat -- "$XORG_LOG" >>"$CONSOLE" 2>&1 || true
    fi
    emit "DEBIAN_DESKTOP_M4_FAIL reason=$reason"
    exit 1
}

move_single_window_to_overview_workspace() {
    local instance_name="$1"
    local failure_prefix="$2"
    local window_output
    local -a windows=()

    if ! window_output="$(
        DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority \
            xdotool search --onlyvisible --classname "^${instance_name}$"
    )"; then
        fail "$failure_prefix-search"
    fi
    ((${#window_output} <= 128)) || fail "$failure_prefix-search-output-too-long"
    mapfile -t windows <<<"$window_output"
    ((${#windows[@]} == 1)) || fail "$failure_prefix-window-count"
    [[ "${windows[0]}" =~ ^[1-9][0-9]*$ ]] || \
        fail "$failure_prefix-window-id"
    DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority \
        xdotool set_desktop_for_window "${windows[0]}" 1 || \
        fail "$failure_prefix-workspace"
}

[[ "$PROBE_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-probe-timeout
[[ "$TIMEOUT_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-timeout
[[ "$BROWSER_ENABLED" == 0 || "$BROWSER_ENABLED" == 1 ]] || \
    fail invalid-browser-enabled
[[ "$INPUT_REQUIRED" == 0 || "$INPUT_REQUIRED" == 1 ]] || \
    fail invalid-input-required
[[ "$SCANOUT_DIAGNOSTIC" == 0 || "$SCANOUT_DIAGNOSTIC" == 1 ]] || \
    fail invalid-scanout-diagnostic
[[ "$X11_PAINT_TEST" == 0 || "$X11_PAINT_TEST" == 1 ]] || \
    fail invalid-x11-paint-test
deadline=$((SECONDS + TIMEOUT_SECONDS))

ready() {
    local status
    local window_tree
    local window_tree_lower

    not_ready_reason="not-evaluated"
    probe udev systemctl is-active --quiet systemd-udevd.service || return 1
    # `systemctl is-active` can wait indefinitely on Asterinas' incomplete
    # D-Bus query path even after systemd has started logind.  This service is
    # ordered After=systemd-logind.service, so a live logind process is the
    # bounded readiness fact we need here.
    probe logind pgrep -x systemd-logind >/dev/null || return 1
    if ! has_desktop_session; then
        not_ready_reason="asterinas-session"
        return 1
    fi
    if [[ "$INPUT_REQUIRED" == 1 ]]; then
        [[ -e "$INPUT_DIRECTORY/event0" ]] || {
            not_ready_reason="keyboard-device"
            return 1
        }
        [[ -e "$INPUT_DIRECTORY/event1" ]] || {
            not_ready_reason="pointer-device"
            return 1
        }
    fi
    [[ -f "$XORG_LOG" ]] || {
        not_ready_reason="xorg-log"
        return 1
    }
    probe framebuffer grep -q 'FBDEV(0)' "$XORG_LOG" || return 1
    if [[ "$INPUT_REQUIRED" == 1 ]]; then
        probe keyboard-xinput grep -q \
            'Adding extended input device.*Asterinas keyboard' "$XORG_LOG" || return 1
        probe pointer-xinput grep -q \
            'Adding extended input device.*Asterinas pointer' "$XORG_LOG" || return 1
    fi
    probe openbox pgrep -u "$USER_ID" -x openbox >/dev/null || return 1
    probe pcmanfm pgrep -u "$USER_ID" -f \
        '(^|/)pcmanfm([[:space:]].*)?--desktop([[:space:]]|$)' \
        >/dev/null || return 1
    probe lxpanel pgrep -u "$USER_ID" -f \
        '(^|/)lxpanel([[:space:]]|$)' >/dev/null || return 1
    probe xterm pgrep -u "$USER_ID" -x xterm >/dev/null || return 1
    if [[ "$BROWSER_ENABLED" == 1 ]]; then
        probe netsurf pgrep -u "$USER_ID" -x netsurf-gtk >/dev/null || return 1
        probe netsurf-window env DISPLAY=:0 \
            XAUTHORITY=/home/asterinas/.Xauthority \
            xdotool search --onlyvisible --classname '^netsurf-gtk$' \
            >/dev/null || return 1
    fi
    probe xterm-window env DISPLAY=:0 \
        XAUTHORITY=/home/asterinas/.Xauthority \
        xdotool search --onlyvisible --classname '^xterm$' \
        >/dev/null || return 1
    window_tree="$(
        bounded env DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority \
            xwininfo -root -tree 2>/dev/null
    )"
    status=$?
    if ((status != 0)); then
        record_probe_failure window-tree "$status"
        return 1
    fi
    window_tree_lower="${window_tree,,}"
    if [[ "$BROWSER_ENABLED" == 1 ]]; then
        [[ "$window_tree_lower" == *netsurf* ]] || {
            not_ready_reason="netsurf-tree"
            return 1
        }
    fi
    [[ "$window_tree_lower" == *"asterinas terminal"* ]] || {
        not_ready_reason="terminal-tree"
        return 1
    }
}

ready_ordered() {
    local window_tree

    visible_window() {
        timeout --signal=KILL 3s env DISPLAY=:0 XAUTHORITY="$XAUTHORITY_PATH" \
            xdotool search --onlyvisible --classname "$1" >/dev/null 2>&1
    }

    # QEMU's explicit handoff has already proved device ownership in the
    # ExecStartPre. Avoid systemctl/loginctl here: their tty and D-Bus paths
    # can remain blocked after Xorg is running. The runtime logs, process set,
    # bounded visible-window probes, and one bounded X window-tree query prove
    # that clients have actually connected and become visible before the
    # framebuffer sample is taken.
    [[ -f "$SESSION_LOG" ]] || {
        not_ready_reason="session-log"
        return 1
    }
    [[ -f "$XORG_LOG" ]] || {
        not_ready_reason="xorg-log"
        return 1
    }
    grep -q 'FBDEV(0)' "$XORG_LOG" || {
        not_ready_reason="framebuffer"
        return 1
    }
    pgrep -u "$USER_ID" -x openbox >/dev/null || {
        not_ready_reason="openbox"
        return 1
    }
    pgrep -u "$USER_ID" -f '(^|/)pcmanfm([[:space:]].*)?--desktop([[:space:]]|$)' >/dev/null || {
        not_ready_reason="pcmanfm"
        return 1
    }
    pgrep -u "$USER_ID" -f '(^|/)lxpanel([[:space:]]|$)' >/dev/null || {
        not_ready_reason="lxpanel"
        return 1
    }
    pgrep -u "$USER_ID" -x xterm >/dev/null || {
        not_ready_reason="xterm"
        return 1
    }
    visible_window '^pcmanfm$' || {
        not_ready_reason="pcmanfm-window"
        return 1
    }
    visible_window '^lxpanel$' || {
        not_ready_reason="lxpanel-window"
        return 1
    }
    visible_window '^xterm$' || {
        not_ready_reason="xterm-window"
        return 1
    }
    if [[ "$BROWSER_ENABLED" == 1 ]]; then
        pgrep -u "$USER_ID" -x netsurf-gtk >/dev/null || {
            not_ready_reason="netsurf"
            return 1
        }
        visible_window '^netsurf-gtk$' || {
            not_ready_reason="netsurf-window"
            return 1
        }
    fi
    if ! window_tree="$(
        timeout --signal=KILL 3s env DISPLAY=:0 XAUTHORITY="$XAUTHORITY_PATH" \
            xwininfo -root -tree 2>/dev/null
    )"; then
        not_ready_reason="window-tree"
        return 1
    fi
    [[ "$window_tree" == *"Asterinas Terminal"* ]] || {
        not_ready_reason="terminal-window"
        return 1
    }
    [[ "$window_tree" == *"panel"* && "$window_tree" == *"pcmanfm"* ]] || {
        not_ready_reason="desktop-windows"
        return 1
    }
    if [[ "$BROWSER_ENABLED" == 1 ]]; then
        [[ "$window_tree" == *"netsurf-gtk"* ]] || {
            not_ready_reason="netsurf-window"
            return 1
        }
    fi
}

if [[ "$CONSOLE" == /dev/ttyS0 ]]; then
    ready_function=ready_ordered
else
    ready_function=ready
fi
while ! "$ready_function"; do
    if ((SECONDS >= deadline)); then
        emit "DEBIAN_DESKTOP_M4_DIAGNOSTIC missing=$not_ready_reason"
        fail desktop-timeout
    fi
    sleep 1
done

if [[ "${ASTERINAS_DESKTOP_SHOW_OVERVIEW:-0}" == 1 ]]; then
    if [[ "$BROWSER_ENABLED" == 1 ]]; then
        move_single_window_to_overview_workspace netsurf-gtk overview-browser
    fi
    move_single_window_to_overview_workspace xterm overview-terminal
    sleep 1
fi

emit "DEBIAN_DESKTOP_M4_UDEV state=active"
emit "DEBIAN_DESKTOP_M4_LOGIND state=active"
emit "DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1"
if [[ "$INPUT_REQUIRED" == 1 ]]; then
    emit "DEBIAN_DESKTOP_M4_INPUT keyboard=evdev pointer=evdev"
fi
emit "DEBIAN_DESKTOP_M4_XORG framebuffer=fbdev display=:0"
emit "DEBIAN_DESKTOP_M4_SHELL wallpaper=asterinas desktop=pcmanfm panel=lxpanel launchers=3"
probe_framebuffer() {
    [[ -x "$FRAMEBUFFER_PROBE" ]] || fail framebuffer-probe-missing
    if [[ "$CONSOLE" == /dev/ttyS0 ]]; then
        if ! fb_probe_output="$(PYTHONOPTIMIZE=1 ASTERINAS_DESKTOP_M4_FRAMEBUFFER_SAMPLE_ONLY=1 \
            bounded "$FRAMEBUFFER_PROBE" --output "$FRAMEBUFFER_SCREENSHOT" 2>&1)"; then
            printf '%s\n' "$fb_probe_output" >>"$CONSOLE"
            fail framebuffer-probe
        fi
    else
        fb_probe_output="$(bounded "$FRAMEBUFFER_PROBE" --output "$FRAMEBUFFER_SCREENSHOT")" || \
            fail framebuffer-probe
    fi
    # desktop_m4_framebuffer_probe emits DEBIAN_DESKTOP_M4_FB_COLORS after
    # validating the sample; keep that marker part of the evidence contract.
    printf '%s\n' "$fb_probe_output" >>"$CONSOLE"
}
if [[ "$INPUT_REQUIRED" == 0 ]]; then
    emit "DEBIAN_DESKTOP_M4_DISPLAY_CLIENTS window-manager=openbox file-manager=pcmanfm panel=lxpanel terminal=xterm input=disabled"
elif [[ "$BROWSER_ENABLED" == 1 ]]; then
    emit "DEBIAN_DESKTOP_M4_CLIENTS window-manager=openbox file-manager=pcmanfm browser=netsurf terminal=xterm"
else
    emit "DEBIAN_DESKTOP_M4_CORE_CLIENTS window-manager=openbox file-manager=pcmanfm panel=lxpanel terminal=xterm"
fi

# A QEMU-only diagnostic can add a large, deterministic X client after the
# normal desktop clients are ready.  It separates client-to-fbdev submission
# from the host scanout check without changing the normal desktop profile.
scanout_diagnostic_pid=""
scanout_pattern_pid=""
if [[ "$SCANOUT_DIAGNOSTIC" == 1 ]]; then
    [[ -x /usr/bin/xmessage ]] || fail scanout-diagnostic-missing
    DISPLAY=:0 XAUTHORITY="$XAUTHORITY_PATH" \
        timeout --signal=KILL 20s /usr/bin/xmessage \
        -geometry 640x480+100+100 \
        -bg '#224466' -fg '#ffffff' \
        -title 'Asterinas scanout diagnostic' \
        -timeout 20 'Asterinas fbdev scanout diagnostic' \
        >>"$SESSION_LOG" 2>&1 &
    scanout_diagnostic_pid=$!
    sleep 2
    kill -0 "$scanout_diagnostic_pid" 2>/dev/null || fail scanout-diagnostic-exited
fi
if [[ "${ASTERINAS_DESKTOP_M4_SCANOUT_PATTERN:-0}" == 1 ]]; then
    [[ -x /usr/lib/asterinas/desktop-m4-framebuffer-rw ]] || fail scanout-pattern-missing
    /usr/lib/asterinas/desktop-m4-framebuffer-rw \
        --device /dev/fb0 --pattern --hold-seconds 15 >>"$CONSOLE" 2>&1 &
    scanout_pattern_pid=$!
fi
if [[ "$X11_PAINT_TEST" == 1 ]]; then
    [[ -x /usr/lib/asterinas/desktop-m4-x11-paint ]] || fail x11-paint-test-missing
    if ! bounded env DISPLAY=:0 XAUTHORITY="$XAUTHORITY_PATH" \
        /usr/lib/asterinas/desktop-m4-x11-paint >>"$CONSOLE" 2>&1; then
        fail x11-paint-test
    fi
fi
# Run the pixel check only after the client marker.  The process checks above
# prove that applications exist, but do not prove that their first paint has
# reached fbdev.  Keeping the probe after that marker preserves the ordered
# gate while allowing the clients a bounded first-paint window.  On QEMU the
# fbdev Xorg server may spend several seconds initializing its software GLX
# provider before GTK clients receive their first expose event; sampling
# immediately would turn a valid black initial scanout into a false failure.
# A zero-second test profile is used by the host contract tests and means
# "sample immediately".  Real QEMU/physical profiles retain the five-second
# first-paint settling window, but never let that window turn a test fixture
# into a probe-timeout failure.
if ((TIMEOUT_SECONDS > 0)); then
    bounded sleep 5
fi
if [[ "$CONSOLE" == /dev/ttyS0 || ("$INPUT_REQUIRED" == 0 && "$BROWSER_ENABLED" == 0) ]]; then
    probe_framebuffer
fi
if [[ -n "$scanout_diagnostic_pid" ]]; then
    kill "$scanout_diagnostic_pid" 2>/dev/null || true
    wait "$scanout_diagnostic_pid" 2>/dev/null || true
fi
if [[ -n "$scanout_pattern_pid" ]]; then
    wait "$scanout_pattern_pid" || fail scanout-pattern
fi
if [[ "$INPUT_REQUIRED" == 0 ]]; then
    emit "DEBIAN_DESKTOP_M4_DISPLAY_READY user=$USER_NAME display=:0"
elif [[ "$BROWSER_ENABLED" == 1 ]]; then
    emit "DEBIAN_DESKTOP_M4_READY user=$USER_NAME display=:0"
else
    emit "DEBIAN_DESKTOP_M4_CORE_READY user=$USER_NAME display=:0"
fi
