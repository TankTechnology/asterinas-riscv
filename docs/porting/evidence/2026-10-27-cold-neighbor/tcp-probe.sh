#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
echo "CT arp_before: $(cat /proc/net/arp 2>&1 | tr '\n' '|')"
python3 -u - <<'PY' 2>&1 | sed 's/^/CT py: /'
import socket, time
def arp():
    try:
        return [l.split()[0] for l in open('/proc/net/arp').read().strip().splitlines()[1:]]
    except Exception as e:
        return ['err:%r' % e]
print('arp_at_start', arp())
for dst in (('10.0.2.2', 80), ('10.0.2.3', 80)):
    t = time.time()
    try:
        c = socket.create_connection(dst, timeout=8)
        print('tcp %s ok %.3fs' % (dst[0], time.time()-t)); c.close()
    except Exception as e:
        print('tcp %s err %.3fs %r' % (dst[0], time.time()-t, e))
    print('arp_after_%s' % dst[0], arp())
PY
echo "CT arp_end: $(cat /proc/net/arp 2>&1 | tr '\n' '|')"
echo "DAILY_CT=ok"; echo "DAILY_DONE=ok"
