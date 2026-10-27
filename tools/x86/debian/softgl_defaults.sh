#!/bin/bash
# SPDX-License-Identifier: MPL-2.0
#
# Make the software-GL defaults part of a Debian image instead of something
# every client has to export.
#
# Why /etc/environment and not /etc/X11/Xsession.d: this image has no display
# manager and Xorg is started directly, so nothing sources Xsession.d. PAM
# (pam_env) reads /etc/environment for every login session, including the
# serial getty session the daily checklist runs in. GTK's GL probe otherwise
# selects Mesa's virtio_gpu driver and fails before the window is realized
# (see docs/porting/2026-10-27-gtk-window-map.md).
#
# XDG_RUNTIME_DIR lives in tmpfs and is created by /etc/tmpfiles.d at boot,
# because pam_systemd only provides it for systemd-logind sessions.
#
# Usage: tools/x86/debian/softgl_defaults.sh <image.ext2>
set -eu

IMAGE=${1:?usage: softgl_defaults.sh <image.ext2>}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

debugfs -R "cat /etc/environment" "$IMAGE" 2>/dev/null >"$WORK/environment" || true
cat >>"$WORK/environment" <<'ENV'
# Software rendering defaults for this image: Mesa cannot drive virtio-gpu on
# the Asterinas DRM stack yet, so GTK's GL probe fails and no window appears.
LIBGL_ALWAYS_SOFTWARE=1
GALLIUM_DRIVER=softpipe
GDK_GL=disable
GDK_BACKEND=x11
NO_AT_BRIDGE=1
XDG_RUNTIME_DIR=/tmp/xdg
ENV

cat >"$WORK/asterinas-softgl.conf" <<'CONF'
d /tmp/xdg 0700 root root -
CONF

# login(1)/su(1) already stack pam_env and pick /etc/environment up, but
# sudo's PAM configuration does not, and the daily checklist runs under sudo.
# Adding the same optional session rule gives sudo sessions the same defaults.
debugfs -R "cat /etc/pam.d/sudo" "$IMAGE" 2>/dev/null >"$WORK/pam-sudo"
if ! grep -q 'pam_env.so' "$WORK/pam-sudo"; then
    printf 'session optional pam_env.so readenv=1\n' >>"$WORK/pam-sudo"
fi

# Belt and braces for login shells as well.
cat >"$WORK/90-asterinas-softgl.sh" <<'PROFILE'
# Software rendering defaults; see docs/porting/2026-10-27-gtk-window-map.md.
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=softpipe
export GDK_GL=disable
export GDK_BACKEND=x11
export NO_AT_BRIDGE=1
[ -n "${XDG_RUNTIME_DIR:-}" ] || { [ -d /tmp/xdg ] && export XDG_RUNTIME_DIR=/tmp/xdg; }
PROFILE

# The console getty here starts an interactive, non-login shell, so
# /etc/profile.d is not read for the session a user types in; bash.bashrc is.
debugfs -R "cat /etc/bash.bashrc" "$IMAGE" 2>/dev/null >"$WORK/bash.bashrc"
if ! grep -q 'asterinas-softgl' "$WORK/bash.bashrc"; then
    cat >>"$WORK/bash.bashrc" <<'BASHRC'

# Software rendering defaults (Asterinas image); see
# docs/porting/2026-10-27-gtk-window-map.md.
export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe GDK_GL=disable GDK_BACKEND=x11 NO_AT_BRIDGE=1
[ -n "${XDG_RUNTIME_DIR:-}" ] || { [ -d /tmp/xdg ] && export XDG_RUNTIME_DIR=/tmp/xdg; }
BASHRC
fi

{
    printf 'rm /etc/environment\n'
    printf 'write %s /etc/environment\n' "$WORK/environment"
    printf 'rm /etc/tmpfiles.d/asterinas-softgl.conf\n'
    printf 'write %s /etc/tmpfiles.d/asterinas-softgl.conf\n' "$WORK/asterinas-softgl.conf"
    printf 'rm /etc/pam.d/sudo\n'
    printf 'write %s /etc/pam.d/sudo\n' "$WORK/pam-sudo"
    printf 'sif /etc/pam.d/sudo mode 0100644\n'
    printf 'rm /etc/profile.d/90-asterinas-softgl.sh\n'
    printf 'write %s /etc/profile.d/90-asterinas-softgl.sh\n' "$WORK/90-asterinas-softgl.sh"
    printf 'sif /etc/profile.d/90-asterinas-softgl.sh mode 0100644\n'
    printf 'rm /etc/bash.bashrc\n'
    printf 'write %s /etc/bash.bashrc\n' "$WORK/bash.bashrc"
    printf 'sif /etc/bash.bashrc mode 0100644\n'
} | debugfs -w -f - "$IMAGE" >"$WORK/apply.log" 2>&1

if grep -q 'Ext2 file already exists' "$WORK/apply.log"; then
    echo "softgl_defaults: debugfs refused to replace a file" >&2
    exit 1
fi
e2fsck -fn "$IMAGE" >/dev/null 2>&1 || {
    echo "softgl_defaults: image is not clean after the update" >&2
    exit 1
}
echo "softgl_defaults: applied to $IMAGE"
