#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
echo "CB arp_before: $(cat /proc/net/arp 2>&1 | tr '\n' '|')"
python3 -u - <<'PY' 2>&1 | sed 's/^/CB py: /'
import socket, struct, time
def q(i):
    b = struct.pack('>HHHHHH', 0x8000 + i, 0x0100, 1, 0, 0, 0)
    return b + b'\x07debian\x03org\x00' + struct.pack('>HH', 1, 1)
def arp():
    try:
        return [l.split()[0] for l in open('/proc/net/arp').read().strip().splitlines()[1:]]
    except Exception as e:
        return ['err:%r' % e]
print('arp_at_start', arp())
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
t = time.time()
for i in range(5):
    s.sendto(q(i), ('10.0.2.3', 53))
got = 0
deadline = time.time() + 3
while time.time() < deadline:
    s.settimeout(max(0.001, deadline - time.time()))
    try:
        d, _ = s.recvfrom(1024); got += 1
    except Exception:
        break
print('burst sent=5 replies=%d lost=%d elapsed=%.3fs' % (got, 5-got, time.time()-t))
print('arp_after_burst', arp())
t = time.time()
s.sendto(q(9), ('10.0.2.3', 53))
try:
    d, _ = s.recvfrom(1024); print('after_burst_single ok %.3fs' % (time.time()-t))
except Exception as e:
    print('after_burst_single fail %.3fs %r' % (time.time()-t, e))
PY
echo "CB arp_end: $(cat /proc/net/arp 2>&1 | tr '\n' '|')"
echo "DAILY_CB=ok"; echo "DAILY_DONE=ok"
