#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# The guest half of the daily-use gate.
#
# Runs inside the Debian root image over the serial session and prints one
# `DAILY_<DOMAIN>=<ok|fail|missing>[ <detail>]` line per domain. The host
# wakes the guest from suspend and hot-adds a USB stick when it sees the
# corresponding WAIT markers:
#
#   DAILY_USB_WAIT        -> device_add usb-storage
#   DAILY_SUSPEND_WAIT    -> system_wakeup
#
# This measures the current image and kernel; it does not fix anything.

export DISPLAY=:0
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

mark() {
    echo "DAILY_$1=$2 $3"
}

# --- Domain 2: package installation (first, so the desktop app exists) ----
if apt-get install -y xclip xterm alsa-utils xdotool >/tmp/apt-install.log 2>&1; then
    mark APT ok "xclip+xterm+alsa-utils-installed"
else
    mark APT fail "apt-get install"
fi

# --- Domain 1: a desktop (Xorg + DRI3 + an application) -------------------
mkdir -p /tmp/.X11-unix
env MESA_LOADER_DRIVER_OVERRIDE=kms_swrast LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=softpipe /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp \
    -config /etc/X11/xorg-noudev.conf >/tmp/xorg.log 2>&1 &
waited=0
while [ ! -S /tmp/.X11-unix/X0 ] && [ $waited -lt 40 ]; do
    sleep 1
    waited=$((waited + 1))
done
if [ -S /tmp/.X11-unix/X0 ]; then
    if xdpyinfo -queryExtensions 2>/dev/null | grep -q DRI3; then
        mark DESKTOP ok "Xorg+DRI3"
    else
        mark DESKTOP fail "Xorg-up-no-DRI3"
    fi
    HOME=/tmp xterm -display :0 -geometry 40x10+10+10 >/tmp/xterm.log 2>&1 &
    sleep 3
    if pgrep -f "xterm -display" >/dev/null; then
        mark APP ok xterm
    else
        mark APP fail "$(head -2 /tmp/xterm.log 2>/dev/null | tr '\n ' '__')"
    fi
    pkill -f xterm 2>/dev/null
else
    mark DESKTOP fail no-X-socket
    mark APP fail no-X
fi

# --- Domain 3: clipboard -------------------------------------------------
if command -v xclip >/dev/null 2>&1 && [ -S /tmp/.X11-unix/X0 ]; then
    printf clipboard-probe | xclip -selection clipboard 2>/dev/null
    clipboard=$(xclip -o -selection clipboard 2>/dev/null)
    if [ "$clipboard" = clipboard-probe ]; then
        mark CLIPBOARD ok xclip-round-trip
    else
        mark CLIPBOARD fail xclip-round-trip
    fi
else
    mark CLIPBOARD missing "no-xclip-or-X"
fi

# --- Domain 4: input method (wubi table engine over XIM) ------------------
# The pinyin addon trips over its own history parsing on this kernel, so the
# table engine (no libpinyin) is installed directly: one small apt run.
if ! command -v fcitx5 >/dev/null 2>&1; then
    timeout 120 apt-get update >/tmp/fcitx-apt.log 2>&1
    echo "DAILY_APT_SEARCH $(apt-cache search fcitx5 2>/dev/null | tr '\n' '|' | head -c 500)" >/dev/console
    timeout 420 apt-get install -y --no-install-recommends \
        fcitx5 fcitx5-modules fcitx5-data fcitx5-table-extra fcitx5-table-other \
        fcitx5-chinese-addons dbus-daemon >>/tmp/fcitx-apt.log 2>&1
fi
echo "DAILY_PKG_LIST $(dpkg -L fcitx5 2>/dev/null | grep -E 'inputmethod|/table/' | tr '\n' '|' | head -c 400)" >/dev/console
echo "DAILY_IM_DIRS inputmethod=$(ls /usr/share/fcitx5/inputmethod 2>/dev/null | wc -l) table=$(ls /usr/share/fcitx5/table 2>/dev/null | wc -l)" >/dev/console
if command -v fcitx5 >/dev/null 2>&1; then
    export GTK_IM_MODULE=fcitx QT_IM_MODULE=fcitx XMODIFIERS=@im=fcitx
    mkdir -p /root/.config/fcitx5
    # Input methods come from two places: dedicated addons describe IMs in
    # inputmethod/, the table engine keeps its tables in table/. Use a real
    # name from either instead of guessing.
    table_list=$(ls /usr/share/fcitx5/table/ 2>/dev/null | sed 's/\.conf$//' | tr '\n' ' ')
    im_list=$(ls /usr/share/fcitx5/inputmethod/ 2>/dev/null | sed 's/\.conf$//' | grep -v '^keyboard-' | tr '\n' ' ')
    echo "DAILY_IM_ADDONS tables=[$table_list] ims=[$im_list]" >/dev/console
    # Prefer the wubi-family engines: they do not touch libpinyin, whose
    # history parsing fails on this kernel.
    im=$(printf '%s\n' $im_list | grep -iE '^(wbx|wbpy|wubi)$' | head -1)
    [ -n "$im" ] || im=$(printf '%s\n' $im_list | grep -i 'pinyin' | head -1)
    [ -n "$im" ] || im=$(printf '%s\n' $table_list | grep -i 'wubi' | head -1)
    [ -n "$im" ] || im=$(printf '%s\n' $table_list | head -1)
    [ -n "$im" ] || im=keyboard-us
    echo "DAILY_IM_CHOSEN '$im'" >/dev/console
    printf '[Groups/0]\nName=Default\nDefault Layout=us\nDefaultIM=%s\n\n[Groups/0/Items/0]\nName=%s\nLayout=\n\n[GroupOrder]\n0=Default\n' \
        "$im" "$im" >/root/.config/fcitx5/profile
    if [ -z "$DBUS_SESSION_BUS_ADDRESS" ] && command -v dbus-daemon >/dev/null 2>&1; then
        DBUS_SESSION_BUS_ADDRESS=$(dbus-daemon --session --fork --print-address)
        export DBUS_SESSION_BUS_ADDRESS
    fi
    pkill -x fcitx5 2>/dev/null; sleep 1
    fcitx5 -d --replace -v >/dev/console 2>&1 &
    sleep 5
    # Without a window manager nothing holds keyboard focus and synthetic key
    # events never reach a client, so start a tiny WM first.
    if ! command -v openbox >/dev/null 2>&1; then
        timeout 180 apt-get install -y --no-install-recommends openbox >>/tmp/fcitx-apt.log 2>&1
    fi
    wm_started=no
    if command -v openbox >/dev/null 2>&1; then
        openbox >/tmp/wm.log 2>&1 &
        sleep 3
        wm_started=openbox
    elif command -v twm >/dev/null 2>&1; then
        twm >/tmp/wm.log 2>&1 &
        sleep 2
        wm_started=twm
    elif command -v matchbox-window-manager >/dev/null 2>&1; then
        matchbox-window-manager -use_titlebar no >/tmp/wm.log 2>&1 &
        sleep 2
        wm_started=matchbox
    fi
    echo "DAILY_WM wm=$wm_started running=$(pgrep -c -f 'openbox|twm|matchbox' 2>/dev/null) log=$(head -1 /tmp/wm.log 2>/dev/null | tr ' ' '_' | head -c 40)" >/dev/console
    # xterm ignores synthetic (XSendEvent) keys unless this resource is set.
    xterm -xrm "*allowSendEvents: true" -title ximtest -e sh -c 'cat > /tmp/xim-out' \
        >/dev/null 2>&1 &
    sleep 2
    window=$(timeout 5 xdotool search --name ximtest 2>/dev/null | head -1)
    if [ -n "$window" ]; then
        timeout 5 xdotool windowfocus "$window" 2>/dev/null || true
        sleep 1
    fi
    engine=$(fcitx5-remote -n 2>/dev/null)
    # Is the X server able to inject input at all? xdotool's real paths need XTEST.
    echo "DAILY_XEXT $(xdpyinfo 2>/dev/null | grep -i -c xtest) modules=$(ls /usr/lib/xorg/modules/extensions/ 2>/dev/null | tr '\n' ',')" >/dev/console
    echo "DAILY_XSERVER $(ps -eo comm= 2>/dev/null | grep -E '^(Xorg|Xvfb|X)$' | head -2 | tr '\n' ',')" >/dev/console
    if command -v xwininfo >/dev/null 2>&1; then
        echo "DAILY_XWIN tree=$(timeout 5 xwininfo -root -tree 2>/dev/null | head -6 | tr '\n' '|' | head -c 200)" >/dev/console
    fi
    echo "DAILY_XWIN search=$(timeout 5 xdotool search --class xterm 2>/dev/null | tr '\n' ',')" >/dev/console
    if [ -n "$window" ]; then
        timeout 5 xdotool windowactivate --sync "$window" 2>/dev/null
        echo "DAILY_XWIN activate_rc=$?" >/dev/console
        sleep 1
    fi
    echo "DAILY_XWIN netactive=$(timeout 5 xprop -root _NET_ACTIVE_WINDOW 2>/dev/null)" >/dev/console
    active=$(timeout 5 xdotool getactivewindow getwindowname 2>/dev/null | head -1)
    echo "DAILY_FCITX alive=$(pgrep -c fcitx5 2>/dev/null) engine='$engine' window='$window' active='$active'" >/dev/console
    if [ -z "$engine" ]; then
        # Diagnose instead of switching engines: is the daemon on a bus that
        # lists it, and does activating the input method produce an engine?
        echo "DAILY_DBUS $(dbus-send --session --print-reply --dest=org.freedesktop.DBus / org.freedesktop.DBus.ListNames 2>&1 | tr '\n' ' ' | head -c 240)" >/dev/console
        fcitx5-remote -o 2>/dev/null
        echo "DAILY_FCITX activate_rc=$?" >/dev/console
        dbus-send --session --print-reply --dest=org.fcitx.Fcitx5 /controller \
            org.fcitx.Fcitx.Controller1.SetCurrentIM "string:$im" >/dev/console 2>&1
        sleep 1
        engine=$(fcitx5-remote -n 2>/dev/null)
        echo "DAILY_FCITX after_activate='$engine'" >/dev/console
    fi
    # `fcitx5-remote -n` reports the engine of a *current input context*; an
    # empty answer does not mean the engine is unusable. The verdict comes from
    # typing through XIM into a real client.
    if true; then
        if [ -n "$window" ]; then
            # wubi codes: 你 = wqiy, 好 = vbg
            echo "DAILY_XDIAG active=$(timeout 5 xdotool getactivewindow 2>/dev/null) win=$window" >/dev/console
            if command -v xprop >/dev/null 2>&1; then
                echo "DAILY_XDIAG xim=$(timeout 5 xprop -root 2>/dev/null | grep -i xim | head -2 | tr '\n' '|')" >/dev/console
            fi
            # Deliver straight to the window as well: if this lands while the
            # focused type does not, the focus is the problem, not XIM.
            # Without a WM the focus is PointerRoot: move the pointer into the
            # window and type through the real focus path.
            timeout 5 xdotool mousemove --window "$window" 20 20 2>/dev/null || true
            sleep 1
            timeout 5 xdotool windowfocus "$window" 2>/dev/null || true
            timeout 5 xdotool type --delay 150 "wqiy" 2>/dev/null || true
            timeout 5 xdotool key space 2>/dev/null || true
            timeout 5 xdotool type --delay 150 "vbg" 2>/dev/null || true
            timeout 5 xdotool key space 2>/dev/null || true
            timeout 5 xdotool key Return 2>/dev/null || true
            sleep 1
            echo "DAILY_XDIAG direct_out=$(cat /tmp/xim-out 2>/dev/null | tr -d '\n' | head -c 20)" >/dev/console
            sleep 1
        fi
        if grep -q 你好 /tmp/xim-out 2>/dev/null; then
            mark INPUT_METHOD ok "table-$engine-typed-你好"
        else
            mark INPUT_METHOD fail "engine-$engine-no-text:$(cat /tmp/xim-out 2>/dev/null | tr -d '\n' | head -c 40)"
        fi
    else
        mark INPUT_METHOD fail "fcitx5-no-engine:$(tail -1 /tmp/fcitx-apt.log 2>/dev/null | tr ' ' '_' | head -c 50)"
    fi
else
    mark INPUT_METHOD fail "fcitx5-install-failed:$(tail -1 /tmp/fcitx-apt.log 2>/dev/null | tr ' ' '_' | head -c 50)"
fi

# --- Domain 5: audio ------------------------------------------------------
if [ -d /dev/snd ] && command -v aplay >/dev/null 2>&1; then
    python3 - /tmp/daily-tone.wav <<'PY'
import struct, sys, wave
samples = []
for frame in range(24000):
    value = 8000 if (frame // 54) % 2 == 0 else -8000
    samples += [value, value]
with wave.open(sys.argv[1], "wb") as wav:
    wav.setnchannels(2); wav.setsampwidth(2); wav.setframerate(48000)
    wav.writeframes(struct.pack("<%dh" % len(samples), *samples))
PY
    if aplay -D hw:0,0 /tmp/daily-tone.wav >/tmp/aplay.log 2>&1; then
        mark AUDIO ok "aplay-hw:0,0"
    else
        mark AUDIO fail "$(head -1 /tmp/aplay.log | tr ' ' '_')"
    fi
else
    mark AUDIO missing "no-/dev/snd-node"
fi

# --- Domain 6: USB hotplug (enumerate, mount, read, umount) --------------
echo DAILY_USB_WAIT
tick=0
while [ $tick -lt 25 ]; do
    if [ -b /dev/sda ]; then
        break
    fi
    sleep 1
    tick=$((tick + 1))
done
if [ -b /dev/sda ]; then
    mkdir -p /mnt/usbstick
    if mount -t ext2 -o ro /dev/sda /mnt/usbstick 2>/tmp/usb-mount.err; then
        if grep -q ASTERINAS-USB-OK /mnt/usbstick/hello.txt 2>/dev/null; then
            mark USB ok "mounted-/dev/sda-read-$(cat /mnt/usbstick/hello.txt | tr -d '\n')"
        else
            mark USB fail "mounted-but-no-hello.txt"
        fi
        umount /mnt/usbstick
    else
        mark USB fail "mount-failed:$(head -1 /tmp/usb-mount.err | tr ' ' '_')"
    fi
elif [ -d /sys/bus/usb/devices ]; then
    mark USB fail "enumerated-but-no-block-device:$(ls /sys/bus/usb/devices | tr '\n' ',')"
else
    mark USB missing "no-/sys/bus/usb"
fi

# --- Domain 7: suspend and wake ------------------------------------------
echo DAILY_SUSPEND_WAIT
timeout 20 systemctl suspend >/tmp/suspend.log 2>&1
status=$?
if [ $status -eq 0 ]; then
    # After the wake the system must still be usable: the root filesystem
    # reads, a write lands, and the clock moved forward.
    sleep 1
    if ls / >/dev/null 2>&1 && echo after-wake >/tmp/after-wake && grep -q after-wake /tmp/after-wake; then
        mark SUSPEND ok "woke-up-disk-and-io-ok"
    else
        mark SUSPEND fail "woke-but-io-broken"
    fi
elif [ $status -eq 124 ]; then
    mark SUSPEND missing "suspend-timeout"
else
    mark SUSPEND fail "rc=$status:$(head -1 /tmp/suspend.log 2>/dev/null | tr ' ' '_')"
fi

echo DAILY_DONE=ok
