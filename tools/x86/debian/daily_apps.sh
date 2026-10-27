#!/bin/bash
# SPDX-License-Identifier: MPL-2.0
#
# Apply the "daily minimal apps" part of the Debian image setup:
#
#   1. the input-method environment defaults, so a GTK application can type
#      Chinese without the user exporting anything (the same delivery paths as
#      tools/x86/debian/softgl_defaults.sh: /etc/environment for PAM sessions,
#      /etc/bash.bashrc for interactive shells, /etc/profile.d for login
#      shells);
#   2. removal of the dangling /etc/X11/xorg.conf.d/20-asterinas.conf fragment,
#      whose ServerLayout names /run/asterinas-input nodes and makes e2fsck
#      complain on an image where /etc/asterinas is absent;
#   3. a check that lxterminal and mousepad are installed. Installing packages
#      needs dpkg/apt inside the guest, which a host-side script cannot do:
#      the image build must run, inside the guest,
#
#          apt-get install -y --no-install-recommends lxterminal mousepad
#
#      (16 packages, lxterminal 0.4.1-1 + mousepad 0.6.3-1 on trixie). This
#      script exits non-zero and prints that command when they are missing.
#
# Usage: tools/x86/debian/daily_apps.sh <image.ext2>
set -eu

IMAGE=${1:?usage: daily_apps.sh <image.ext2>}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

debugfs -R "cat /etc/environment" "$IMAGE" 2>/dev/null >"$WORK/environment" || true
if ! grep -q '^GTK_IM_MODULE=' "$WORK/environment"; then
    cat >>"$WORK/environment" <<'ENV'
GTK_IM_MODULE=xim
XMODIFIERS=@im=fcitx
QT_IM_MODULE=fcitx
ENV
fi

cat >"$WORK/91-asterinas-im.sh" <<'PROFILE'
# Input method defaults; see docs/porting/2026-10-27-terminal-and-editor.md.
export GTK_IM_MODULE=xim
export XMODIFIERS=@im=fcitx
export QT_IM_MODULE=fcitx
PROFILE

debugfs -R "cat /etc/bash.bashrc" "$IMAGE" 2>/dev/null >"$WORK/bash.bashrc" || true
if ! grep -q 'GTK_IM_MODULE' "$WORK/bash.bashrc"; then
    cat >>"$WORK/bash.bashrc" <<'BASHRC'

# Input method defaults; see docs/porting/2026-10-27-terminal-and-editor.md.
export GTK_IM_MODULE=xim XMODIFIERS=@im=fcitx QT_IM_MODULE=fcitx
BASHRC
fi

{
    printf 'rm /etc/environment\n'
    printf 'write %s /etc/environment\n' "$WORK/environment"
    printf 'rm /etc/profile.d/91-asterinas-im.sh\n'
    printf 'write %s /etc/profile.d/91-asterinas-im.sh\n' "$WORK/91-asterinas-im.sh"
    printf 'sif /etc/profile.d/91-asterinas-im.sh mode 0100644\n'
    printf 'rm /etc/bash.bashrc\n'
    printf 'write %s /etc/bash.bashrc\n' "$WORK/bash.bashrc"
    printf 'sif /etc/bash.bashrc mode 0100644\n'
    printf 'rm /etc/X11/xorg.conf.d/20-asterinas.conf\n'
} | debugfs -w -f - "$IMAGE" >"$WORK/apply.log" 2>&1

if grep -q 'Ext2 file already exists' "$WORK/apply.log"; then
    echo "daily_apps: debugfs refused to replace a file" >&2
    exit 1
fi

missing=""
for app in lxterminal mousepad; do
    if ! debugfs -R "stat /usr/bin/$app" "$IMAGE" 2>/dev/null | grep -q 'Type: regular'; then
        missing="$missing $app"
    fi
done
if [ -n "$missing" ]; then
    cat >&2 <<EOF
daily_apps: missing$missing in $IMAGE.
Install them inside the guest during the image build:

    apt-get install -y --no-install-recommends lxterminal mousepad

then run this script again to apply the environment defaults.
EOF
    exit 1
fi

e2fsck -fn "$IMAGE" >/dev/null 2>&1 || {
    echo "daily_apps: image is not clean after the update" >&2
    exit 1
}
echo "daily_apps: applied to $IMAGE"
