#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
W=/var/tmp/im4; rm -rf "$W"; mkdir -p "$W"
echo "IM5 devpts_mount=[$(mount 2>/dev/null | grep -i pts | tr '\n' '|')]"
echo "IM5 devpts_ls=[$(ls /dev/pts 2>&1 | tr '\n' ',')]"
ls -l /dev/ptmx /dev/pts 2>&1 | sed 's/^/IM5 ls /'
python3 - <<'PY' 2>&1 | sed 's/^/IM5 /'
import os, fcntl, termios, errno
try:
    m, s = os.openpty()
    print("openpty ok master=%d slave=%d name=%s" % (m, s, os.ttyname(s)))
    os.write(m, b"hi\n")
    print("read:", os.read(s, 16))
except OSError as e:
    print("openpty errno=%d %s" % (e.errno, errno.errorcode.get(e.errno,"?")))
try:
    fd = os.open("/dev/ptmx", os.O_RDWR | os.O_NOCTTY)
    n = fcntl.ioctl(fd, termios.TIOCGPTN, b"\0\0\0\0")
    num = int.from_bytes(n, "little")
    print("TIOCGPTN ok num=%d" % num)
    print("TIOCSPTLCK rc=", fcntl.ioctl(fd, termios.TIOCSPTLCK, b"\0\0\0\0"))
    path = "/dev/pts/%d" % num
    print("slave exists:", os.path.exists(path))
    s2 = os.open(path, os.O_RDWR | os.O_NOCTTY)
    print("slave open ok fd=%d" % s2)
except OSError as e:
    print("ptmx path errno=%d %s" % (e.errno, errno.errorcode.get(e.errno,"?")))
PY
command -v script >/dev/null && { script -qc 'echo SCRIPT_PTY_OK' /dev/null 2>&1 | tr -d '\r' | sed 's/^/IM5 /'; }
mkdir -p /tmp/.X11-unix
env MESA_LOADER_DRIVER_OVERRIDE=kms_swrast LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe \
    /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp -config /etc/X11/xorg-noudev.conf >"$W/xorg.log" 2>&1 &
waited=0; while [ ! -S /tmp/.X11-unix/X0 ] && [ $waited -lt 40 ]; do sleep 1; waited=$((waited+1)); done
export DISPLAY=:0
echo "IM5 xorg=$([ -S /tmp/.X11-unix/X0 ] && echo yes || echo no)"
xterm -title ptytest -e sleep 60 >"$W/x.out" 2>"$W/x.err" &
sleep 3
win=$(timeout 5 xdotool search --name ptytest 2>/dev/null | head -1)
echo "IM5 xterm win=$win map=$(timeout 5 xwininfo -id "$win" 2>/dev/null | grep -m1 'Map State')"
echo "IM5 xterm_err=$(head -3 "$W/x.err" | tr '\n' '|' | head -c 200)"
echo "IM5 pts_after=[$(ls /dev/pts 2>&1 | tr '\n' ',')]"
xterm -version 2>&1 | sed 's/^/IM5 xtermver /'
echo "IM5 done"
echo "DAILY_IM5=ok"; echo "DAILY_DONE=ok"
