#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -uo pipefail

readonly _asterinas_control=/run/systemd/system.control
readonly _asterinas_home=/run/asterinas-physical-home
readonly _asterinas_browser=asterinas-browser-web.service
readonly -a _asterinas_external_units=(
    asterinas-desktop-m5.service
    asterinas-browser-web.service
    asterinas-browser-web-evidence.service
    asterinas-desktop-m5-network.service
    serial-getty@ttyS0.service
    console-getty.service
)
_asterinas_external_status=0
_asterinas_failure_step=none

record_failure() {
    printf '__ASTERINAS_PHYSICAL_EXTERNAL_FAILURE__ status=%s step=%s\n' \
        "$1" "$2"
    if [[ "$_asterinas_external_status" == 0 ]]; then
        _asterinas_external_status="$1"
        _asterinas_failure_step="$2"
    fi
}

systemctl_bounded() {
    /usr/bin/timeout --kill-after=1s 5s /usr/bin/systemctl "$@"
}

/usr/bin/install -d -m 0755 "$_asterinas_control" || record_failure $? install-control
/usr/bin/install -d -m 0700 -o 1000 -g 1000 \
    "$_asterinas_home" \
    "$_asterinas_home/.mozilla" \
    "$_asterinas_home/.mozilla/asterinas-browser-web" \
    "$_asterinas_home/.cache" \
    "$_asterinas_home/.config" \
    "$_asterinas_home/Downloads" || record_failure $? install-home
/usr/bin/install -m 0600 -o 1000 -g 1000 /dev/null \
    "$_asterinas_home/browser-web-timeline.log" || record_failure $? install-timeline
/usr/bin/install -d -m 0755 \
    "$_asterinas_control/$_asterinas_browser.d" || \
    record_failure $? install-browser-drop-in
printf '%s\n' \
    '[Service]' \
    'Environment=HOME=/run/asterinas-physical-home' \
    'Environment=ASTERINAS_WEB_NETWORK_MODE=proxy' \
    'Environment=ASTERINAS_DESKTOP_PROXY_HOST=127.0.0.1' \
    'Environment=ASTERINAS_DESKTOP_PROXY_PORT=9' \
    'Environment=XDG_CACHE_HOME=/run/asterinas-physical-home/.cache' \
    >"$_asterinas_control/$_asterinas_browser.d/physical.conf" || \
    record_failure $? write-browser-drop-in

for _asterinas_external_unit in \
    asterinas-browser-web-evidence.service \
    asterinas-desktop-m5-network.service \
    serial-getty@ttyS0.service \
    console-getty.service; do
    /usr/bin/ln -sfn /dev/null \
        "$_asterinas_control/$_asterinas_external_unit" || \
        record_failure $? mask-unit
done
systemctl_bounded daemon-reload \
    >/dev/null 2>&1 || record_failure $? daemon-reload

_asterinas_stop_units=()
for _asterinas_external_unit in "${_asterinas_external_units[@]}"; do
    _asterinas_unit_state="$(
        systemctl_bounded is-active "$_asterinas_external_unit" 2>/dev/null || true
    )"
    case "$_asterinas_unit_state" in
        active | activating | deactivating | reloading)
            _asterinas_stop_units+=("$_asterinas_external_unit")
            ;;
        inactive | failed) ;;
        *) record_failure 125 inspect-unit-state ;;
    esac
done
if ((${#_asterinas_stop_units[@]} > 0)); then
    systemctl_bounded stop --no-block \
        "${_asterinas_stop_units[@]}" >/dev/null 2>&1 || true
fi
for _asterinas_external_unit in "${_asterinas_external_units[@]}"; do
    _asterinas_after_stop_state="$(
        systemctl_bounded is-active "$_asterinas_external_unit" 2>/dev/null || true
    )"
    case "$_asterinas_after_stop_state" in
        inactive | failed) ;;
        *) record_failure 126 verify-stopped-state ;;
    esac
done

/usr/bin/mountpoint -q /home/asterinas ||
    /usr/bin/mount --bind "$_asterinas_home" /home/asterinas || \
    record_failure $? bind-home
systemctl_bounded reset-failed \
    asterinas-desktop-m5.service \
    asterinas-browser-web-timeline-basic.service \
    asterinas-browser-web.service \
    asterinas-browser-web-evidence.service \
    asterinas-desktop-m5-network.service >/dev/null 2>&1 || true
systemctl_bounded start --no-block asterinas-desktop-m5.service \
    >/dev/null 2>&1 || record_failure $? start-desktop
systemctl_bounded start --no-block \
    asterinas-browser-web-timeline-basic.service \
    >/dev/null 2>&1 || record_failure $? start-timeline
/usr/bin/sleep 1

_asterinas_evidence_state="$(
    systemctl_bounded is-active \
        asterinas-browser-web-evidence.service 2>/dev/null || true
)"
_asterinas_evidence_pid="$(
    systemctl_bounded show --property MainPID --value \
        asterinas-browser-web-evidence.service 2>/dev/null || true
)"
_asterinas_network_state="$(
    systemctl_bounded is-active \
        asterinas-desktop-m5-network.service 2>/dev/null || true
)"
_asterinas_network_pid="$(
    systemctl_bounded show --property MainPID --value \
        asterinas-desktop-m5-network.service 2>/dev/null || true
)"
_asterinas_terminal_status=0
[[ "$_asterinas_evidence_state" == inactive ]] &&
    [[ "$_asterinas_evidence_pid" == 0 ]] &&
    [[ "$_asterinas_network_state" == inactive ]] &&
    [[ "$_asterinas_network_pid" == 0 ]] || _asterinas_terminal_status=124
case "$_asterinas_external_status" in
    0 | 124) ;;
    *) _asterinas_terminal_status="$_asterinas_external_status" ;;
esac
printf '__ASTERINAS_PHYSICAL_EXTERNAL__ status=%s setup_status=%s failure_step=%s evidence_state=%s evidence_pid=%s network_state=%s network_pid=%s\n' \
    "$_asterinas_terminal_status" \
    "$_asterinas_external_status" \
    "$_asterinas_failure_step" \
    "$_asterinas_evidence_state" \
    "$_asterinas_evidence_pid" \
    "$_asterinas_network_state" \
    "$_asterinas_network_pid"
