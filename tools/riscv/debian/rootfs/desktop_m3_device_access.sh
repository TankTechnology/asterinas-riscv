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
# Asterinas creates the framebuffer and evdev nodes after systemd has begun
# activating the graphical target.  Marionette-driven browser gates do not
# need local input, but an interactive desktop must not start Xorg before both
# configured evdev nodes exist: AutoAddDevices is disabled in xorg.conf.
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
if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" != 1 ]]; then
    while [[ ! -c /dev/input/event0 || ! -c /dev/input/event1 ]]; do
        if ((SECONDS >= device_deadline)); then
            printf '%s\n' \
                'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: desktop input devices did not appear' \
                >&2
            exit 1
        fi
        /usr/bin/sleep 1
    done
fi
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
