#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

shopt -s nullglob
input_devices=()
readonly INPUT_DIRECTORY="${ASTERINAS_DESKTOP_INPUT_DIRECTORY:-/dev/input}"
readonly STABLE_INPUT_DIRECTORY="${ASTERINAS_DESKTOP_STABLE_INPUT_DIRECTORY:-/run/asterinas-input}"
readonly XKB_CACHE_DIR="/var/lib/xkb"
readonly DISPLAY_DEVICE=/dev/fb0

# The desktop and browser services both run this helper during graphical boot.
# Serialize the tmpfs mount and input-link replacement so one service cannot
# remove a link between the other's unlink and create operations. The lock
# lives in volatile /run and is released automatically when this process exits.
exec 9>/run/asterinas-desktop-device-access.lock
if ! /usr/bin/flock -w 125 9; then
    printf '%s\n' 'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: device setup lock timed out' >&2
    exit 1
fi

# Keep diagnostics off the synchronous console path. On Asterinas the console
# driver can block a service while servicing terminal queries; device setup
# itself is independent of that observation.
printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-start' \
    >>/run/browser-web-device-stage.log
# Xorg asks xkbcomp to atomically replace server-0.xkm in this directory. The
# Asterinas ext2 path cannot yet complete that replacement, which makes Xorg
# abort before the virtual keyboard is activated. The compiled keymap is an
# ephemeral cache, so keep it off the persistent root filesystem. Xorg runs as
# the desktop user, so the cache needs sticky shared-write access.
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

# Asterinas creates the framebuffer and evdev nodes after systemd has begun
# activating the graphical target. Xorg must not start before both devices
# exist because AutoAddDevices is disabled.
readonly device_deadline=$((SECONDS + 120))
while [[ ! -c "$DISPLAY_DEVICE" ]]; do
    if ((SECONDS >= device_deadline)); then
        printf '%s\n' "BROWSER_WEB_DESKTOP_STAGE=device-access-failed reason=${DISPLAY_DEVICE##*/}-timeout" \
            >>/run/browser-web-device-stage.log
        printf '%s\n' "ASTERINAS_DESKTOP_DEVICE_ACCESS failed: $DISPLAY_DEVICE did not appear" >&2
        exit 1
    fi
    /usr/bin/sleep 1
done
keyboard_node=""
pointer_node=""
/usr/bin/install -d -m 0755 -- "$STABLE_INPUT_DIRECTORY"
# The desktop and browser services can reach this point concurrently if their
# flock calls do not serialize. Build links in a private directory and atomically
# replace each public name, so neither service observes the other's remove/create
# window or fails because the other service already created a link.
/usr/bin/rm -f -- "$STABLE_INPUT_DIRECTORY/keyboard" "$STABLE_INPUT_DIRECTORY/pointer"
# The QEMU profile deliberately freezes the two virtio input nodes in
# event0/event1 order. Avoid evdev identity ioctls here: older Asterinas
# kernels can block while copying the variable-length name and phys strings,
# even though the character nodes are usable by Xorg.
keyboard_node="$INPUT_DIRECTORY/event0"
pointer_node="$INPUT_DIRECTORY/event1"
[[ -c "$keyboard_node" && -c "$pointer_node" ]] || {
    printf '%s\n' \
        'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: ordered input devices did not appear' \
        >&2
    exit 1
}
input_devices=("$INPUT_DIRECTORY"/event*)
link_directory=$(/usr/bin/mktemp -d "$STABLE_INPUT_DIRECTORY/.device-links.XXXXXX")
trap '/usr/bin/rm -rf -- "$link_directory"' EXIT
/usr/bin/ln -s -- "$keyboard_node" "$link_directory/keyboard"
/usr/bin/ln -s -- "$pointer_node" "$link_directory/pointer"
/usr/bin/mv -Tf -- "$link_directory/keyboard" "$STABLE_INPUT_DIRECTORY/keyboard"
/usr/bin/mv -Tf -- "$link_directory/pointer" "$STABLE_INPUT_DIRECTORY/pointer"
/usr/bin/rmdir -- "$link_directory"
trap - EXIT

display_nodes=(/dev/fb0)
if ! chown asterinas:video "${display_nodes[@]}" ||
    ! chmod 0660 "${display_nodes[@]}"; then
    printf '%s\n' "BROWSER_WEB_DESKTOP_STAGE=device-access-failed reason=${DISPLAY_DEVICE##*/}-permissions" \
        >>/run/browser-web-device-stage.log
    printf '%s\n' "ASTERINAS_DESKTOP_DEVICE_ACCESS failed: cannot configure $DISPLAY_DEVICE" >&2
    exit 1
fi
printf '%s\n' "BROWSER_WEB_DESKTOP_STAGE=${DISPLAY_DEVICE##*/}-ready" \
    >>/run/browser-web-device-stage.log
if ((${#input_devices[@]} > 0)); then
    if ! chown asterinas:input "${input_devices[@]}" || ! chmod 0660 "${input_devices[@]}"; then
        printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-failed reason=input-permissions' \
            >>/run/browser-web-device-stage.log
        printf '%s\n' 'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: cannot configure input devices' >&2
        exit 1
    fi
else
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=input-devices-absent' \
        >>/run/browser-web-device-stage.log
fi
printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-done' \
    >>/run/browser-web-device-stage.log
