#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

shopt -s nullglob
input_devices=()

if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
    # Keep diagnostics off the synchronous console path.  On Asterinas the
    # console driver can block a service while servicing terminal queries;
    # device setup itself is independent of that observation.
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-start' \
        >>/run/browser-web-device-stage.log
fi
# Asterinas creates the framebuffer after systemd has begun activating the
# graphical target.  The display is the hard prerequisite for Xorg; evdev is
# optional for Marionette-driven browser gates and must not keep the display
# server in an unbounded restart loop when the kernel exposes no input nodes.
readonly device_deadline=$((SECONDS + 120))
while [[ ! -c /dev/fb0 ]]; do
    if ((SECONDS >= device_deadline)); then
        if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
            printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-failed reason=fb0-timeout' \
                >>/run/browser-web-device-stage.log
        fi
        printf '%s\n' 'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: /dev/fb0 did not appear' >&2
        exit 1
    fi
    /usr/bin/sleep 1
done
input_devices=(/dev/input/event*)

if ! chown asterinas:video /dev/fb0 || ! chmod 0660 /dev/fb0; then
    if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
        printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-failed reason=fb0-permissions' \
            >>/run/browser-web-device-stage.log
    fi
    printf '%s\n' 'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: cannot configure /dev/fb0' >&2
    exit 1
fi
if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=fb0-ready' >>/run/browser-web-device-stage.log
fi
if ((${#input_devices[@]} > 0)); then
    if ! chown asterinas:input "${input_devices[@]}" || ! chmod 0660 "${input_devices[@]}"; then
        if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
            printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-failed reason=input-permissions' \
                >>/run/browser-web-device-stage.log
        fi
        printf '%s\n' 'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: cannot configure input devices' >&2
        exit 1
    fi
elif [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=input-devices-absent' \
        >>/run/browser-web-device-stage.log
fi
if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-done' \
        >>/run/browser-web-device-stage.log
fi
