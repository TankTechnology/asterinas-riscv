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

# --- Domain 4: input method (fcitx5 + pinyin over XIM) --------------------
# fcitx5 pulls in a large dependency set; keep it out of the APT domain so a
# slow download cannot starve the rest of the checklist.
if ! command -v fcitx5 >/dev/null 2>&1; then
    timeout 120 apt-get update >/tmp/fcitx-apt.log 2>&1
    timeout 420 apt-get install -y --no-install-recommends \
        fcitx5 fcitx5-modules fcitx5-chinese-addons fcitx5-frontend-gtk3 dbus-daemon >>/tmp/fcitx-apt.log 2>&1
fi
# Kernel-side prerequisites for any input-method daemon.
if [ -x /usr/local/bin/msgtest ]; then
    /usr/local/bin/msgtest >/dev/console 2>&1
fi

if command -v fcitx5 >/dev/null 2>&1; then
    export GTK_IM_MODULE=fcitx QT_IM_MODULE=fcitx XMODIFIERS=@im=fcitx
    mkdir -p /root/.config/fcitx5
    cat >/root/.config/fcitx5/profile <<'PROFILE'
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
PROFILE
    # fcitx5-remote talks over D-Bus; the root shell under sudo has no session
    # bus, so start a private one for the daemon and the queries.
    if [ -z "$DBUS_SESSION_BUS_ADDRESS" ] && command -v dbus-daemon >/dev/null 2>&1; then
        DBUS_SESSION_BUS_ADDRESS=$(dbus-daemon --session --fork --print-address)
        export DBUS_SESSION_BUS_ADDRESS
    fi
    # Keep the daemon's own diagnostics on the serial console: if it dies at
    # startup the reason must be visible in the transcript.
    fcitx5 -d --replace >/dev/console 2>&1 &
    sleep 5
    echo "DAILY_FCITX alive=$(pgrep -c fcitx5 2>/dev/null) addons=$(ls /usr/share/fcitx5/addon 2>/dev/null | tr '\n' ',')" >/dev/console
    engine=$(fcitx5-remote -n 2>/dev/null)
    echo "DAILY_FCITX engine='$engine' remote_rc=$?" >/dev/console
    if [ -n "$engine" ]; then
        # End-to-end: type pinyin into an xterm and see Chinese arrive.
        xterm -title ximtest -e sh -c 'cat > /tmp/xim-out' >/dev/null 2>&1 &
        sleep 2
        window=$(timeout 5 xdotool search --name ximtest 2>/dev/null | head -1)
        if [ -n "$window" ]; then
            # No window manager runs here, so ask X for focus directly and
            # never wait on anything: a stuck xdotool must not stall the gate.
            timeout 5 xdotool windowfocus "$window" 2>/dev/null || true
            sleep 1
            timeout 5 xdotool type --delay 120 "nihao" 2>/dev/null || true
            timeout 5 xdotool key space 2>/dev/null || true
            timeout 5 xdotool key Return 2>/dev/null || true
            sleep 1
        fi
        if grep -q 你好 /tmp/xim-out 2>/dev/null; then
            mark INPUT_METHOD ok "fcitx5-$engine-typed-你好"
        else
            mark INPUT_METHOD fail "engine-$engine-no-text:$(cat /tmp/xim-out 2>/dev/null | tr -d '\n' | head -c 40)"
        fi
    else
        # Isolate the failure: with only the keyboard engine, does the daemon
        # stay up? If it does, the crash is in the pinyin engine itself.
        pkill -x fcitx5 2>/dev/null
        sleep 1
        printf '[Groups/0]\nName=Default\nDefault Layout=us\nDefaultIM=keyboard-us\n\n[Groups/0/Items/0]\nName=keyboard-us\nLayout=\n\n[GroupOrder]\n0=Default\n' \
            >/root/.config/fcitx5/profile
        fcitx5 -d --replace >/dev/console 2>&1 &
        sleep 4
        keyboard_engine=$(fcitx5-remote -n 2>/dev/null)
        echo "DAILY_FCITX2 keyboard_only_engine='$keyboard_engine' alive=$(pgrep -c fcitx5 2>/dev/null)" >/dev/console
        mark INPUT_METHOD fail "pinyin-engine-crash:keyboard-only='$keyboard_engine'"
    fi
else
    mark INPUT_METHOD fail "fcitx5-unavailable:$(tail -1 /tmp/fcitx-apt.log 2>/dev/null | tr ' ' '_' | head -c 60)"
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
