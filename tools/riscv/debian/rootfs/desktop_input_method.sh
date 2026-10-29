#!/bin/bash
# SPDX-License-Identifier: MPL-2.0
#
# Configure the Chinese input method inside the staged rootfs.
#
# This is the RISC-V port of the session that already works on the x86 image
# (tools/x86/debian/daily_session.sh). Four things there were not obvious and
# are reproduced here:
#
# * the table addon must be eager. While it is loaded on demand, fcitx parses
#   the profile before the engine exists, treats wbpy as unknown and rewrites
#   the group down to keyboard-us, so a new window never starts in Chinese.
# * XDG_RUNTIME_DIR has to be per user. A single value in /etc/environment
#   makes a dbus-daemon started as root refuse to run ("owned by uid 1000, not
#   our uid 0").
# * xterm's Xlib XIM swallows every keystroke once XMODIFIERS names fcitx, so
#   xterm is told not to use an input method at all. Chinese stays available in
#   the GTK clients, which go through fcitx's GTK frontend.
# * the session must not register XIM (fcitx5 --disable xim). Two XIM servers
#   make GTK clients started with GTK_IM_MODULE=xim hang before their first map.
#
# Run as root inside the staged root (the builder does this through its
# chroot).

set -euo pipefail

readonly USER_NAME=asterinas
readonly USER_ID=1000

install -d -o "$USER_ID" -g "$USER_ID" "/home/${USER_NAME}/.config/fcitx5"
cat >"/home/${USER_NAME}/.config/fcitx5/profile" <<'EOF'
[Groups/0]
Name=Default
Default Layout=us
DefaultIM=wbpy

[Groups/0/Items/0]
Name=wbpy
Layout=

[Groups/0/Items/1]
Name=keyboard-us
Layout=

[GroupOrder]
0=Default
EOF
chown -R "$USER_ID:$USER_ID" "/home/${USER_NAME}/.config/fcitx5"

# Eager table addon, system wide (the session may start fcitx as either user).
if [ -f /usr/share/fcitx5/addon/table.conf ]; then
    sed -i 's/^OnDemand=True/OnDemand=False/' /usr/share/fcitx5/addon/table.conf
    install -d -o "$USER_ID" -g "$USER_ID" "/home/${USER_NAME}/.local/share/fcitx5/addon"
    sed 's/^OnDemand=True/OnDemand=False/' /usr/share/fcitx5/addon/table.conf \
        >"/home/${USER_NAME}/.local/share/fcitx5/addon/table.conf"
    chown -R "$USER_ID:$USER_ID" "/home/${USER_NAME}/.local/share/fcitx5"
fi

# xterm: no XIM (see above).
install -d /etc/X11/app-defaults
touch /etc/X11/app-defaults/XTerm
grep -q '^XTerm\*inputMethod: none' /etc/X11/app-defaults/XTerm || \
    printf '\n! Xlib XIM swallows keystrokes with this fcitx5/XIM pair.\nXTerm*inputMethod: none\n' \
        >>/etc/X11/app-defaults/XTerm

# Client defaults for anything started outside the session as well.
cat >/etc/profile.d/92-asterinas-im.sh <<'EOF'
# fcitx5 as the input method for GTK and Qt clients. XIM is deliberately not
# registered by the session; see desktop_input_method.sh.
export GTK_IM_MODULE=fcitx
export QT_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx
EOF
chmod 0644 /etc/profile.d/92-asterinas-im.sh

echo "ASTERINAS_INPUT_METHOD ims=$(ls /usr/share/fcitx5/inputmethod 2>/dev/null | wc -l) tables=$(ls /usr/share/fcitx5/table 2>/dev/null | wc -l) profile=wbpy"
