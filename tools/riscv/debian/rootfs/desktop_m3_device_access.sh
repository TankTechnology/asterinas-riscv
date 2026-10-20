#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

shopt -s nullglob
input_devices=()
readonly INPUT_DIRECTORY="${ASTERINAS_DESKTOP_M3_INPUT_DIRECTORY:-/dev/input}"
readonly STABLE_INPUT_DIRECTORY="${ASTERINAS_DESKTOP_M3_STABLE_INPUT_DIRECTORY:-/run/asterinas-input}"
readonly XKB_CACHE_DIR="/var/lib/xkb"
readonly OFFLINE_DESKTOP="${ASTERINAS_DESKTOP_OFFLINE:-0}"
if ! display_provider="$(/usr/lib/asterinas/desktop-display-provider --name)"; then
    printf '%s\n' 'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: invalid display provider config' >&2
    exit 1
fi
readonly display_provider
INPUT_IDENTITY_MODE="${ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE:-strict}"

# systemd.setenv is intentionally visible in the kernel command line but is
# not inherited by this service on the current Asterinas systemd handoff.
# QEMU's virtio devices have a stable registration order, so consume only the
# explicit QEMU token here; physical boots remain strict and identity-bound.
if [[ "$INPUT_IDENTITY_MODE" == strict ]] &&
    [[ -r /proc/cmdline ]] &&
    [[ " $(</proc/cmdline) " == *" systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered "* ]]; then
    INPUT_IDENTITY_MODE=ordered
fi
readonly INPUT_IDENTITY_MODE

# Ordered QEMU boots need a bounded service-boundary trace. Physical boots
# keep this diagnostic quiet; the setup logic itself is unchanged.
phase() {
    [[ "$INPUT_IDENTITY_MODE" == ordered ]] || return 0
    local marker="ASTERINAS_DESKTOP_DEVICE_ACCESS phase=$1"
    # The console device is diagnostic-only.  A blocked console write must
    # never hold ExecStartPre (and therefore PID 1) hostage.  Keep the marker
    # when the device is responsive, but bound the write and retain a local
    # volatile fallback for post-mortem inspection.
    if ! /usr/bin/timeout --signal=KILL 1s \
        /usr/bin/printf '%s\n' "$marker" >/dev/console 2>/dev/null; then
        /usr/bin/printf '%s\n' "$marker" \
            >>/run/asterinas-desktop-device-phase.log 2>/dev/null || true
    fi
}
phase start

case "$OFFLINE_DESKTOP" in
    0|1) ;;
    *)
        printf '%s\n' 'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: invalid offline desktop mode' >&2
        exit 1
        ;;
esac
case "$INPUT_IDENTITY_MODE" in
    strict|ordered) ;;
    *)
        printf '%s\n' 'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: invalid input identity mode' >&2
        exit 1
        ;;
esac
phase "provider-$display_provider"

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
# Asterinas creates the framebuffer, DRM and evdev nodes after systemd has begun
# activating the graphical target.  Xorg must not start before both identified
# devices exist: AutoAddDevices is disabled, and event numbers vary with the
# order in which the two physical xHCI controllers finish initialization.
readonly device_deadline=$((SECONDS + 120))
if [[ "$display_provider" == drm ]]; then
    while [[ ! -c /dev/dri/card0 || ! -c /dev/dri/renderD128 ]]; do
        if ((SECONDS >= device_deadline)); then
            printf '%s\n' 'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: DRM nodes did not appear' >&2
            exit 1
        fi
        /usr/bin/sleep 1
    done
    # DRM nodes are created root:root by the kernel.  Grant only the existing
    # render group access, and verify the result before Xorg/Firefox starts.
    /usr/bin/chown root:render /dev/dri/card0 /dev/dri/renderD128
    /usr/bin/chmod 0660 /dev/dri/card0 /dev/dri/renderD128
    [[ "$(/usr/bin/stat -c '%U %G %a' /dev/dri/card0)" == "root render 660" ]]
    [[ "$(/usr/bin/stat -c '%U %G %a' /dev/dri/renderD128)" == "root render 660" ]]
    # The DRM Xorg server still opens its controlling VT even with
    # -novtswitch.  Asterinas creates tty1 as a root-only node, so an
    # unprivileged desktop service otherwise reaches xf86OpenConsole and
    # fails before modesetting can probe card0.  Keep the grant narrow to the
    # tty group and bound both VFS operations; a stuck device must fail the
    # service rather than hold PID 1.
    if [[ -c /dev/tty1 ]]; then
        if ! /usr/bin/timeout --signal=KILL 5s \
            /usr/bin/chown root:tty /dev/tty1 ||
            ! /usr/bin/timeout --signal=KILL 5s \
            /usr/bin/chmod 0660 /dev/tty1 ||
            [[ "$(/usr/bin/stat -c '%U %G %a' /dev/tty1)" != "root tty 660" ]]; then
            printf '%s\n' \
                'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: cannot configure tty1 for DRM Xorg' \
                >&2
            exit 1
        fi
    fi
    # Keep the validated fbdev fallback usable if modesetting fails after the
    # DRM nodes have opened.  The fallback is bounded and one-shot in the
    # session, so preparing this existing node does not hand ownership to a
    # second Xorg server.
    if [[ -c /dev/fb0 ]]; then
        /usr/bin/chown 1000:44 /dev/fb0
        /usr/bin/chmod 0660 /dev/fb0
        [[ "$(/usr/bin/stat -c '%U %G %a' /dev/fb0)" == "asterinas video 660" ]]
    fi
fi
if [[ "$display_provider" != drm && "$INPUT_IDENTITY_MODE" != ordered ]]; then
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
fi
keyboard_node=""
pointer_node=""
if [[ "$OFFLINE_DESKTOP" == 1 ]]; then
    phase input-disabled
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=desktop-input-disabled' \
        >>/run/browser-web-device-stage.log 2>/dev/null || true
else
    if [[ "$INPUT_IDENTITY_MODE" == ordered ]]; then
        # QEMU's virtio keyboard is registered before its tablet. In this
        # explicit test-only mode, avoid the identity helper entirely: on this
        # kernel it can remain blocked in an ioctl and prevent ExecStartPre
        # from handing control to Xorg. Physical boots stay strict.
        input_candidates=("$INPUT_DIRECTORY"/event*)
        ((${#input_candidates[@]} == 2)) || {
            printf '%s\n' \
                'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: QEMU input devices are incomplete' \
                >&2
            exit 1
        }
        keyboard_node="${input_candidates[0]}"
        pointer_node="${input_candidates[1]}"
        input_devices=("${input_candidates[@]}")
        phase input-ordered
    else
      while true; do
        if input_nodes="$(
            /usr/bin/timeout 2 \
                env PYTHONPYCACHEPREFIX=/run/asterinas-python-cache \
                    /usr/bin/python3 /usr/lib/asterinas/desktop-input-identity
        )"; then
            read -r keyboard_node pointer_node <<<"$input_nodes"
            break
        else
            input_status=$?
        fi
        ((input_status != 2)) || exit 1
        if ((SECONDS >= device_deadline)); then
            printf '%s\n' \
                'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: desktop input devices did not appear' \
                >&2
            exit 1
        fi
        /usr/bin/sleep 1
      done
    fi
    [[ "$keyboard_node" != "$pointer_node" ]] || {
        printf '%s\n' 'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: ambiguous desktop input device identity' >&2
        exit 1
    }
    # Recreate the stable names on every boot, including the ordered QEMU
    # handoff.  /run is ephemeral, so skipping this step makes the first
    # desktop epoch work while the post-reboot epoch loses Xorg input nodes.
    phase input-stable-before
    /usr/bin/install -d -m 0755 -- "$STABLE_INPUT_DIRECTORY"
    /usr/bin/rm -f -- "$STABLE_INPUT_DIRECTORY/keyboard" "$STABLE_INPUT_DIRECTORY/pointer"
    /usr/bin/ln -s -- "$keyboard_node" "$STABLE_INPUT_DIRECTORY/keyboard"
    /usr/bin/ln -s -- "$pointer_node" "$STABLE_INPUT_DIRECTORY/pointer"
    phase input-stable-done
fi
phase done

if [[ "$display_provider" != drm ]]; then
    if [[ "$INPUT_IDENTITY_MODE" == ordered ]]; then
        # QEMU's synthetic device nodes are created as root:root 0600.  The
        # current Asterinas VFS has previously stopped while changing a device
        # inode's mode.  Keep this synthetic handoff bounded and reversible: a
        # timeout must fail the desktop unit and leave the debug console alive,
        # rather than strand PID 1 waiting for Xorg's prerequisite.
        # Physical boots stay on the strict owner/group path below.
        phase fbdev-permissions-before
        if ! /usr/bin/timeout --signal=KILL 5s chmod 0666 /dev/fb0; then
            if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
                printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-failed reason=fb0-permissions' \
                    >>/run/browser-web-device-stage.log
            fi
            printf '%s\n' 'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: cannot configure /dev/fb0' >&2
            exit 1
        fi
        phase fbdev-permissions-done
    else
        if ! chown 1000:44 /dev/fb0 || ! chmod 0660 /dev/fb0; then
            if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
                printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-failed reason=fb0-permissions' \
                    >>/run/browser-web-device-stage.log
            fi
            printf '%s\n' 'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: cannot configure /dev/fb0' >&2
            exit 1
        fi
    fi
    if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
        printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=fb0-ready' >>/run/browser-web-device-stage.log
    fi
fi
if ((${#input_devices[@]} > 0)); then
    if [[ "$INPUT_IDENTITY_MODE" == ordered ]]; then
        # Ordered QEMU boots use synthetic evdev nodes.  Xorg opens the
        # stable symlinks as the desktop user, so identity alone is not
        # sufficient: apply the same narrow input-group ownership as the
        # physical path, but bound each VFS operation so a stuck device
        # fails ExecStartPre instead of stranding PID 1.
        phase input-permissions-before
        if ! /usr/bin/timeout --signal=KILL 5s \
            /usr/bin/chown 1000:996 "${input_devices[@]}" ||
            ! /usr/bin/timeout --signal=KILL 5s \
            /usr/bin/chmod 0660 "${input_devices[@]}"; then
            printf '%s\n' \
                'ASTERINAS_DESKTOP_DEVICE_ACCESS failed: cannot configure input devices' \
                >&2
            exit 1
        fi
        phase input-permissions-done
    elif ! chown 1000:996 "${input_devices[@]}" || ! chmod 0660 "${input_devices[@]}"; then
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
