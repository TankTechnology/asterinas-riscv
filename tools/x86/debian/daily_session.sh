#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Install and verify the Asterinas Debian desktop session inside the guest.
#
# This is the guest-side half of the "boot straight into a usable desktop"
# work: the image ships the packages and configs below, and this script makes
# an existing image match, so the change is reproducible from the repo rather
# than from a hand-edited disk.
#
# What it installs:
#
# - openbox as the window manager, pcmanfm --desktop for icons and wallpaper,
#   lxpanel for the application menu and the task bar;
# - fcitx5 with the wbpy (五笔拼音) table engine, its GTK3/GTK2 frontends and
#   the WenQuanYi CJK font, so a freshly opened window starts in Chinese and
#   the candidates render instead of tofu;
# - asterinas-session / asterinas-desktop plus a systemd unit in
#   graphical.target, which run Xorg as root (the DRM and input nodes are
#   root-only on this kernel) and the session as uid 1000;
# - the image-level IM defaults (GTK_IM_MODULE=fcitx, XMODIFIERS=@im=fcitx).
#
# Run it as root inside the guest. It is idempotent.

set -euo pipefail

SERIAL_USER=asterinas
SERIAL_UID=1000

if [ "$(id -u)" -ne 0 ]; then
    echo "daily_session.sh: run as root" >&2
    exit 1
fi

echo "DAILY_SESSION_PACKAGES"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    openbox pcmanfm lxpanel x11-xserver-utils xterm x11-utils \
    fcitx5 fcitx5-table fcitx5-table-extra fcitx5-chinese-addons \
    fcitx5-frontend-gtk3 fcitx5-frontend-gtk2 \
    fonts-wqy-microhei dbus-daemon >/tmp/daily-session-apt.log 2>&1 || {
    tail -20 /tmp/daily-session-apt.log >&2
    exit 1
}

# --- Xorg -------------------------------------------------------------------
# Xorg binds /dev/input/event0 directly: the image has no udev and the kernel
# does not yet implement logind seat handoff, so the modesetting driver is
# given an explicit evdev keyboard. The server is started by
# asterinas-desktop, not by this config.
install -d /etc/X11
cat >/etc/X11/xorg-kbd.conf <<'EOF'
Section "Device"
    Identifier "Asterinas DRM"
    Driver "modesetting"
    Option "AccelMethod" "glamor"
EndSection

Section "Screen"
    Identifier "Asterinas screen"
    Device "Asterinas DRM"
EndSection

Section "InputDevice"
    Identifier "kbd"
    Driver "evdev"
    Option "Device" "/dev/input/event0"
EndSection

Section "ServerLayout"
    Identifier "Asterinas layout"
    Screen 0 "Asterinas screen"
    InputDevice "kbd" "CoreKeyboard"
EndSection

Section "ServerFlags"
    Option "AutoAddDevices" "false"
    Option "AutoEnableDevices" "false"
    Option "BlankTime" "0"
    Option "StandbyTime" "0"
    Option "SuspendTime" "0"
    Option "OffTime" "0"
EndSection
EOF

# --- Session ----------------------------------------------------------------
# The session runs as uid 1000. It owns a private D-Bus session bus, starts the
# input method first (so XIM/GTK clients find it when they start), then the WM,
# the desktop icons and the panel.
cat >/usr/local/bin/asterinas-session <<'EOF'
#!/bin/sh
# The Asterinas Debian desktop session. Started as uid 1000.
export DISPLAY=:0
export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe GDK_GL=disable
export GDK_BACKEND=x11 NO_AT_BRIDGE=1
export GTK_IM_MODULE=fcitx QT_IM_MODULE=fcitx XMODIFIERS=@im=fcitx
export XDG_RUNTIME_DIR=/tmp/xdg-asterinas
mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    DBUS_SESSION_BUS_ADDRESS=$(dbus-daemon --session --fork --print-address)
    export DBUS_SESSION_BUS_ADDRESS
fi
echo "$DBUS_SESSION_BUS_ADDRESS" > /tmp/asterinas-session-dbus
# The input method first, so IM clients find it as they start.
fcitx5 -d --replace >/tmp/fcitx5.log 2>&1
sleep 1
openbox >/tmp/openbox.log 2>&1 &
pcmanfm --desktop --profile=Asterinas >/tmp/pcmanfm.log 2>&1 &
lxpanel >/tmp/lxpanel.log 2>&1 &
wait
EOF
chmod 755 /usr/local/bin/asterinas-session

# Xorg must run as root: /dev/dri/card0 and /dev/input/event0 are 0600
# root-owned on this kernel, and the image has no Xorg.wrap. The session is
# dropped to uid 1000 with setpriv, the same way the serial login helper does.
cat >/usr/local/sbin/asterinas-desktop <<'EOF'
#!/bin/sh
# Start the Asterinas Debian desktop: Xorg as root and the session as uid 1000.
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=softpipe
if [ ! -S /tmp/.X11-unix/X0 ]; then
    rm -f /tmp/.X0-lock
    Xorg :0 -noreset -nolisten tcp -config /etc/X11/xorg-kbd.conf >/tmp/xorg.log 2>&1 &
    xpid=$!
    i=0
    while [ $i -lt 150 ]; do
        [ -S /tmp/.X11-unix/X0 ] && break
        kill -0 "$xpid" 2>/dev/null || { echo "Xorg exited"; tail -20 /tmp/xorg.log; exit 1; }
        sleep 0.2
        i=$((i+1))
    done
    [ -S /tmp/.X11-unix/X0 ] || { echo "Xorg did not start"; exit 1; }
fi
exec setpriv --reuid=1000 --regid=1000 --init-groups \
    env HOME=/home/asterinas USER=asterinas LOGNAME=asterinas SHELL=/bin/sh \
    DISPLAY=:0 XDG_RUNTIME_DIR=/tmp/xdg-asterinas \
    LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe GDK_GL=disable \
    GDK_BACKEND=x11 NO_AT_BRIDGE=1 \
    GTK_IM_MODULE=fcitx XMODIFIERS=@im=fcitx QT_IM_MODULE=fcitx \
    /usr/local/bin/asterinas-session
EOF
chmod 755 /usr/local/sbin/asterinas-desktop

cat >/etc/systemd/system/asterinas-desktop.service <<'EOF'
[Unit]
Description=Asterinas Debian desktop session
After=systemd-user-sessions.service

[Service]
Type=simple
ExecStart=/usr/local/sbin/asterinas-desktop
Restart=on-failure
RestartSec=2
KeyringMode=inherit

[Install]
WantedBy=graphical.target
EOF
systemctl daemon-reload
systemctl enable asterinas-desktop >/dev/null

# XDG_RUNTIME_DIR is per-user. /tmp/xdg belongs to the session user (GLib
# needs to create dconf/GIO directories there), while root gets its own
# directory: a dbus-daemon started through sudo rejects a runtime dir owned
# by another uid, and the acceptance harness starts one as root.
install -d /etc/tmpfiles.d
cat >/etc/tmpfiles.d/asterinas-session.conf <<'EOF'
d /tmp/xdg 0700 asterinas asterinas -
d /tmp/xdg-asterinas 0700 asterinas asterinas -
d /tmp/xdg-root 0700 root root -
EOF
systemd-tmpfiles --create >/dev/null 2>&1 || true
chown -R "$SERIAL_USER:$SERIAL_USER" /tmp/xdg /tmp/xdg-asterinas 2>/dev/null || true
chown root:root /tmp/xdg-root 2>/dev/null || true

# /etc/environment is shared by every user, so it must not name a per-user
# runtime directory. Interactive shells set one based on the uid instead.
sed -i '/^XDG_RUNTIME_DIR=/d' /etc/environment 2>/dev/null || true
if [ -f /etc/bash.bashrc ]; then
    sed -i 's#^\[ -n "${XDG_RUNTIME_DIR:-}" \] || { \[ -d /tmp/xdg \] && export XDG_RUNTIME_DIR=/tmp/xdg; }#[ "$(id -u)" -eq 0 ] \&\& export XDG_RUNTIME_DIR=/tmp/xdg-root || true#' /etc/bash.bashrc
fi
if [ -f /etc/profile.d/90-asterinas-softgl.sh ]; then
    if ! grep -q 'XDG_RUNTIME_DIR is per-user' /etc/profile.d/90-asterinas-softgl.sh; then
        sed -i '/XDG_RUNTIME_DIR/d' /etc/profile.d/90-asterinas-softgl.sh
        cat >>/etc/profile.d/90-asterinas-softgl.sh <<'EOF'
# XDG_RUNTIME_DIR is per-user: root gets its own directory so that a
# dbus-daemon started through sudo is not rejected by a uid mismatch.
if [ "$(id -u)" -eq 0 ]; then
    [ -n "${XDG_RUNTIME_DIR:-}" ] || export XDG_RUNTIME_DIR=/tmp/xdg-root
else
    [ -n "${XDG_RUNTIME_DIR:-}" ] || export XDG_RUNTIME_DIR=/tmp/xdg
fi
EOF
    fi
fi

# xterm uses Xlib XIM, and fcitx5's XIM client swallows its keystrokes (the
# physical keyboard produces no characters at all, not even Latin ones). Turn
# XIM off for xterm; Chinese input stays available in the GTK clients
# (lxterminal, mousepad), which use the fcitx frontend.
if ! grep -q '^XTerm\*inputMethod: none' /etc/X11/app-defaults/XTerm 2>/dev/null; then
    cat >>/etc/X11/app-defaults/XTerm <<'EOF'

! Xlib XIM swallows keystrokes on this fcitx5/XIM pair; see
! docs/porting/2026-10-28-desktop-session.md. Chinese input is available in
! the GTK clients (lxterminal, mousepad), which use the fcitx frontend.
XTerm*inputMethod: none
EOF
fi

# --- Input method defaults for the user -------------------------------------
# wbpy is first in the group, so every new input context starts in Chinese
# (Ctrl+Space toggles back to Latin). The table addon is made eager: while it
# is loaded on demand, fcitx parses the profile before the engine exists and
# drops wbpy from the group, leaving new windows in keyboard-us.
install -d -o "$SERIAL_USER" -g "$SERIAL_USER" /home/asterinas/.config/fcitx5
cat >/home/asterinas/.config/fcitx5/profile <<'EOF'
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
# Make the table addon eager for every user, not only uid 1000: the harness
# starts fcitx as root too, and a lazy addon makes it drop wbpy from the
# profile at startup (no engine, no Chinese input).
sed -i 's/^OnDemand=True/OnDemand=False/' /usr/share/fcitx5/addon/table.conf
install -d -o "$SERIAL_USER" -g "$SERIAL_USER" /home/asterinas/.local/share/fcitx5/addon
sed 's/^OnDemand=True/OnDemand=False/' \
    /usr/share/fcitx5/addon/table.conf \
    >/home/asterinas/.local/share/fcitx5/addon/table.conf
chown -R "$SERIAL_USER:$SERIAL_USER" \
    /home/asterinas/.config/fcitx5 /home/asterinas/.local/share/fcitx5

# --- Image-level IM environment ---------------------------------------------
# GTK clients launched from a serial shell (not from the session) should also
# reach fcitx; the fcitx5 GTK frontend is installed above.
for f in /etc/environment /etc/bash.bashrc /etc/profile.d/91-asterinas-im.sh; do
    [ -f "$f" ] && sed -i 's/GTK_IM_MODULE=xim/GTK_IM_MODULE=fcitx/g' "$f"
done

echo "DAILY_SESSION_UNIT $(systemctl is-enabled asterinas-desktop 2>/dev/null || echo unknown)"
echo "DAILY_SESSION_ENGINES ims=$(ls /usr/share/fcitx5/inputmethod 2>/dev/null | wc -l) table=$(ls /usr/share/fcitx5/table 2>/dev/null | wc -l)"
echo "DAILY_SESSION_FONT $(fc-list 2>/dev/null | grep -ci 'wqy\|cjk')"
echo "DAILY_SESSION_DONE"
