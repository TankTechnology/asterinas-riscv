#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
W=/var/tmp/im3; rm -rf "$W"; mkdir -p "$W"; mkdir -p /tmp/.X11-unix
env MESA_LOADER_DRIVER_OVERRIDE=kms_swrast LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe \
    /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp -config /etc/X11/xorg-noudev.conf >"$W/xorg.log" 2>&1 &
waited=0; while [ ! -S /tmp/.X11-unix/X0 ] && [ $waited -lt 40 ]; do sleep 1; waited=$((waited+1)); done
export DISPLAY=:0
echo "IM4 xorg=$([ -S /tmp/.X11-unix/X0 ] && echo yes || echo no)"
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    DBUS_SESSION_BUS_ADDRESS=$(dbus-daemon --session --fork --print-address); export DBUS_SESSION_BUS_ADDRESS
fi
mkdir -p /root/.config/fcitx5
printf '[Groups/0]\nName=Default\nDefault Layout=us\nDefaultIM=wbpy\n\n[Groups/0/Items/0]\nName=wbpy\nLayout=\n\n[GroupOrder]\n0=Default\n' >/root/.config/fcitx5/profile
export GTK_IM_MODULE=fcitx QT_IM_MODULE=fcitx XMODIFIERS=@im=fcitx
pkill -x fcitx5 2>/dev/null; sleep 1
fcitx5 -d --replace >"$W/fcitx.log" 2>&1
sleep 5
echo "IM4 fcitx_alive=$(pgrep -c -x fcitx5) engine=[$(fcitx5-remote -n 2>&1)] status=[$(fcitx5-remote 2>&1)]"
echo "IM4 remote_o=$(fcitx5-remote -o 2>&1; echo rc=$?) after=[$(fcitx5-remote -n 2>&1)] status=[$(fcitx5-remote 2>&1)]"
echo "IM4 dbus_im=$(timeout 5 dbus-send --session --print-reply --dest=org.fcitx.Fcitx5 /controller org.fcitx.Fcitx.Controller1.CurrentInputMethod 2>&1 | tr '\n' ' ' | head -c 200)"
echo "IM4 ims=$(timeout 5 dbus-send --session --print-reply --dest=org.fcitx.Fcitx5 /controller org.fcitx.Fcitx.Controller1.AvailableInputMethods 2>&1 | head -c 300 | tr '\n' ' ')"
echo "IM4 fcitx_log=$(tail -6 "$W/fcitx.log" | tr '\n' '|' | head -c 300)"
openbox >"$W/wm.log" 2>&1 & sleep 2
xterm -xrm "*allowSendEvents: true" -title imtest -e sh -c 'cat > /tmp/im-out' >"$W/xterm.out" 2>"$W/xterm.err" &
sleep 3
win=$(timeout 5 xdotool search --name imtest 2>/dev/null | head -1)
echo "IM4 win=$win map=$(timeout 5 xwininfo -id "$win" 2>/dev/null | grep -m1 'Map State') geo=$(timeout 5 xdotool getwindowgeometry "$win" 2>&1 | tr '\n' '|' | head -c 120)"
echo "IM4 xterm_err=$(head -c 300 "$W/xterm.err" | tr '\n' '|')"
echo "IM4 xterm_out=$(head -c 200 "$W/xterm.out" | tr '\n' '|')"
echo "IM4 onlyvisible=$(timeout 5 xdotool search --onlyvisible --name imtest 2>/dev/null | head -1)"
echo "IM4 xtree=$(timeout 5 xwininfo -root -tree 2>/dev/null | grep -i -m3 'imtest\|xterm' | tr '\n' '|' | head -c 200)"
echo "IM4 xtest=$(xdpyinfo 2>/dev/null | grep -ci xtest) xkb=$(setxkbmap -query 2>&1 | tr '\n' '|' | head -c 100)"
xterm -title imtest2 -e sleep 60 >"$W/xterm2.out" 2>"$W/xterm2.err" & sleep 3
w2=$(timeout 5 xdotool search --name imtest2 2>/dev/null | head -1)
echo "IM4 win2=$w2 map=$(timeout 5 xwininfo -id "$w2" 2>/dev/null | grep -m1 'Map State') err=$(head -c 200 "$W/xterm2.err" | tr '\n' '|')"
echo "IM4 done"
echo "DAILY_IM4=ok"; echo "DAILY_DONE=ok"
