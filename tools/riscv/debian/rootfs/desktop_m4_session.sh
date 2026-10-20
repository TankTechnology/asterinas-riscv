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
readonly DISPLAY_REUSE="${ASTERINAS_DISPLAY_REUSE:-1}"
if ! DISPLAY_PROVIDER="$(/usr/lib/asterinas/desktop-display-provider --name 2>/dev/null)"; then
    printf '%s\n' 'DEBIAN_DESKTOP_M4_FAIL reason=invalid-display-provider-config' >&2
    exit 65
fi
if ! provider_config_directory="$(/usr/lib/asterinas/desktop-display-provider 2>/dev/null)"; then
    printf '%s\n' \
        "DEBIAN_DESKTOP_M4_FAIL reason=invalid-display-provider-config provider=$DISPLAY_PROVIDER" >&2
    exit 65
fi
readonly PROVIDER_RUNTIME="${ASTERINAS_DISPLAY_PROVIDER_RUNTIME:-/run/asterinas/display-provider}"

record_provider() {
    local selected="$1" runtime_directory temporary
    [[ "$selected" == fbdev || "$selected" == drm ]] || return 64
    [[ "$PROVIDER_RUNTIME" == /* && "$PROVIDER_RUNTIME" != *$'\n'* ]] || return 64
    runtime_directory="${PROVIDER_RUNTIME%/*}"
    [[ -n "$runtime_directory" && "$runtime_directory" != "$PROVIDER_RUNTIME" ]] || return 64
    if ! /usr/bin/mkdir -p -- "$runtime_directory" 2>/dev/null; then
        return 1
    fi
    temporary="$(/usr/bin/mktemp "${PROVIDER_RUNTIME}.XXXXXX")" || return 1
    if ! {
        printf '%s\n' "$selected" >"$temporary"
        /usr/bin/chmod 0644 -- "$temporary"
        /usr/bin/mv -f -- "$temporary" "$PROVIDER_RUNTIME"
    }; then
        /usr/bin/rm -f -- "$temporary"
        return 1
    fi
}

provider_receipt() {
    local requested="$1" selected="$2" reason="$3"
    local line="ASTERINAS_DISPLAY_PROVIDER_RECEIPT requested=$requested selected=$selected reason=$reason bounded=1"
    printf '%s\n' "$line" >&2
    printf '%s\n' "$line" >>"$SESSION_LOG" 2>/dev/null || true
    printf '%s\n' "$line" >>/dev/console 2>/dev/null || true
}
INPUT_IDENTITY_MODE="${ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE:-strict}"
if [[ "$INPUT_IDENTITY_MODE" == strict ]] &&
    [[ -r /proc/cmdline ]] &&
    [[ " $(</proc/cmdline) " == *" systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered "* ]]; then
    INPUT_IDENTITY_MODE=ordered
fi
readonly INPUT_IDENTITY_MODE

# Ordered QEMU boots use a bounded phase trace to distinguish the service
# preflight from xinit/Xorg startup. Physical boots retain the existing quiet
# behavior.
phase() {
    [[ "$INPUT_IDENTITY_MODE" == ordered ]] || return 0
    # The ordered unit runs as the unprivileged desktop user.  Use the
    # service's journal+console stderr stream instead of opening /dev/console,
    # whose device path can block or reject the user and mask the real phase.
    printf '%s\n' "ASTERINAS_DESKTOP_M4_SESSION phase=$1" >&2 || true
}
phase start

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

[[ "$BROWSER_ENABLED" == 0 || "$BROWSER_ENABLED" == 1 ]] || {
    printf '%s\n' 'invalid ASTERINAS_DESKTOP_BROWSER_ENABLED' >&2
    exit 64
}
[[ "$INPUT_REQUIRED" == 0 || "$INPUT_REQUIRED" == 1 ]] || {
    printf '%s\n' 'invalid ASTERINAS_DESKTOP_INPUT_REQUIRED' >&2
    exit 64
}
[[ "$DISPLAY_REUSE" == 0 || "$DISPLAY_REUSE" == 1 ]] || {
    printf '%s\n' 'invalid ASTERINAS_DISPLAY_REUSE' >&2
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
            : >"$HOME/netsurf-m7.log"
            chmod 0600 "$HOME/netsurf-m7.log"
            /usr/bin/netsurf-gtk -v "${browser_arguments[@]}" "$browser_url" \
                >"$HOME/netsurf-m7.log" 2>&1 &
        else
            /usr/bin/netsurf-gtk "${browser_arguments[@]}" "$browser_url" &
        fi
    fi
    /usr/bin/sleep 1
    /usr/bin/xterm -geometry 100x30+48+72 -title "Asterinas Terminal" &
    wait "$window_manager_pid"
    exit $?
fi

{
    printf 'DESKTOP_M4_DEVICE identity='
    /usr/bin/id
    shopt -s nullglob
    readonly input_devices=(/dev/input/event*)
    if [[ "$DISPLAY_PROVIDER" == drm ]]; then
        /usr/bin/stat -c 'DESKTOP_M4_DEVICE path=%n uid=%u gid=%g mode=%a type=%F' \
            /dev/dri/card0 /dev/dri/renderD128 "${input_devices[@]}"
        printf '%s\n' 'DESKTOP_M4_DEVICE drm-open=deferred-to-xorg'
    else
        if [[ "$INPUT_IDENTITY_MODE" == ordered ]]; then
            # The Asterinas fb0 path can block even on metadata operations
            # while the display owner is being handed off.  Ordered boots let
            # Xorg perform the bounded provider open; stat(2) here would
            # recreate the r70 session boundary before phase=provider-fbdev.
            printf '%s\n' 'DESKTOP_M4_DEVICE framebuffer-stat=deferred'
            printf '%s\n' 'DESKTOP_M4_DEVICE framebuffer-open-rw=deferred'
        else
            /usr/bin/stat -c 'DESKTOP_M4_DEVICE path=%n uid=%u gid=%g mode=%a type=%F' \
                /dev/fb0 "${input_devices[@]}"
            if exec 9<>/dev/fb0; then
                printf '%s\n' 'DESKTOP_M4_DEVICE framebuffer-open-rw=ok'
                exec 9>&-
            else
                printf '%s\n' 'DESKTOP_M4_DEVICE framebuffer-open-rw=failed'
            fi
        fi
    fi
} >>"$SESSION_LOG" 2>&1

if ! record_provider "$DISPLAY_PROVIDER"; then
    # M4 may run as the unprivileged desktop user.  The selected provider is
    # already validated by the resolver; a runtime receipt is best-effort for
    # this profile and the child xsession inherits the selected value.
    printf '%s\n' \
        "DEBIAN_DESKTOP_M4_DISPLAY_PROVIDER_STATE unavailable provider=$DISPLAY_PROVIDER" \
        >&2
fi
phase "provider-$DISPLAY_PROVIDER"

provider_config_file() {
    local selected="$1"
    if [[ "$selected" == drm ]]; then
        printf '%s\n' "$provider_config_directory/20-asterinas.conf"
    elif [[ "$INPUT_REQUIRED" == 0 ]]; then
        printf '%s\n' "$provider_config_directory/20-asterinas-no-input.conf"
    elif [[ "$INPUT_IDENTITY_MODE" == ordered ]]; then
        printf '%s\n' "$provider_config_directory/20-asterinas-qemu.conf"
    else
        # Keep the physical M4 profile explicit as well.  The image builder
        # does not install the legacy /etc/X11/xorg.conf.d symlink for M4: Xorg's
        # automatic config-directory merge can otherwise reintroduce old
        # /run/asterinas-input paths after this session selected its config.
        printf '%s\n' "$provider_config_directory/20-asterinas.conf"
    fi
}

xorg_config_file="$(provider_config_file "$DISPLAY_PROVIDER")"
if [[ "$DISPLAY_PROVIDER" == drm ]]; then
    # Keep the DRM handoff diagnosable on the QEMU serial contract. The
    # modesetting server writes its authoritative diagnostics to the runtime
    # logfile; forwarding that file does not change the Xorg command line or
    # framebuffer ownership.
    /usr/bin/tail --retry -n 0 -f "$SESSION_LOG" >&2 &
    readonly session_log_tail_pid=$!
    /usr/bin/tail --retry -n 0 -f "$XORG_LOG" >&2 &
    readonly xorg_log_tail_pid=$!
    trap '/usr/bin/kill "$session_log_tail_pid" "$xorg_log_tail_pid" 2>/dev/null || true' EXIT
fi

drm_xorg_log_ready() {
    [[ "$DISPLAY_PROVIDER" == drm && -f "$XORG_LOG" ]] || return 1
    grep -Fqi 'glamor X acceleration enabled on virgl' "$XORG_LOG" || return 1
    grep -Fq 'Output Virtual-1 connected' "$XORG_LOG"
}

run_xinit() {
    local config_file="$1" selected="$2" xinit_pid status
    XORG_READY=0
    XORG_FAILURE_REASON=xorg-process-exited
    phase "xinit-before provider=$selected"
    /usr/bin/rm -f -- /tmp/.X11-unix/X0
    /usr/bin/xinit "$0" --xsession -- \
        /usr/bin/Xorg :0 -noreset -nolisten tcp \
        -extension MIT-SHM -config "$config_file" \
        -logfile "$XORG_LOG" -novtswitch \
        >>"$SESSION_LOG" 2>&1 &
    xinit_pid=$!
    phase "xinit-started pid=$xinit_pid"
    for _ in {1..120}; do
        XORG_READY_SOURCE=""
        if [[ -S /tmp/.X11-unix/X0 ]]; then
            if /usr/bin/timeout 5 /usr/bin/xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
                XORG_READY_SOURCE=xdpyinfo
            elif drm_xorg_log_ready; then
                XORG_READY_SOURCE=drm-log
            fi
        fi
        if [[ -n "$XORG_READY_SOURCE" ]]; then
            XORG_READY=1
            phase "xorg-ready source=$XORG_READY_SOURCE"
            printf '%s\n' \
                "DEBIAN_DESKTOP_M4_XORG_START provider=$selected status=ready source=${XORG_READY_SOURCE:-xdpyinfo} pid=$xinit_pid" \
                >>"$SESSION_LOG" 2>&1 || true
            break
        fi
        if ! kill -0 "$xinit_pid" 2>/dev/null; then
            wait "$xinit_pid" 2>/dev/null || true
            XORG_FAILURE_REASON=xorg-process-exited
            return 1
        fi
        /usr/bin/sleep 1
    done
    if ((XORG_READY == 0)); then
        phase "xorg-timeout reason=$XORG_FAILURE_REASON"
        XORG_FAILURE_REASON=xorg-start-timeout
        kill "$xinit_pid" 2>/dev/null || true
        wait "$xinit_pid" 2>/dev/null || true
        return 124
    fi
    if wait "$xinit_pid"; then
        status=0
    else
        status=$?
    fi
    return "$status"
}

reuse_existing_display() {
    local selected="$1"
    [[ "$DISPLAY_REUSE" == 1 ]] || return 1
    [[ -S /tmp/.X11-unix/X0 ]] || return 1
    /usr/bin/timeout 2 /usr/bin/xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 || return 1
    if [[ "$selected" == drm ]] && ! drm_xorg_log_ready; then
        return 1
    fi
    printf '%s\n' \
        "DEBIAN_DESKTOP_M4_XORG_REUSE provider=$selected display=$DISPLAY source=existing-server bounded=1" \
        >>"$SESSION_LOG" 2>&1 || true
    printf '%s\n' \
        "DEBIAN_DESKTOP_M4_XORG_REUSE provider=$selected display=$DISPLAY source=existing-server bounded=1" \
        >&2
    return 0
}

if [[ "$DISPLAY_PROVIDER" == drm ]]; then
    if reuse_existing_display "$DISPLAY_PROVIDER"; then
        exit 0
    fi
    xorg_status=0
    if run_xinit "$xorg_config_file" "$DISPLAY_PROVIDER"; then
        xorg_status=0
    else
        xorg_status=$?
    fi
    if ((xorg_status != 0)); then
        xorg_failure_reason="$XORG_FAILURE_REASON"
        if ((XORG_READY == 0)); then
            printf '%s\n' \
                "DEBIAN_DESKTOP_M4_XORG_FAIL provider=drm reason=$xorg_failure_reason" \
                >>"$SESSION_LOG" 2>&1 || true
            if fallback_config_directory="$(ASTERINAS_DISPLAY_PROVIDER=fbdev \
                /usr/lib/asterinas/desktop-display-provider 2>/dev/null)"; then
                DISPLAY_PROVIDER=fbdev
                export ASTERINAS_DISPLAY_PROVIDER="$DISPLAY_PROVIDER"
                provider_config_directory="$fallback_config_directory"
                record_provider "$DISPLAY_PROVIDER" ||
                    printf '%s\n' \
                        'DEBIAN_DESKTOP_M4_DISPLAY_PROVIDER_STATE unavailable provider=fbdev' \
                        >&2
                provider_receipt drm fbdev "$xorg_failure_reason"
                printf '%s\n' \
                    "DEBIAN_DESKTOP_M4_DISPLAY_PROVIDER_FALLBACK from=drm to=fbdev reason=$xorg_failure_reason fallback_attempted=1" \
                    >>"$SESSION_LOG" 2>&1 || true
                printf '%s\n' \
                    "DEBIAN_DESKTOP_M4_DISPLAY_PROVIDER_FALLBACK from=drm to=fbdev reason=$xorg_failure_reason fallback_attempted=1" \
                    >&2
                xorg_config_file="$(provider_config_file fbdev)"
                fallback_status=0
                if run_xinit "$xorg_config_file" "$DISPLAY_PROVIDER"; then
                    fallback_status=0
                else
                    fallback_status=$?
                fi
                if ((fallback_status != 0)); then
                    fallback_failure_reason="$XORG_FAILURE_REASON"
                    printf '%s\n' \
                        "DEBIAN_DESKTOP_M4_FAIL reason=display-provider-fallback from=drm to=fbdev detail=$fallback_failure_reason" \
                        >&2
                    exit "$fallback_status"
                fi
            else
                provider_receipt drm unavailable provider-config
                printf '%s\n' \
                    'DEBIAN_DESKTOP_M4_FAIL reason=display-provider-fallback-config' >&2
                exit 65
            fi
        else
            # A server that was ready and then exited is a real session
            # failure, not a provider-selection failure. Do not mask it as a
            # successful systemd service or start a second Xorg owner.
            exit "$xorg_status"
        fi
    fi
else
    if reuse_existing_display "$DISPLAY_PROVIDER"; then
        exit 0
    fi
    exec /usr/bin/xinit "$0" --xsession -- \
        /usr/bin/Xorg :0 -noreset -nolisten tcp \
        -extension MIT-SHM -config "$xorg_config_file" \
        -logfile "$XORG_LOG" -novtswitch \
        >>"$SESSION_LOG" 2>&1
fi
