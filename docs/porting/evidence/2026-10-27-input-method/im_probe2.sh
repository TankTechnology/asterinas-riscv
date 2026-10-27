#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
W=/var/tmp/im2
rm -rf "$W"; mkdir -p "$W"
mkdir -p /tmp/.X11-unix
env MESA_LOADER_DRIVER_OVERRIDE=kms_swrast LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe \
    /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp -config /etc/X11/xorg-noudev.conf >"$W/xorg.log" 2>&1 &
waited=0
while [ ! -S /tmp/.X11-unix/X0 ] && [ $waited -lt 40 ]; do sleep 1; waited=$((waited+1)); done
export DISPLAY=:0
echo "IM3 xorg_ready=$([ -S /tmp/.X11-unix/X0 ] && echo yes || echo no)"
echo "IM3 fcitx_help=[$(timeout 5 fcitx5 --help 2>&1 | tr '\n' '|' | head -c 300)]"
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    DBUS_SESSION_BUS_ADDRESS=$(dbus-daemon --session --fork --print-address); export DBUS_SESSION_BUS_ADDRESS
fi
echo "IM3 dbus=[$DBUS_SESSION_BUS_ADDRESS]"
mkdir -p /root/.config/fcitx5
printf '[Groups/0]\nName=Default\nDefault Layout=us\nDefaultIM=wbpy\n\n[Groups/0/Items/0]\nName=wbpy\nLayout=\n\n[GroupOrder]\n0=Default\n' >/root/.config/fcitx5/profile
export GTK_IM_MODULE=fcitx QT_IM_MODULE=fcitx XMODIFIERS=@im=fcitx
echo "IM3 fg_start"
timeout 6 fcitx5 -r -v >"$W/fg.log" 2>&1
echo "IM3 fg_rc=$? fg_alive=$(pgrep -c -x fcitx5) fg_log=$(head -c 500 "$W/fg.log" | tr '\n' '|')"
pkill -x fcitx5 2>/dev/null; sleep 1
fcitx5 -d >"$W/d.log" 2>&1
sleep 4
echo "IM3 d_start rc=$? alive=$(pgrep -c -x fcitx5) dlog=$(head -c 300 "$W/d.log" | tr '\n' '|')"
echo "IM3 remote engine=[$(fcitx5-remote -n 2>&1)] status=[$(fcitx5-remote 2>&1)]"
echo "IM3 xim=$(xprop -root XIM_SERVERS 2>&1 | head -1)"
openbox >"$W/wm.log" 2>&1 & sleep 2
# --- xev: does ANY synthetic key reach a client? ---
xev -name imxev -geometry 300x200 >"$W/xev.log" 2>&1 &
sleep 2
ev=$(timeout 5 xdotool search --name imxev 2>/dev/null | head -1)
echo "IM3 xev_win=$ev"
timeout 5 xdotool windowactivate --sync "$ev" 2>/dev/null; echo "IM3 xev_activate_rc=$?"
sleep 1
echo "IM3 xev_focus=$(timeout 5 xdotool getwindowfocus 2>/dev/null) active=$(timeout 5 xdotool getactivewindow 2>/dev/null)"
timeout 5 xdotool key a 2>"$W/xdo1.err"; echo "IM3 xev_xtest_rc=$? err=$(head -1 "$W/xdo1.err")"
sleep 1
timeout 5 xdotool key --window "$ev" b 2>"$W/xdo2.err"; echo "IM3 xev_send_rc=$? err=$(head -1 "$W/xdo2.err")"
sleep 1
echo "IM3 xev_keypresses=$(grep -c KeyPress "$W/xev.log") tail=$(grep -a -m2 'KeyPress' "$W/xev.log" | tr '\n' '|' | head -c 200)"
# --- xterm with Return so cat flushes ---
xterm -xrm "*allowSendEvents: true" -title imtest -e sh -c 'cat > /tmp/im-out' >/dev/null 2>&1 &
sleep 2
win=$(timeout 5 xdotool search --name imtest 2>/dev/null | head -1)
echo "IM3 xterm_win=$win map=$(timeout 5 xwininfo -id "$win" 2>/dev/null | grep -m1 'Map State')"
timeout 5 xdotool windowactivate --sync "$win" 2>/dev/null; echo "IM3 win_activate_rc=$?"
timeout 5 xdotool windowfocus "$win" 2>/dev/null; echo "IM3 win_focus_rc=$?"
sleep 1
timeout 5 xdotool key --window "$win" a 2>/dev/null; timeout 5 xdotool key --window "$win" b 2>/dev/null
timeout 5 xdotool key --window "$win" Return 2>/dev/null; sleep 1
echo "IM3 send_ascii=[$(cat -v /tmp/im-out 2>/dev/null | head -c 40)]"
timeout 5 xdotool type --delay 100 "cd" 2>/dev/null; timeout 5 xdotool key Return 2>/dev/null; sleep 1
echo "IM3 xtest_ascii=[$(cat -v /tmp/im-out 2>/dev/null | head -c 40)]"
echo "IM3 done"
echo "DAILY_IM3=ok"; echo "DAILY_DONE=ok"
