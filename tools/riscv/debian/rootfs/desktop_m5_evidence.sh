#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_M5_CONSOLE:-/dev/console}"
readonly INPUT_DIRECTORY="${ASTERINAS_DESKTOP_M5_INPUT_DIRECTORY:-/dev/input}"
readonly XORG_LOG="${ASTERINAS_DESKTOP_M5_XORG_LOG:-/home/asterinas/Xorg.0.log}"
# m5f19b reached a large Navigator around guest second 3,300. Reserve a
# separate 600-second formal gate inside a 4,500-second total deadline; the
# prior 30-second gate was shorter than one observed 44.9-second greeting.
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS:-4500}"
readonly FORMAL_GATE_TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M5_FORMAL_GATE_TIMEOUT_SECONDS:-600}"
readonly PROC_ROOT="${ASTERINAS_DESKTOP_M5_PROC_ROOT:-/proc}"
readonly PROFILE_DIRECTORY="${ASTERINAS_DESKTOP_M5_PROFILE_DIRECTORY:-/home/asterinas/.mozilla/asterinas-browser-m5}"
readonly NAVIGATOR_READY_FILE="${ASTERINAS_DESKTOP_M5_NAVIGATOR_READY_FILE:-$PROFILE_DIRECTORY/NavigatorWindowReady}"
readonly CONTENT_GATE="${ASTERINAS_DESKTOP_M5_CONTENT_GATE:-/usr/lib/asterinas/browser-m5-marionette-gate}"
readonly PROCESS_SAMPLE_LOG="${ASTERINAS_DESKTOP_M5_PROCESS_SAMPLE_LOG:-/home/asterinas/firefox-m5-process-samples.log}"
readonly KERNEL_SAMPLE_LOG="${ASTERINAS_DESKTOP_M5_KERNEL_SAMPLE_LOG:-/home/asterinas/firefox-m5-kernel-samples.log}"
readonly SECURITY_LOG="${ASTERINAS_DESKTOP_M5_SECURITY_LOG:-/home/asterinas/firefox-m5-security-evidence.log}"
readonly FIREFOX_STDERR="${ASTERINAS_DESKTOP_M5_FIREFOX_STDERR:-/home/asterinas/firefox-m5-plain-stderr.log}"
readonly USER_NAME=asterinas
readonly USER_ID=1000
browser_pid=""
marionette_ready=false
security_checked_pid=""

emit() { printf '%s\n' "$1" >>"$CONSOLE"; }
fail() { emit "DEBIAN_BROWSER_M5_FAIL reason=$1"; exit 1; }

[[ "$TIMEOUT_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-timeout
[[ "$FORMAL_GATE_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-formal-timeout
deadline=$((SECONDS + TIMEOUT_SECONDS))
ready() {
    local command_line sessions
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
    browser_pid="$(systemctl show --property MainPID --value asterinas-browser-m5.service 2>/dev/null)" || return 1
    [[ "$browser_pid" =~ ^[1-9][0-9]*$ ]] || return 1
    [[ -r "$PROC_ROOT/$browser_pid/cmdline" ]] || return 1
    [[ "$(cat "$PROC_ROOT/$browser_pid/comm" 2>/dev/null)" == firefox-esr ]] || return 1
    command_line="$(tr '\0' ' ' <"$PROC_ROOT/$browser_pid/cmdline")"
    [[ "$command_line" == *" --offline "* ]] || return 1
    [[ "$command_line" == *" --marionette "* ]] || return 1
    [[ "$command_line" == *" file:///usr/share/asterinas/browser-m5/index.html"* ]] || return 1
}

diagnostic_ready() {
    browser_pid="$(systemctl show --property MainPID --value asterinas-browser-m5.service 2>/dev/null)" || return 1
    [[ "$browser_pid" =~ ^[1-9][0-9]*$ ]] || return 1
    [[ "$(cat "$PROC_ROOT/$browser_pid/comm" 2>/dev/null)" == firefox-esr ]] || return 1
    [[ "$(cat "$PROFILE_DIRECTORY/MarionetteActivePort" 2>/dev/null)" == 2828 ]] || return 1
}

navigator_ready() {
    local evidence
    evidence="$(cat "$NAVIGATOR_READY_FILE" 2>/dev/null)" || return 1
    [[ "$evidence" == "browser_pid=$browser_pid "* ]]
}

validate_zero_capabilities() {
    local pid="$1" role="$2" field
    [[ -r "$PROC_ROOT/$pid/status" ]] || fail "security-status-$role"
    for field in CapInh CapPrm CapEff CapBnd CapAmb; do
        grep -Eq "^${field}:[[:space:]]+0+$" "$PROC_ROOT/$pid/status" ||
            fail "security-capability-$role-$field"
    done
    printf 'A_M5_SECURITY role=%s pid=%s capabilities=zero\n' \
        "$role" "$pid" >>"$SECURITY_LOG"
}

validate_parent_security() {
    local command_line environment nnp prefs
    validate_zero_capabilities "$browser_pid" parent
    command_line="$(tr '\0' ' ' <"$PROC_ROOT/$browser_pid/cmdline")"
    environment="$(tr '\0' '\n' <"$PROC_ROOT/$browser_pid/environ")"
    [[ "$command_line" != *--no-sandbox* ]] || fail security-cmdline-no-sandbox
    if grep -Eq '^(MOZ_DISABLE_(CONTENT|GMP|RDD|SOCKET)_SANDBOX|MOZ_FORCE_DISABLE_E10S)=([^0]|0*[1-9])' \
        <<<"$environment"; then
        fail security-environment-sandbox-disabled
    fi
    grep -qx 'MOZ_SANDBOX_LOGGING=1' <<<"$environment" ||
        fail security-sandbox-logging-missing
    prefs="$PROFILE_DIRECTORY/prefs.js"
    if [[ -f "$prefs" ]] && grep -Eq \
        'user_pref\("security\.sandbox\.[^"]+",[[:space:]]*0\)' "$prefs"; then
        fail security-pref-sandbox-disabled
    fi
    if grep -q '^NoNewPrivs:' "$PROC_ROOT/$browser_pid/status"; then
        grep -Eq '^NoNewPrivs:[[:space:]]+1$' "$PROC_ROOT/$browser_pid/status" ||
            fail security-parent-no-new-privileges
        nnp=enabled
    else
        nnp=kernel-status-unavailable
    fi
    printf 'A_M5_SECURITY role=parent pid=%s no_new_privs=%s sandbox_disable=absent sandbox_logging=enabled\n' \
        "$browser_pid" "$nnp" >>"$SECURITY_LOG"
    emit "DEBIAN_BROWSER_M5_SECURITY parent_caps=zero no_new_privs=$nnp sandbox_disable=absent sandbox_logging=enabled"
}

validate_child_security() {
    local process pid command_line role content_seen=false
    for process in "$PROC_ROOT"/[1-9]*; do
        [[ -d "$process" ]] || continue
        pid="${process##*/}"
        [[ "$pid" != "$browser_pid" ]] || continue
        command_line="$(tr '\0' ' ' <"$process/cmdline" 2>/dev/null || true)"
        [[ "$command_line" == *" -parentPid $browser_pid "* ]] || continue
        role=child
        [[ "$command_line" == *" socket "* ]] && role=socket
        [[ "$command_line" == *" rdd "* ]] && role=rdd
        if [[ "$command_line" == *" tab "* ]]; then
            role=content
            content_seen=true
        fi
        validate_zero_capabilities "$pid" "$role"
        if [[ "$role" == content ]]; then
            grep -q '^Seccomp:' "$process/status" ||
                fail security-content-seccomp-unavailable
            grep -Eq '^Seccomp:[[:space:]]+2$' "$process/status" ||
                fail security-content-seccomp
        fi
    done
    [[ "$content_seen" == true ]] || fail security-content-process-missing
    {
        printf 'A_M5_SECURITY sandbox_log_and_prefs\n'
        grep -Ei 'sandbox|seccomp' "$FIREFOX_STDERR" 2>/dev/null | tail -100 || true
        grep -Ei 'security\.sandbox' "$PROFILE_DIRECTORY/prefs.js" 2>/dev/null || true
    } >>"$SECURITY_LOG"
    emit "DEBIAN_BROWSER_M5_SECURITY child_caps=zero content=present seccomp=enabled"
}

sample_firefox_processes() {
    local stage="$1" process pid comm command_line
    {
        printf 'A_M5_PROC_SAMPLE stage=%s wall_ns=%s browser_pid=%s\n' \
            "$stage" "$(date +%s%N)" "$browser_pid"
        for process in "$PROC_ROOT"/[1-9]*; do
            [[ -d "$process" ]] || continue
            pid="${process##*/}"
            comm="$(cat "$process/comm" 2>/dev/null || true)"
            command_line="$(tr '\0' ' ' <"$process/cmdline" 2>/dev/null || true)"
            if [[ "$pid" != "$browser_pid" && "$comm" != *firefox* && "$command_line" != *firefox* ]]; then
                continue
            fi
            printf 'A_M5_PROC pid=%s comm=%q cmdline=%q\n' "$pid" "$comm" "$command_line"
            for name in status stat wchan; do
                printf -- '--- pid=%s file=%s ---\n' "$pid" "$name"
                cat "$process/$name" 2>&1 || true
            done
        done
    } >>"$PROCESS_SAMPLE_LOG" 2>&1 || true
    {
        printf 'A_M5_KERNEL_SAMPLE stage=%s wall_ns=%s\n' "$stage" "$(date +%s%N)"
        timeout 2 dmesg 2>&1 | grep -Ei \
            'oom|out of memory|killed process|sigkill|signal 9' | tail -100 || true
    } >>"$KERNEL_SAMPLE_LOG" 2>&1 || true
}

while ! ready || ! navigator_ready; do
    if ready && [[ "$security_checked_pid" != "$browser_pid" ]]; then
        validate_parent_security
        security_checked_pid="$browser_pid"
    fi
    if [[ "$marionette_ready" == false ]] && diagnostic_ready; then
        remaining=$((deadline - SECONDS))
        ((remaining > 0)) || fail browser-timeout
        diagnostic_timeout="$remaining"
        ((diagnostic_timeout <= 30)) || diagnostic_timeout=30
        if diagnostic_evidence="$("$CONTENT_GATE" \
            --firefox-pid "$browser_pid" --timeout "$diagnostic_timeout" \
            --diagnose-once 2>>"$CONSOLE")"; then
            emit "$diagnostic_evidence"
            if [[ "$diagnostic_evidence" == "DEBIAN_BROWSER_M5_DIAGNOSTIC ready=true status="* ]]; then
                marionette_ready=true
            fi
        else
            emit "DEBIAN_BROWSER_M5_DIAGNOSTIC status=unavailable"
        fi
    fi
    ((SECONDS < deadline)) || fail browser-timeout
    sleep 1
done

remaining=$((deadline - SECONDS))
((remaining > 0)) || fail browser-timeout
gate_timeout="$remaining"
((gate_timeout <= FORMAL_GATE_TIMEOUT_SECONDS)) || gate_timeout="$FORMAL_GATE_TIMEOUT_SECONDS"
emit "DEBIAN_BROWSER_M5_NAVIGATOR state=visible browser_pid=$browser_pid marionette_status_ready=$marionette_ready"
sample_firefox_processes formal-gate-start
if ! content_evidence="$("$CONTENT_GATE" \
    --firefox-pid "$browser_pid" --timeout "$gate_timeout" 2>>"$CONSOLE")"; then
    sample_firefox_processes formal-gate-failed
    fail browser-content
fi
sample_firefox_processes formal-gate-done
[[ "$content_evidence" == "DEBIAN_BROWSER_M5_CONTENT js=pass media=vp8-webm canplay=pass ended=pass network_mode=private-loopback source=file direct_nonloopback_ip=unavailable" ]] ||
    fail browser-content-output
validate_child_security

emit "DEBIAN_BROWSER_M5_UDEV state=active"
emit "DEBIAN_BROWSER_M5_LOGIND state=active"
emit "DEBIAN_BROWSER_M5_SESSION user=asterinas tty=tty1"
emit "DEBIAN_BROWSER_M5_INPUT keyboard=evdev pointer=evdev"
emit "DEBIAN_BROWSER_M5_XORG framebuffer=fbdev display=:0"
emit "DEBIAN_BROWSER_M5_CLIENTS window-manager=matchbox browser=firefox-esr terminal=xterm"
emit "DEBIAN_BROWSER_M5_WORKLOAD mode=offline scheme=file network=private-loopback"
emit "$content_evidence"
emit "DEBIAN_BROWSER_M5_READY user=asterinas display=:0"
