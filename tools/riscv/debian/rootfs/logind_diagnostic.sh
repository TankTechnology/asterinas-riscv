#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -u

readonly CONSOLE="${ASTERINAS_LOGIND_DIAGNOSTIC_CONSOLE:-/dev/console}"
readonly LOG_FILE="${ASTERINAS_LOGIND_DIAGNOSTIC_LOG:-/var/log/asterinas-logind-diagnostic.log}"
readonly TIMEOUT_SECONDS="${ASTERINAS_LOGIND_DIAGNOSTIC_TIMEOUT_SECONDS:-300}"
readonly SAMPLE_INTERVAL_SECONDS="${ASTERINAS_LOGIND_DIAGNOSTIC_SAMPLE_INTERVAL_SECONDS:-5}"

emit() {
    (printf '%s\n' "$1" >>"$CONSOLE") 2>/dev/null || true
}

log() {
    printf '%s\n' "$1" >>"$LOG_FILE" 2>/dev/null || true
}

snapshot() {
    local elapsed="$1"
    local state
    local udev_state
    local dbus_state
    local logind_show
    local seats
    local input_nodes
    local vt_nodes

    state="$(systemctl show systemd-logind.service \
        -p ActiveState -p SubState -p Result -p MainPID \
        -p ExecMainStartTimestampMonotonic -p ActiveEnterTimestampMonotonic \
        2>/dev/null | tr '\n' ' ')"
    udev_state="$(systemctl is-active systemd-udevd.service 2>/dev/null || true)"
    dbus_state="$(systemctl is-active dbus.service 2>/dev/null || true)"
    logind_show="$(systemctl status --no-pager --full systemd-logind.service 2>&1 || true)"
    seats="$(loginctl list-seats --no-legend 2>&1 || true)"
    input_nodes="$(find /dev/input -maxdepth 1 -type c -printf '%f ' 2>/dev/null || true)"
    vt_nodes="$(find /dev -maxdepth 1 \( -name 'tty[0-9]*' -o -name 'vcs*' \) \
        -printf '%f ' 2>/dev/null || true)"

    log "sample elapsed=${elapsed}s logind=[$state] udev=${udev_state} dbus=${dbus_state} seats=[$seats] input=[$input_nodes] vt=[$vt_nodes]"
    log "--- systemctl status at elapsed=${elapsed}s ---"
    log "$logind_show"
}

fail() {
    local reason="$1"
    snapshot "$TIMEOUT_SECONDS"
    log "--- dbus/logind diagnostics ---"
    busctl status org.freedesktop.login1 >>"$LOG_FILE" 2>&1 || true
    loginctl seat-status >>"$LOG_FILE" 2>&1 || true
    journalctl --no-pager --full -u systemd-logind.service -u systemd-udevd.service \
        -u dbus.service -n 200 >>"$LOG_FILE" 2>&1 || true
    emit "DEBIAN_LOGIND_DIAGNOSTIC_FAIL reason=$reason"
    log "DEBIAN_LOGIND_DIAGNOSTIC_FAIL reason=$reason"
    exit 1
}

[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-timeout
[[ "$SAMPLE_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-interval

install -d -m 0755 "$(dirname -- "$LOG_FILE")" 2>/dev/null || true
: >"$LOG_FILE" 2>/dev/null || true
emit "DEBIAN_LOGIND_DIAGNOSTIC_BEGIN timeout=${TIMEOUT_SECONDS}s interval=${SAMPLE_INTERVAL_SECONDS}s"
log "DEBIAN_LOGIND_DIAGNOSTIC_BEGIN timeout=${TIMEOUT_SECONDS}s interval=${SAMPLE_INTERVAL_SECONDS}s"

deadline=$((SECONDS + TIMEOUT_SECONDS))
while ((SECONDS < deadline)); do
    elapsed=$((TIMEOUT_SECONDS - (deadline - SECONDS)))
    snapshot "$elapsed"
    if systemctl is-active --quiet systemd-logind.service 2>/dev/null; then
        seats="$(loginctl list-seats --no-legend 2>/dev/null || true)"
        emit "DEBIAN_LOGIND_DIAGNOSTIC_ACTIVE elapsed=${elapsed}s seats=${seats:-none}"
        log "DEBIAN_LOGIND_DIAGNOSTIC_ACTIVE elapsed=${elapsed}s seats=${seats:-none}"
        exit 0
    fi
    sleep "$SAMPLE_INTERVAL_SECONDS"
done

fail logind-timeout
