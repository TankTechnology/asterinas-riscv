#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

export DISPLAY=:0
export HOME=/home/asterinas
export XAUTHORITY="$HOME/.Xauthority"
readonly SESSION_LOG="$HOME/desktop-m5-session.log"
if ! DISPLAY_PROVIDER="$(/usr/lib/asterinas/desktop-display-provider --name 2>/dev/null)"; then
    printf '%s\n' \
        'BROWSER_WEB_DESKTOP_STAGE=display-provider-failed reason=invalid-config' >&2
    exit 65
fi
if ! provider_config_directory="$(/usr/lib/asterinas/desktop-display-provider 2>/dev/null)"; then
    printf '%s\n' \
        "BROWSER_WEB_DESKTOP_STAGE=display-provider-failed provider=$DISPLAY_PROVIDER reason=invalid-config" >&2
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

if ! record_provider "$DISPLAY_PROVIDER"; then
    if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
        emit_stage display-provider-failed 0 \
            "provider=$DISPLAY_PROVIDER reason=runtime-state"
        exit 65
    fi
    printf '%s\n' \
        "BROWSER_WEB_DESKTOP_STAGE=display-provider-state-unavailable provider=$DISPLAY_PROVIDER" \
        >&2
fi
emit_stage display-provider-ready 0 "provider=$DISPLAY_PROVIDER"

if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
    # The online browser image does not need xinit's nested session, xterm, or
    # matchbox helper. Start Xorg directly and keep one lightweight WM so
    # Firefox gets a stable root window without the extra process chain.
    # Keep the marker on stderr as well as the console: systemd captures the
    # service stderr in the journal, while some Asterinas console devices do
    # not support direct writes from an unprivileged session process.
    emit_stage xorg-start 0 "provider=$DISPLAY_PROVIDER"
    # Mirror the online provider's Xorg logfile into the service stream.  Xorg
    # expects a regular logfile (using /dev/stderr makes it exit immediately
    # on this guest), while a failed gate may not persist the writable root.
    # The bounded tail keeps the decisive fbdev/VT error observable in the
    # journal+console stream without changing the Xorg invocation semantics.
    : >"$HOME/Xorg.0.log"
    # A failed Xorg can leave its Unix socket behind.  Never advertise that
    # stale endpoint as a live display to Firefox.
    /usr/bin/rm -f -- /tmp/.X11-unix/X0
    XORG_READY=0
    XORG_FAILURE_REASON=xorg-process-exited
    xorg_log_tailer_pid=0
    xorg_pid=0

    start_xorg() {
        local config_directory="$1" selected="$2"
        XORG_READY=0
        XORG_FAILURE_REASON=xorg-process-exited
        /usr/bin/rm -f -- /tmp/.X11-unix/X0
        /usr/bin/tail -n 0 -f "$HOME/Xorg.0.log" >&2 &
        xorg_log_tailer_pid=$!
        /usr/bin/Xorg :0 -noreset -nolisten tcp -ac -novtswitch -keeptty -extension GLX \
            -configdir "$config_directory" \
            -logfile "$HOME/Xorg.0.log" vt1 &
        xorg_pid=$!
        for _ in {1..120}; do
            if [[ -S /tmp/.X11-unix/X0 ]] &&
                /usr/bin/timeout 5 /usr/bin/xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
                XORG_READY=1
                emit_stage x-socket-ready "$xorg_pid" "provider=$selected"
                return 0
            fi
            if ! kill -0 "$xorg_pid" 2>/dev/null; then
                wait "$xorg_pid" 2>/dev/null || true
                XORG_FAILURE_REASON=xorg-process-exited
                return 1
            fi
            /usr/bin/sleep 1
        done
        XORG_FAILURE_REASON=xorg-start-timeout
        return 124
    }

    stop_xorg() {
        if ((xorg_pid > 0)) && kill -0 "$xorg_pid" 2>/dev/null; then
            kill "$xorg_pid" 2>/dev/null || true
            wait "$xorg_pid" 2>/dev/null || true
        fi
        if ((xorg_log_tailer_pid > 0)); then
            kill "$xorg_log_tailer_pid" 2>/dev/null || true
            wait "$xorg_log_tailer_pid" 2>/dev/null || true
        fi
    }

    if ! start_xorg "$provider_config_directory" "$DISPLAY_PROVIDER"; then
        xorg_failure_reason="$XORG_FAILURE_REASON"
        stop_xorg
        emit_stage xorg-failed "$xorg_pid" \
            "provider=$DISPLAY_PROVIDER reason=$xorg_failure_reason"
        if [[ "$DISPLAY_PROVIDER" == drm ]]; then
            requested_provider=drm
            if fallback_config_directory="$(ASTERINAS_DISPLAY_PROVIDER=fbdev \
                /usr/lib/asterinas/desktop-display-provider 2>/dev/null)"; then
                DISPLAY_PROVIDER=fbdev
                provider_config_directory="$fallback_config_directory"
                if ! record_provider "$DISPLAY_PROVIDER"; then
                    provider_receipt "$requested_provider" fbdev runtime-state
                    emit_stage display-provider-fallback-failed 0 reason=runtime-state
                    exit 65
                fi
                provider_receipt "$requested_provider" fbdev "$xorg_failure_reason"
                emit_stage display-provider-fallback 0 \
                    "from=drm to=fbdev reason=$xorg_failure_reason fallback_attempted=1"
                if ! start_xorg "$provider_config_directory" "$DISPLAY_PROVIDER"; then
                    fallback_failure_reason="$XORG_FAILURE_REASON"
                    stop_xorg
                    emit_stage display-provider-fallback-failed "$xorg_pid" \
                        "from=drm to=fbdev reason=$fallback_failure_reason fallback_attempted=1"
                    exit 1
                fi
            else
                provider_receipt "$requested_provider" unavailable provider-config
                emit_stage display-provider-fallback-failed 0 \
                    'from=drm to=fbdev reason=provider-config fallback_attempted=1'
                exit 65
            fi
        else
            exit 1
        fi
    fi
    # Xorg owns the VT as the privileged display provider; keep the window
    # manager unprivileged so the desktop surface cannot grant Firefox extra
    # capabilities through the session process.
    /usr/sbin/runuser --user asterinas --preserve-environment -- \
        /usr/bin/openbox --sm-disable >>"$SESSION_LOG" 2>&1 &
    if wait "$xorg_pid"; then
        xorg_status=0
    else
        xorg_status=$?
    fi
    stop_xorg
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

run_offline_xinit() {
    local config_directory="$1" selected="$2" xinit_pid
    XORG_READY=0
    XORG_FAILURE_REASON=xorg-process-exited
    /usr/bin/rm -f -- /tmp/.X11-unix/X0
    /usr/bin/xinit "$0" --xsession -- \
        /usr/bin/Xorg :0 -noreset -nolisten tcp -extension GLX \
        -configdir "$config_directory" \
        -logfile "$HOME/Xorg.0.log" vt1 \
        >>"$SESSION_LOG" 2>&1 &
    xinit_pid=$!
    for _ in {1..120}; do
        if [[ -S /tmp/.X11-unix/X0 ]] &&
            /usr/bin/timeout 5 /usr/bin/xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
            XORG_READY=1
            printf '%s\n' \
                "BROWSER_WEB_DESKTOP_STAGE=x-socket-ready pid=$xinit_pid provider=$selected" \
                >&2
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
        XORG_FAILURE_REASON=xorg-start-timeout
        kill "$xinit_pid" 2>/dev/null || true
        wait "$xinit_pid" 2>/dev/null || true
        return 124
    fi
    wait "$xinit_pid"
}

if [[ "$DISPLAY_PROVIDER" == drm ]]; then
    xorg_status=0
    if run_offline_xinit "$provider_config_directory" "$DISPLAY_PROVIDER"; then
        xorg_status=0
    else
        xorg_status=$?
    fi
    if ((xorg_status != 0)); then
        xorg_failure_reason="$XORG_FAILURE_REASON"
        if ((XORG_READY == 0)); then
            if fallback_config_directory="$(ASTERINAS_DISPLAY_PROVIDER=fbdev \
                /usr/lib/asterinas/desktop-display-provider 2>/dev/null)"; then
                DISPLAY_PROVIDER=fbdev
                export ASTERINAS_DISPLAY_PROVIDER="$DISPLAY_PROVIDER"
                record_provider "$DISPLAY_PROVIDER" || true
                provider_receipt drm fbdev "$xorg_failure_reason"
                printf '%s\n' \
                    "BROWSER_WEB_DESKTOP_STAGE=display-provider-fallback pid=0 from=drm to=fbdev reason=$xorg_failure_reason fallback_attempted=1" \
                    >&2
                fallback_status=0
                if run_offline_xinit "$fallback_config_directory" "$DISPLAY_PROVIDER"; then
                    fallback_status=0
                else
                    fallback_status=$?
                fi
                if ((fallback_status != 0)); then
                    fallback_failure_reason="$XORG_FAILURE_REASON"
                    printf '%s\n' \
                        "BROWSER_WEB_DESKTOP_STAGE=display-provider-fallback-failed pid=0 from=drm to=fbdev reason=$fallback_failure_reason fallback_attempted=1" \
                        >&2
                    exit "$fallback_status"
                fi
            else
                provider_receipt drm unavailable provider-config
                printf '%s\n' \
                    'BROWSER_WEB_DESKTOP_STAGE=display-provider-fallback-failed pid=0 from=drm to=fbdev reason=provider-config fallback_attempted=1' \
                    >&2
                exit 65
            fi
        else
            exit "$xorg_status"
        fi
    fi
else
    exec /usr/bin/xinit "$0" --xsession -- \
        /usr/bin/Xorg :0 -noreset -nolisten tcp -extension GLX \
        -configdir "$provider_config_directory" \
        -logfile "$HOME/Xorg.0.log" vt1 \
        >>"$SESSION_LOG" 2>&1
fi
