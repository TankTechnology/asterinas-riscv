#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

shopt -s nullglob
input_devices=()
readonly XKB_CACHE_DIR="/var/lib/xkb"

if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
    # Keep diagnostics off the synchronous console path.  On Asterinas the
    # console driver can block a service while servicing terminal queries;
    # device setup itself is independent of that observation.
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-start' \
        >>/run/browser-web-device-stage.log
    # Xorg asks xkbcomp to atomically replace server-0.xkm in this directory.
    # The Asterinas ext2 path on Megrez cannot yet complete that replacement,
    # which makes Xorg abort before the virtual keyboard is activated.  The
    # compiled keymap is an ephemeral cache, so keep it off the persistent
    # root filesystem for the online browser session.  Xorg runs as the
    # desktop user, so the cache needs /tmp-style sticky shared-write access.
    xkb_cache_created=0
    if ! /usr/bin/mountpoint -q "$XKB_CACHE_DIR"; then
        if ! /usr/bin/mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs \
            "$XKB_CACHE_DIR"; then
            printf '%s\n' \
                'BROWSER_WEB_DESKTOP_STAGE=device-access-failed reason=xkb-cache-mount' \
                >>/run/browser-web-device-stage.log
            printf '%s\n' \
                'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: cannot mount XKB cache tmpfs' \
            >&2
            exit 1
        fi
        xkb_cache_created=1
    fi
    if ! xkb_mount=$(/usr/bin/awk -v path="$XKB_CACHE_DIR" \
        '$2 == path { print $3, $4; matches++ } END { if (matches != 1) exit 1 }' \
        /proc/self/mounts); then
        xkb_mount=""
    fi
    read -r xkb_fstype xkb_options <<<"$xkb_mount"
    if [[ "$xkb_fstype" != tmpfs ]] ||
        [[ ",$xkb_options," != *",rw,"* ]] ||
        [[ ",$xkb_options," != *",nosuid,"* ]] ||
        [[ ",$xkb_options," != *",nodev,"* ]]; then
        printf '%s\n' \
            'BROWSER_WEB_DESKTOP_STAGE=device-access-failed reason=xkb-cache-contract' \
            >>/run/browser-web-device-stage.log
        printf '%s\n' \
            'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: invalid XKB cache mount' \
            >&2
        printf 'xkb-cache fstype=%s options=%s\n' \
            "${xkb_fstype:-missing}" "${xkb_options:-missing}" >&2
        exit 1
    fi
    if ((xkb_cache_created)) &&
        { ! /usr/bin/chown root:root "$XKB_CACHE_DIR" ||
            ! /usr/bin/chmod 01777 "$XKB_CACHE_DIR"; }; then
        printf '%s\n' \
            'BROWSER_WEB_DESKTOP_STAGE=device-access-failed reason=xkb-cache-permissions' \
            >>/run/browser-web-device-stage.log
        printf '%s\n' \
            'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: cannot restrict XKB cache' \
            >&2
        exit 1
    fi
    if ! xkb_owner_mode=$(/usr/bin/stat -c "%u %g %a" "$XKB_CACHE_DIR"); then
        xkb_owner_mode="unknown"
    fi
    if [[ "$xkb_owner_mode" != "0 0 1777" ]]; then
        printf '%s\n' \
            'BROWSER_WEB_DESKTOP_STAGE=device-access-failed reason=xkb-cache-contract' \
            >>/run/browser-web-device-stage.log
        printf '%s\n' \
            'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: invalid XKB cache ownership or mode' \
            >&2
        printf 'xkb-cache owner-mode=%s\n' "$xkb_owner_mode" >&2
        exit 1
    fi
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=xkb-cache-ready' \
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
