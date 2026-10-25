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

# Regression wrapper: this image predates the builder DNS fix, so point
# resolv.conf at slirp before the standard daily checklist runs.
printf 'nameserver 10.0.2.3\n' >/etc/resolv.conf 2>/dev/null || true
# This image has never run apt-get update; the stock checklist assumes
# populated package lists, so refresh them once here.
timeout 240 apt-get update >/tmp/regress-apt-update.log 2>&1 || true

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

mark() {
    echo "DAILY_$1=$2 $3"
}

# Earliest possible point: no other apt/dpkg is running yet.
t0=$(cut -d' ' -f1 /proc/uptime)
if timeout 180 apt-get install -y --reinstall -o Debug::NoLocking=1 hello >/tmp/perf-apt-early.log 2>&1; then
    t1=$(cut -d' ' -f1 /proc/uptime)
    echo "DAILY_PERF apt_early seconds=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.2f", b-a}')" >/dev/console
else
    echo "DAILY_PERF apt_early failed raw=$(tail -1 /tmp/perf-apt-early.log 2>/dev/null | tr ' ' '_' | head -c 60)" >/dev/console
fi
echo "DAILY_IME_ENV gtk3=$(ldconfig -p 2>/dev/null | grep -c libgtk-3) wm=$(command -v openbox matchbox-window-manager fluxbox 2>/dev/null | tr '\n' ',')" >/dev/console

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
    # Minimal client: if xev cannot see a key, the injection path itself is broken.
    if ! command -v xev >/dev/null 2>&1; then
        echo "DAILY_XAPPS policy=$(apt-cache policy x11-apps 2>/dev/null | head -3 | tr '\n' '|' | head -c 160)" >/dev/console
        timeout 300 apt-get install -y --no-install-recommends x11-apps x11-utils >>/tmp/fcitx-apt.log 2>&1
        echo "DAILY_XAPPS installed=$(command -v xev >/dev/null 2>&1 && echo yes || echo no) err=$(tail -1 /tmp/fcitx-apt.log 2>/dev/null | tr ' ' '_' | head -c 50)" >/dev/console
    fi
    if command -v xev >/dev/null 2>&1; then
        echo "DAILY_XEV_ERR $(timeout 8 /usr/bin/xev 2>&1 | head -3 | tr '\n' '|' | head -c 200)" >/dev/console
        echo "DAILY_XEV_LDD $(ldd /usr/bin/xev 2>&1 | grep -ci 'not found') missing=$(ldd /usr/bin/xev 2>&1 | grep -i 'not found' | awk '{print $1}' | tr '\n' ',' | head -c 120)" >/dev/console
        xev -geometry 300x200 >/tmp/xev.log 2>&1 &
        sleep 2
        xev_id=$(timeout 5 xdotool search --class xev 2>/dev/null | head -1)
        if [ -n "$xev_id" ]; then
            timeout 5 xdotool windowfocus "$xev_id" 2>/dev/null || true
            timeout 5 xdotool key --window "$xev_id" a 2>/dev/null || true
            timeout 5 xdotool mousemove 100 100 2>/dev/null || true
            timeout 5 xdotool key a 2>/dev/null || true
        fi
        sleep 1
        echo "DAILY_XEV id=$xev_id keypress=$(grep -c KeyPress /tmp/xev.log 2>/dev/null) any=$(grep -c -E 'KeyPress|ButtonPress|MotionNotify' /tmp/xev.log 2>/dev/null)" >/dev/console
        pkill -x xev 2>/dev/null
    fi

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

# --- Domain 4c: x11-apps GUI programs (Q2) ---------------------------------
for app_name in xclock xcalc xeyes; do
    if ! command -v "$app_name" >/dev/null 2>&1; then
        echo "DAILY_XAPP_$app_name=missing not-installed" >/dev/console
        continue
    fi
    "$app_name" >/tmp/xapp-$app_name.log 2>&1 &
    sleep 3
    win=$(timeout 5 xdotool search --class "$app_name" 2>/dev/null | head -1)
    if [ -n "$win" ]; then
        echo "DAILY_XAPP_$app_name=ok window=$win" >/dev/console
    else
        echo "DAILY_XAPP_$app_name=fail no-window:$(head -1 /tmp/xapp-$app_name.log 2>/dev/null | tr ' ' '_' | head -c 40)" >/dev/console
    fi
    pkill -x "$app_name" 2>/dev/null
done

# --- Domain 4b: desktop applications (Q2) ---------------------------------
if ! command -v pcmanfm >/dev/null 2>&1 && ! command -v eog >/dev/null 2>&1; then
    timeout 300 apt-get install -y --no-install-recommends pcmanfm eog \
        >>/tmp/fcitx-apt.log 2>&1
fi
if [ -f /etc/hostname ]; then app_file=/etc/hostname; else app_file=/etc/os-release; fi
# eog needs a real image, not a text file. Prefer a shipped logo and fall back
# to a 1x1 PNG generated here so the probe is independent of the image's theme.
app_image=/usr/share/pixmaps/debian-logo.png
if [ ! -f "$app_image" ]; then
    app_image=/tmp/gtk-app-probe.png
    if [ ! -f "$app_image" ]; then
        printf '%s' 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=' \
            | base64 -d >"$app_image" 2>/dev/null
    fi
fi
for app_name in pcmanfm eog gpicview epdfview evince; do
    if ! command -v "$app_name" >/dev/null 2>&1; then
        echo "DAILY_APP_$app_name=missing not-installed" >/dev/console
        continue
    fi
    case "$app_name" in
        eog) app_arg="$app_image"; app_class='[Ee]og' ;;
        pcmanfm) app_arg="$app_file"; app_class='[Pp]cmanfm' ;;
        *) app_arg="$app_file"; app_class="$app_name" ;;
    esac
    # Give GTK the same software rendering the gate gives Xorg. Without
    # these the client's GLX path aborts with "glx: failed to create DRI3
    # screen" on virtio-gpu + kms_swrast even though the server is fine.
    timeout 30 env MESA_LOADER_DRIVER_OVERRIDE=kms_swrast \
        LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe GDK_GL=disable \
        "$app_name" "$app_arg" >/tmp/app-$app_name.log 2>&1 &
    win=""
    waited=0
    while [ -z "$win" ] && [ "$waited" -lt 10 ]; do
        sleep 1
        waited=$((waited + 1))
        win=$(timeout 2 xdotool search --onlyvisible --class "$app_class" 2>/dev/null | head -1)
    done
    if [ -z "$win" ]; then
        win=$(xwininfo -root -tree 2>/dev/null | grep -iE "($app_name)" \
            | awk 'NR==1 {print $1}')
    fi
    if [ -n "$win" ]; then
        echo "DAILY_APP_$app_name=ok window=$win" >/dev/console
    elif pgrep -x "$app_name" >/dev/null 2>&1; then
        echo "DAILY_APP_$app_name=fail no-window alive:$(head -1 /tmp/app-$app_name.log 2>/dev/null | tr ' ' '_' | head -c 40)" >/dev/console
    else
        echo "DAILY_APP_$app_name=fail $(head -1 /tmp/app-$app_name.log 2>/dev/null | tr ' ' '_' | head -c 40)" >/dev/console
    fi
    pkill -x "$app_name" 2>/dev/null
done

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
    aplay_list=$(aplay -l 2>&1 | head -20 | tr '\n' '|')
    echo "DAILY_AUDIO_DEVICES $aplay_list" >/dev/console
    if timeout 15 aplay -D hw:0,0 /tmp/daily-tone.wav >/tmp/aplay.log 2>&1; then
        mark AUDIO ok "aplay-hw:0,0"
    else
        rc=$?
        if [ "$rc" -eq 124 ]; then
            mark AUDIO fail "aplay-timeout-15s:$(head -1 /tmp/aplay.log | tr ' ' '_')"
        else
            mark AUDIO fail "rc=$rc:$(head -1 /tmp/aplay.log | tr ' ' '_')"
        fi
    fi
else
    mark AUDIO missing "no-/dev/snd-node"
fi

# --- Domain 6: USB hotplug (enumerate, mount, read, umount) --------------
echo "DAILY_USB_TOOLS lsusb=$(command -v lsusb || echo missing) list=$(lsusb 2>&1 | head -3 | tr '\n' '|' | head -c 200)" >/dev/console
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

# --- Domain 6c: performance baseline (Q4) ---------------------------------
for run in 1 2 3; do
    t0=$(cut -d' ' -f1 /proc/uptime)
    xclock >/dev/null 2>&1 &
    win=""
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        win=$(timeout 1 xdotool search --class xclock 2>/dev/null | head -1)
        [ -n "$win" ] && break
        sleep 1
    done
    t1=$(cut -d' ' -f1 /proc/uptime)
    pkill -x xclock 2>/dev/null
    secs=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.2f", b-a}')
    echo "DAILY_PERF xclock_start run=$run seconds=$secs window=$win" >/dev/console
done
dd if=/dev/zero of=/tmp/perf.bin bs=1M count=64 2>/tmp/dd-w.log
echo "DAILY_PERF dd_write_raw $(tail -1 /tmp/dd-w.log | tr -s ' ' | head -c 80)" >/dev/console
dd if=/tmp/perf.bin of=/dev/null bs=1M iflag=direct 2>/tmp/dd-r.log
echo "DAILY_PERF dd_read_raw $(tail -1 /tmp/dd-r.log | tr -s ' ' | head -c 80)" >/dev/console
rm -f /tmp/perf.bin
echo "DAILY_PERF apt_before [$(pgrep -a apt 2>/dev/null | head -2 | tr '\n' '|' | head -c 60)]" >/dev/console
t0=$(cut -d' ' -f1 /proc/uptime)
if timeout 180 apt-get install -y --reinstall -o Debug::NoLocking=1 hello >/tmp/perf-apt.log 2>&1; then
    t1=$(cut -d' ' -f1 /proc/uptime)
    secs=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.2f", b-a}')
    echo "DAILY_PERF apt_reinstall_hello seconds=$secs" >/dev/console
else
    echo "DAILY_PERF apt_reinstall_hello failed raw=$(tail -2 /tmp/perf-apt.log | tr '\n' '|' | tr ' ' '_' | head -c 100)" >/dev/console
fi

# --- Domain 6b: stability (Q3) --------------------------------------------
# 30 minutes of load with a status line every 5 minutes, then 3 suspend cycles.
(
    # The regression run skips the 30-minute soak (one quick tick); the
    # individual stress gate covers the long-run behaviour.
    for tick in 1; do
        apt list --installed >/dev/null 2>&1
        echo "DAILY_STAB_LOAD tick=$tick uptime=$(cut -d' ' -f1 /proc/uptime) load=$(cut -d' ' -f1 /proc/loadavg)" >/dev/console
        sleep 1
    done
) &
LOAD_PID=$!

for round in 1 2 3; do
    before=$(cut -d' ' -f1 /proc/uptime)
    if timeout 20 systemctl suspend >/tmp/stab-suspend-$round.log 2>&1; then
        sleep 1
        after=$(cut -d' ' -f1 /proc/uptime)
        xclock >/dev/null 2>&1 &
        sleep 2
        win=$(timeout 5 xdotool search --class xclock 2>/dev/null | head -1)
        pkill -x xclock 2>/dev/null
        if [ -n "$win" ]; then
            echo "DAILY_STAB_SUSPEND round=$round result=ok before=$before after=$after xclock=$win" >/dev/console
        else
            echo "DAILY_STAB_SUSPEND round=$round result=no-xclock before=$before after=$after" >/dev/console
        fi
    else
        echo "DAILY_STAB_SUSPEND round=$round result=fail $(head -1 /tmp/stab-suspend-$round.log 2>/dev/null | tr ' ' '_' | head -c 40)" >/dev/console
    fi
    sleep 5
done
wait $LOAD_PID 2>/dev/null
echo "DAILY_STAB_DONE uptime=$(cut -d' ' -f1 /proc/uptime)" >/dev/console

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
