#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
echo "CA arp_before: $(cat /proc/net/arp 2>&1 | tr '\n' '|')"
python3 -u - <<'PY' 2>&1 | sed 's/^/CA py: /'
import socket, struct, time
def q(i):
    b = struct.pack('>HHHHHH', 0x9000 + i, 0x0100, 1, 0, 0, 0)
    return b + b'\x07debian\x03org\x00' + struct.pack('>HH', 1, 1)
def arp():
    try:
        return [l.split()[0] for l in open('/proc/net/arp').read().strip().splitlines()[1:]]
    except Exception:
        return []
print('arp_at_start', arp())
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
t = time.time()
s.sendto(q(0), ('10.0.2.3', 53))
seen = None
for _ in range(3000):
    if '10.0.2.3' in arp():
        seen = time.time() - t
        break
    time.sleep(0.001)
print('cold_arp_resolved %.4fs' % (seen if seen is not None else -1))
s.setblocking(False)
try:
    d, _ = s.recv(1024)
    print('first_packet_reply bytes=%d after %.4fs' % (len(d), time.time() - t))
except Exception as e:
    print('first_packet_reply none errno=%r after %.4fs' % (e, time.time() - t))
s.close()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
t = time.time(); s.sendto(q(1), ('10.0.2.3', 53))
try:
    d, _ = s.recvfrom(1024); print('second_packet_reply %.4fs' % (time.time() - t))
except Exception as e:
    print('second_packet_reply fail %r' % (e,))
print('arp_end', arp())
PY
echo "DAILY_CA=ok"; echo "DAILY_DONE=ok"
