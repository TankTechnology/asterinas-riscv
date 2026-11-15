#!/bin/bash
# SPDX-License-Identifier: MPL-2.0
#
# Configure the Chinese input method inside the staged rootfs.
#
# This is the RISC-V port of the session that already works on the x86 image
# (tools/x86/debian/daily_session.sh). What matters here:
#
# * the keyboard layout has to be the *first* item of the group. fcitx's
#   InputMethodGroup::setDefaultInputMethod treats item 0 as the layout slot:
#   when the requested default is item 0 and the group holds more than one
#   item, it silently picks item 1 instead. With [pinyin, keyboard-us] the
#   profile says DefaultIM=pinyin while every context really lands on
#   keyboard-us; [keyboard-us, pinyin] is what makes pinyin the default
#   (verified on the board: the focused context reports pinyin and `nihao` +
#   space commits 你好 with no manual switch).
# * the engine is pinyin (libpinyin, from fcitx5-chinese-addons). It ships
#   with OnDemand=True; the addon is made eager so the engine is loaded with
#   the rest of the input-method addons instead of on first use.
# * the input method must be *active* out of the box. fcitx5 starts inactive
#   by default, and an inactive context passes every key through unchanged --
#   typing pinyin then produces literal letters and looks exactly like a
#   broken engine. ActiveByDefault asks for that.
# * XDG_RUNTIME_DIR has to be per user. A single value in /etc/environment
#   makes a dbus-daemon started as root refuse to run ("owned by uid 1000, not
#   our uid 0").
# * xterm's Xlib XIM never receives keys here, so xterm is told not to use an
#   input method at all. Chinese stays available in the GTK clients, which go
#   through fcitx's GTK frontend.
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
DefaultIM=pinyin

[Groups/0/Items/0]
Name=keyboard-us
Layout=

[Groups/0/Items/1]
Name=pinyin
Layout=

[GroupOrder]
0=Default
EOF

# Start with the input method switched on; see the header for why.
cat >"/home/${USER_NAME}/.config/fcitx5/config" <<'EOF'
[Behavior]
ActiveByDefault=True
EOF
chown -R "$USER_ID:$USER_ID" "/home/${USER_NAME}/.config/fcitx5"

# Load the engine with the rest of the input-method addons rather than on
# first use, so the first keystroke in a fresh session already has a live
# pinyin engine behind it.
if [ -f /usr/share/fcitx5/addon/pinyin.conf ]; then
    sed -i 's/^OnDemand=True/OnDemand=False/' /usr/share/fcitx5/addon/pinyin.conf
    install -d -o "$USER_ID" -g "$USER_ID" "/home/${USER_NAME}/.local/share/fcitx5/addon"
    sed 's/^OnDemand=True/OnDemand=False/' /usr/share/fcitx5/addon/pinyin.conf \
        >"/home/${USER_NAME}/.local/share/fcitx5/addon/pinyin.conf"
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

echo "ASTERINAS_INPUT_METHOD ims=$(ls /usr/share/fcitx5/inputmethod 2>/dev/null | wc -l) tables=$(ls /usr/share/fcitx5/table 2>/dev/null | wc -l) profile=pinyin"
