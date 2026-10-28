#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

export DISPLAY=:0
export HOME=/home/asterinas
export XAUTHORITY="$HOME/.Xauthority"
SESSION_LOG="$HOME/desktop-m4-session.log"
XORG_LOG="$HOME/Xorg.0.log"
readonly URL_FILE="${ASTERINAS_DESKTOP_URL_FILE:-/run/asterinas-desktop-url}"
readonly BROWSER_ENABLED="${ASTERINAS_DESKTOP_BROWSER_ENABLED:-1}"
readonly INPUT_REQUIRED="${ASTERINAS_DESKTOP_INPUT_REQUIRED:-1}"
INPUT_IDENTITY_MODE="${ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE:-strict}"
if [[ "$INPUT_IDENTITY_MODE" == strict ]] &&
    [[ -r /proc/cmdline ]] &&
    [[ " $(</proc/cmdline) " == *" systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered "* ]]; then
    INPUT_IDENTITY_MODE=ordered
fi
readonly INPUT_IDENTITY_MODE

# The QEMU desktop profile uses the ext2 root image only as a read-mostly
# application image.  Keep transient X/session state on tmpfs until the
# filesystem writeback path is independently qualified; physical boots retain
# the normal per-user locations.
if [[ "$INPUT_IDENTITY_MODE" == ordered ]]; then
    export XAUTHORITY=/run/asterinas.Xauthority
    SESSION_LOG=/run/asterinas-desktop-m4-session.log
    XORG_LOG=/run/asterinas-desktop-m4-Xorg.0.log
fi
readonly SESSION_LOG XORG_LOG

map_client_windows() {
    local window_class window_id window_ids visible_ids pending
    local -a window_classes=(pcmanfm lxpanel netsurf-gtk xterm)

    # The Asterinas X11/fbdev path can leave a client in the server's
    # unmapped state after Openbox has accepted it.  Give each client a
    # bounded, authenticated MapWindow handoff; this is a condition-based
    # recovery step, not an unbounded sleep or a compositor substitute.
    for _ in {1..10}; do
        pending=0
        for window_class in "${window_classes[@]}"; do
            if [[ "$window_class" == netsurf-gtk && "$BROWSER_ENABLED" == 0 ]]; then
                continue
            fi
            window_ids="$(timeout --signal=KILL 3s \
                /usr/bin/xdotool search --class "$window_class" 2>/dev/null || true)"
            if [[ -z "$window_ids" ]]; then
                pending=1
                continue
            fi
            visible_ids="$(timeout --signal=KILL 3s \
                /usr/bin/xdotool search --onlyvisible --classname "^${window_class}$" \
                2>/dev/null || true)"
            while read -r window_id; do
                [[ "$window_id" =~ ^[1-9][0-9]*$ ]] || continue
                if ! grep -Fqx "$window_id" <<<"$visible_ids"; then
                    timeout --signal=KILL 3s \
                        /usr/bin/xdotool windowmap "$window_id" 2>/dev/null || true
                fi
            done <<<"$window_ids"
        done
        if ((pending == 0)); then
            printf '%s\n' 'DESKTOP_M4_WINDOW_HANDOFF status=complete' >>"$SESSION_LOG"
            return 0
        fi
        /usr/bin/sleep 1
    done
    printf '%s\n' 'DESKTOP_M4_WINDOW_HANDOFF status=partial' >>"$SESSION_LOG"
    return 1
}

[[ "$BROWSER_ENABLED" == 0 || "$BROWSER_ENABLED" == 1 ]] || {
    printf '%s\n' 'invalid ASTERINAS_DESKTOP_BROWSER_ENABLED' >&2
    exit 64
}
[[ "$INPUT_REQUIRED" == 0 || "$INPUT_REQUIRED" == 1 ]] || {
    printf '%s\n' 'invalid ASTERINAS_DESKTOP_INPUT_REQUIRED' >&2
    exit 64
}

if [[ "${1-}" == --xsession ]]; then
    if [[ "$BROWSER_ENABLED" == 1 ]]; then
        browser_url="file:///usr/share/asterinas/desktop-m4-welcome.html"
        browser_arguments=()
        proxy_host="${ASTERINAS_DESKTOP_PROXY_HOST:-}"
        proxy_port="${ASTERINAS_DESKTOP_PROXY_PORT:-}"
        if [[ -n "$proxy_host" || -n "$proxy_port" ]]; then
            [[ "$proxy_host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || exit 64
            [[ "$proxy_port" =~ ^[1-9][0-9]{0,4}$ ]] || exit 64
            ((proxy_port <= 65535)) || exit 64
            browser_arguments+=(
                --http_proxy=1
                --http_proxy_host="$proxy_host"
                --http_proxy_port="$proxy_port"
            )
        fi
        if [[ -f "$URL_FILE" && ! -L "$URL_FILE" ]]; then
            candidate="$(<"$URL_FILE")"
            if ((${#candidate} <= 2048)) && \
                [[ "$candidate" =~ ^https?://[^[:space:][:cntrl:]]+$ ]]; then
                browser_url="$candidate"
                browser_arguments+=(--enable_javascript=1)
            fi
        fi
    fi
    /usr/bin/openbox &
    readonly window_manager_pid=$!
    /usr/bin/sleep 1
    /usr/bin/pcmanfm --desktop --profile Asterinas &
    /usr/bin/lxpanel --profile Asterinas &
    if [[ "$BROWSER_ENABLED" == 1 ]]; then
        if [[ "${ASTERINAS_DESKTOP_BROWSER_VERBOSE:-0}" == 1 ]]; then
            : >"$HOME/netsurf.log"
            chmod 0600 "$HOME/netsurf.log"
            /usr/bin/netsurf-gtk -v "${browser_arguments[@]}" "$browser_url" \
                >"$HOME/netsurf.log" 2>&1 &
        else
            /usr/bin/netsurf-gtk "${browser_arguments[@]}" "$browser_url" &
        fi
    fi
    /usr/bin/sleep 1
    /usr/bin/xterm -geometry 100x30+48+72 -title "Asterinas Terminal" &
    map_client_windows || true
    wait "$window_manager_pid"
    exit $?
fi

{
    printf 'DESKTOP_M4_DEVICE identity='
    /usr/bin/id
    shopt -s nullglob
    readonly input_devices=(/dev/input/event*)
    /usr/bin/stat -c 'DESKTOP_M4_DEVICE path=%n uid=%u gid=%g mode=%a type=%F' \
        /dev/fb0 "${input_devices[@]}"
    if exec 9<>/dev/fb0; then
        printf '%s\n' 'DESKTOP_M4_DEVICE framebuffer-open-rw=ok'
        exec 9>&-
    else
        printf '%s\n' 'DESKTOP_M4_DEVICE framebuffer-open-rw=failed'
    fi
} >>"$SESSION_LOG" 2>&1

xorg_config_args=()
if [[ "$INPUT_REQUIRED" == 0 ]]; then
    xorg_config_args=(
        -config /etc/asterinas/display-providers/fbdev/xorg.conf.d/20-asterinas-no-input.conf
    )
elif [[ "$INPUT_IDENTITY_MODE" == ordered ]]; then
    xorg_config_args=(
        -config /etc/asterinas/display-providers/fbdev/xorg.conf.d/20-asterinas-qemu.conf
    )
else
    # Keep the physical M4 profile explicit as well.  The image builder does
    # not install the legacy /etc/X11/xorg.conf.d symlink for M4: Xorg's
    # automatic config-directory merge can otherwise reintroduce the old
    # /run/asterinas-input paths after this session selected its config.
    xorg_config_args=(
        -config /etc/asterinas/display-providers/fbdev/xorg.conf.d/20-asterinas.conf
    )
fi
exec /usr/bin/xinit "$0" --xsession -- \
    /usr/bin/Xorg :0 -noreset -nolisten tcp \
    -extension MIT-SHM "${xorg_config_args[@]}" \
    -logfile "$XORG_LOG" vt1 \
    >>"$SESSION_LOG" 2>&1
