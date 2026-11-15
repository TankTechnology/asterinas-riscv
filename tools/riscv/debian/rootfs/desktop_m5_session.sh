#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

export DISPLAY=:0
export HOME=/home/asterinas
export XAUTHORITY="$HOME/.Xauthority"
SESSION_LOG="$HOME/desktop-m5-session.log"
# A root filesystem that filled up can leave this path with an unreadable
# directory entry ("Stale file handle"). Every later redirect fails, and under
# `set -e` that aborts the session before the window manager starts -- the
# desktop then never becomes ready and the boot watchdog reboots the board.
# Probe the path once and fall back to a fresh name instead of dying.
if ! : >>"$SESSION_LOG" 2>/dev/null; then
    SESSION_LOG="$HOME/desktop-m5-session-${EPOCHSECONDS:-0}.log"
fi
readonly SESSION_LOG
readonly DISPLAY_PROVIDER="${ASTERINAS_DISPLAY_PROVIDER:-fbdev}"

guest_monotonic_ns() {
    local raw="${EPOCHREALTIME-}"
    if [[ "$raw" =~ ^[0-9]+\.[0-9]{6}$ ]]; then
        raw="${raw/./}"
        printf '%s000' "$raw"
    else
        printf '%s000000000' "${EPOCHSECONDS:-0}"
    fi
}

emit_stage() {
    local stage="$1" pid="${2:-0}" detail="${3:-}" now line
    now="$(guest_monotonic_ns)"
    line="BROWSER_WEB_DESKTOP_STAGE=$stage guest_monotonic_ns=$now pid=$pid"
    [[ -z "$detail" ]] || line="$line $detail"
    printf '%s\n' "$line" >&2
    printf '%s\n' "$line" >>/dev/console 2>/dev/null || true
}

if provider_config_directory="$(/usr/lib/asterinas/desktop-display-provider)"; then
    readonly provider_config_directory
else
    readonly provider_status=$?
    emit_stage display-provider-failed 0 \
        "provider=$DISPLAY_PROVIDER status=$provider_status"
    exit "$provider_status"
fi
emit_stage display-provider-ready 0 "provider=$DISPLAY_PROVIDER"

if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
    # The online browser image does not need xinit's nested session, xterm, or
    # matchbox helper. Start Xorg directly and keep one lightweight WM so
    # Firefox gets a stable root window without the extra process chain.
    # Keep the marker on stderr as well as the console: systemd captures the
    # service stderr in the journal, while some Asterinas console devices do
    # not support direct writes from an unprivileged session process.
    emit_stage xorg-start
    # Mirror the online provider's Xorg logfile into the service stream.  Xorg
    # expects a regular logfile (using /dev/stderr makes it exit immediately
    # on this guest), while a failed gate may not persist the writable root.
    # The bounded tail keeps the decisive fbdev/VT error observable in the
    # journal+console stream without changing the Xorg invocation semantics.
    #
    # The log lives in $HOME, but a damaged inode (EIO/ESTALE) or a read-only
    # root must not take the whole session down: fall back to the volatile
    # /tmp path and say so on the service stream.
    xorg_log="$HOME/Xorg.0.log"
    if ! : >"$xorg_log" 2>/dev/null; then
        /usr/bin/rm -f -- "$xorg_log" 2>/dev/null || true
        if ! : >"$xorg_log" 2>/dev/null; then
            xorg_log="${TMPDIR:-/tmp}/asterinas-desktop-m5-Xorg.log"
            : >"$xorg_log"
            emit_stage xorg-log-fallback 0 "path=$xorg_log"
        fi
    fi
    # A failed Xorg can leave its Unix socket behind.  Never advertise that
    # stale endpoint as a live display to Firefox.
    /usr/bin/rm -f -- /tmp/.X11-unix/X0
    /usr/bin/tail -n 0 -f "$xorg_log" >&2 &
    readonly xorg_log_tailer_pid=$!
    /usr/bin/Xorg :0 -noreset -nolisten tcp -ac -novtswitch -keeptty -extension GLX \
        -configdir "$provider_config_directory" \
        -logfile "$xorg_log" vt1 &
    readonly xorg_pid=$!
    for _ in {1..120}; do
        if [[ -S /tmp/.X11-unix/X0 ]] &&
            /usr/bin/timeout 5 /usr/bin/xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
            break
        fi
        if ! kill -0 "$xorg_pid" 2>/dev/null; then
            kill "$xorg_log_tailer_pid" 2>/dev/null || true
            emit_stage xorg-failed "$xorg_pid" reason=process-exited
            exit 1
        fi
        /usr/bin/sleep 1
    done
    if [[ ! -S /tmp/.X11-unix/X0 ]]; then
        kill "$xorg_log_tailer_pid" 2>/dev/null || true
        emit_stage xorg-failed "$xorg_pid" reason=socket-timeout
        exit 1
    fi
    emit_stage x-socket-ready "$xorg_pid"
    # Chinese input: the GTK clients below reach fcitx5 through their immodule
    # over the session bus, so the bus address and the client defaults have to
    # be in place before the first client starts.  A session bus normally
    # already exists for this boot; create one when it does not, otherwise
    # fcitx5 exits with "Failed to create dbus connection" and the clients
    # silently fall back to plain keyboard input.
    export GTK_IM_MODULE=fcitx QT_IM_MODULE=fcitx XMODIFIERS=@im=fcitx
    if [[ -d /run ]]; then
        /usr/bin/install -d -o asterinas -g asterinas -m 0700 \
            /run/user/1000 2>/dev/null || true
        export XDG_RUNTIME_DIR=/run/user/1000
    fi
    if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
        # The session bus must be owned by the user that will talk to it: a
        # daemon started by root accepts the connection but fcitx5 then fails
        # to request `org.fcitx.Fcitx5` ("Unable to request dbus name"), and
        # every client silently falls back to plain keyboard input.  Start it
        # as asterinas and re-use it when a previous session already did.
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus"
        if ! /usr/bin/dbus-send --bus="$DBUS_SESSION_BUS_ADDRESS" --print-reply \
            --dest=org.freedesktop.DBus / org.freedesktop.DBus.Peer.Ping \
            >/dev/null 2>&1; then
            /usr/bin/rm -f /run/user/1000/bus 2>/dev/null || true
            /usr/sbin/runuser --user asterinas -- \
                /usr/bin/dbus-daemon --session --fork --nopidfile \
                --address="$DBUS_SESSION_BUS_ADDRESS" >/dev/null 2>&1 || true
        fi
        export DBUS_SESSION_BUS_ADDRESS
    fi
    if [[ -x /usr/bin/fcitx5 ]]; then
        /usr/sbin/runuser --user asterinas --preserve-environment -- \
            /usr/bin/fcitx5 -d --replace --disable xim >>"$SESSION_LOG" 2>&1 || true
        /usr/bin/sleep 1
    fi
    # Xorg owns the VT as the privileged display provider; keep the window
    # manager and desktop shell unprivileged so they cannot grant Firefox extra
    # capabilities through the session process.
    /usr/sbin/runuser --user asterinas --preserve-environment -- \
        /usr/bin/openbox --sm-disable >>"$SESSION_LOG" 2>&1 &
    emit_stage window-manager-start "$!"
    /usr/sbin/runuser --user asterinas --preserve-environment -- \
        /usr/bin/pcmanfm --desktop --profile Asterinas >>"$SESSION_LOG" 2>&1 &
    emit_stage desktop-start "$!"
    /usr/sbin/runuser --user asterinas --preserve-environment -- \
        /usr/bin/lxpanel --profile Asterinas >>"$SESSION_LOG" 2>&1 &
    emit_stage panel-start "$!"
    wait "$xorg_pid"
    xorg_status=$?
    kill "$xorg_log_tailer_pid" 2>/dev/null || true
    wait "$xorg_log_tailer_pid" 2>/dev/null || true
    exit "$xorg_status"
fi

if [[ "${1-}" == --xsession ]]; then
    /usr/bin/matchbox-window-manager -use_titlebar yes &
    readonly window_manager_pid=$!
    /usr/bin/sleep 1
    /usr/bin/xterm -geometry 100x30+48+72 -title "Asterinas Browser M5" &
    wait "$window_manager_pid"
    exit $?
fi

exec /usr/bin/xinit "$0" --xsession -- \
    /usr/bin/Xorg :0 -noreset -nolisten tcp -extension GLX \
    -configdir "$provider_config_directory" \
    -logfile "$HOME/Xorg.0.log" vt1 \
    >>"$SESSION_LOG" 2>&1
