#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games
export HOME=/root
W=/var/tmp/im
rm -rf "$W"; mkdir -p "$W"
mkdir -p /tmp/.X11-unix
env MESA_LOADER_DRIVER_OVERRIDE=kms_swrast LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe \
    /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp -config /etc/X11/xorg-noudev.conf >"$W/xorg.log" 2>&1 &
waited=0
while [ ! -S /tmp/.X11-unix/X0 ] && [ $waited -lt 40 ]; do sleep 1; waited=$((waited+1)); done
export DISPLAY=:0
echo "IM2 xorg_ready=$([ -S /tmp/.X11-unix/X0 ] && echo yes || echo no) waited=$waited"
need=""
for t in fcitx5 fcitx5-remote openbox xterm xdotool xprop xev xdpyinfo dbus-daemon; do
    command -v "$t" >/dev/null 2>&1 || need="$need $t"
done
echo "IM2 missing=[$need]"
if [ -n "$need" ]; then
    timeout 240 apt-get update >"$W/apt.log" 2>&1
    timeout 420 apt-get install -y --no-install-recommends fcitx5 fcitx5-modules fcitx5-data fcitx5-table-extra fcitx5-chinese-addons dbus-daemon openbox xterm xdotool x11-utils x11-apps >>"$W/apt.log" 2>&1
fi
for t in fcitx5 fcitx5-remote openbox xterm xdotool xprop xev; do printf 'IM2 bin %s=%s\n' "$t" "$(command -v $t 2>/dev/null || echo NA)"; done
echo "IM2 locale LANG=[$LANG] LC_ALL=[$LC_ALL] LC_CTYPE=[$LC_CTYPE]"
echo "IM2 locale_avail=[$(locale -a 2>/dev/null | grep -iE 'zh_CN|en_US|C.utf8' | tr '\n' ',')]"
echo "IM2 im_config=$(command -v im-config >/dev/null && im-config -m 2>&1 | tr '\n' '|' | head -c 100 || echo NA)"
export GTK_IM_MODULE=fcitx QT_IM_MODULE=fcitx XMODIFIERS=@im=fcitx
echo "IM2 env GTK=[$GTK_IM_MODULE] QT=[$QT_IM_MODULE] XMOD=[$XMODIFIERS]"
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    DBUS_SESSION_BUS_ADDRESS=$(dbus-daemon --session --fork --print-address)
    export DBUS_SESSION_BUS_ADDRESS
fi
echo "IM2 dbus=[$DBUS_SESSION_BUS_ADDRESS]"
mkdir -p /root/.config/fcitx5
im=wbpy
printf '[Groups/0]\nName=Default\nDefault Layout=us\nDefaultIM=%s\n\n[Groups/0/Items/0]\nName=%s\nLayout=\n\n[GroupOrder]\n0=Default\n' "$im" "$im" >/root/.config/fcitx5/profile
pkill -x fcitx5 2>/dev/null; sleep 1
fcitx5 -d --replace -v >"$W/fcitx.log" 2>&1 &
sleep 5
echo "IM2 fcitx alive=$(pgrep -c -x fcitx5) engine=[$(fcitx5-remote -n 2>&1)] status_rc=$(fcitx5-remote 2>&1; echo rc=$?)"
echo "IM2 fcitx_log=$(tail -8 "$W/fcitx.log" | tr '\n' '|' | head -c 400)"
echo "IM2 fcitx_engines=$(fcitx5-remote -l 2>&1 | tr '\n' ',' | head -c 200)"
echo "IM2 xim_servers=$(xprop -root XIM_SERVERS 2>&1 | head -1)"
echo "IM2 profile=$(tr '\n' '|' </root/.config/fcitx5/profile | head -c 200)"
openbox >"$W/wm.log" 2>&1 &
sleep 3
echo "IM2 wm alive=$(pgrep -c -x openbox) log=$(head -2 "$W/wm.log" | tr '\n' '|' | head -c 120)"
xterm -xrm "*allowSendEvents: true" -title imtest -e sh -c 'cat > /tmp/im-out' >/dev/null 2>&1 &
sleep 2
win=$(timeout 5 xdotool search --name imtest 2>/dev/null | head -1)
echo "IM2 xterm_win=$win"
timeout 5 xdotool windowfocus "$win" 2>/dev/null; echo "IM2 windowfocus_rc=$?"
timeout 5 xdotool windowactivate --sync "$win" 2>/dev/null; echo "IM2 windowactivate_rc=$?"
sleep 1
echo "IM2 focus ids active=[$(timeout 5 xdotool getactivewindow 2>/dev/null)] focus=[$(timeout 5 xdotool getwindowfocus 2>/dev/null)] netactive=[$(timeout 5 xprop -root _NET_ACTIVE_WINDOW 2>/dev/null | head -1)]"
timeout 5 xdotool mousemove --window "$win" 20 20 2>/dev/null; sleep 1
timeout 5 xdotool type --delay 120 "abc" 2>/dev/null; sleep 1
echo "IM2 xtest_ascii=[$(cat /tmp/im-out 2>/dev/null | tr -d '\n')]"
timeout 5 xdotool type --window "$win" --delay 120 "xyz" 2>/dev/null; sleep 1
echo "IM2 sendevent_ascii=[$(cat /tmp/im-out 2>/dev/null | tr -d '\n')]"
echo "IM2 keycodes=$(xmodmap -pk 2>/dev/null | grep -c 'a.*a' )"
echo "IM2 fcitx_after_focus engine=[$(fcitx5-remote -n 2>&1)] status=[$(fcitx5-remote 2>&1)]"
# wubi: 你 = wqiy, 好 = vbg ; try through focus path and via XSendEvent
timeout 5 xdotool type --delay 150 "wqiy" 2>/dev/null; timeout 5 xdotool key space 2>/dev/null
timeout 5 xdotool type --delay 150 "vbg" 2>/dev/null; timeout 5 xdotool key space 2>/dev/null
timeout 5 xdotool key Return 2>/dev/null; sleep 1
echo "IM2 wubi_focus_out=[$(cat /tmp/im-out 2>/dev/null | tr -d '\n' | head -c 40)]"
timeout 5 xdotool type --window "$win" --delay 150 "wqiy" 2>/dev/null; timeout 5 xdotool key --window "$win" space 2>/dev/null
timeout 5 xdotool type --window "$win" --delay 150 "vbg" 2>/dev/null; timeout 5 xdotool key --window "$win" space 2>/dev/null
timeout 5 xdotool key --window "$win" Return 2>/dev/null; sleep 1
echo "IM2 wubi_send_out=[$(cat /tmp/im-out 2>/dev/null | tr -d '\n' | head -c 40)]"
echo "IM2 done"
echo "DAILY_IM2=ok"; echo "DAILY_DONE=ok"
