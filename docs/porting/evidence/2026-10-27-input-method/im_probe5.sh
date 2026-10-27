#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
W=/var/tmp/im5; rm -rf "$W"; mkdir -p "$W"
python3 - <<'PY' 2>&1 | sed 's/^/IM6 /'
import os, fcntl, termios, errno, sys
TIOCSCTTY = 0x540E
m, s = os.openpty()
print("openpty ok name=%s" % os.ttyname(s))
pid = os.fork()
if pid == 0:
    try:
        os.setsid()
        print("child setsid ok")
    except OSError as e:
        print("child setsid errno=%d %s" % (e.errno, errno.errorcode.get(e.errno,"?")))
    try:
        fcntl.ioctl(s, TIOCSCTTY, 0)
        print("child TIOCSCTTY ok")
    except OSError as e:
        print("child TIOCSCTTY errno=%d %s" % (e.errno, errno.errorcode.get(e.errno,"?")))
    os._exit(0)
os.waitpid(pid, 0)
PY
echo "IM6 done"
echo "DAILY_IM6=ok"; echo "DAILY_DONE=ok"
