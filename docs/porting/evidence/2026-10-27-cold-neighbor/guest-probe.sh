#!/bin/sh
# Cold-neighbour diagnosis (v2): no sudo, every netlink call has a timeout,
# Python runs unbuffered so the serial log shows each phase as it happens.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
W=/var/tmp/cn
rm -rf "$W"
mkdir -p "$W"

echo "CN arp_before: $(cat /proc/net/arp 2>&1 | tr '\n' '|')"
echo "CN whoami=$(id -u) sudo=$(command -v sudo || echo none)"
timeout 5 ip neigh flush dev eth0 >/dev/null 2>&1
echo "CN flush1_rc=$?"
sleep 1
echo "CN arp_after_flush1: $(cat /proc/net/arp 2>&1 | tr '\n' '|')"

python3 -u - <<'EOF' 2>&1 | sed 's/^/CN py: /'
import os, socket, struct, time

def q(i=0):
    b = struct.pack('>HHHHHH', 0x7000 + i, 0x0100, 1, 0, 0, 0)
    return b + b'\x07debian\x03org\x00' + struct.pack('>HH', 1, 1)

def arp_ips():
    try:
        lines = open('/proc/net/arp').read().strip().splitlines()[1:]
        return [l.split()[0] for l in lines]
    except Exception as e:
        return ['err:%r' % e]

def flush():
    rc = os.system('timeout 5 ip neigh flush dev eth0 >/dev/null 2>&1')
    time.sleep(0.3)
    return rc

def one(label, dst=('10.0.2.3', 53), timeout=3.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    t = time.time()
    try:
        s.sendto(q(), dst)
    except Exception as e:
        print('%s send_error=%r' % (label, e)); s.close(); return
    try:
        d, _ = s.recvfrom(1024)
        print('%s ok %.3fs bytes=%d' % (label, time.time() - t, len(d)))
    except Exception as e:
        print('%s fail %.3fs errno=%r' % (label, time.time() - t, e))
    s.close()

print('arp_at_start', arp_ips())
one('single1_cold')
print('arp_after_single1', arp_ips())
one('single2_warm')

print('flush2_rc', flush(), 'arp_after_flush2', arp_ips())
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
t = time.time(); s.sendto(q(1), ('10.0.2.3', 53))
seen = None
for _ in range(50):
    time.sleep(0.01)
    if '10.0.2.3' in arp_ips():
        seen = time.time() - t
        break
print('arp_entry_seen %.3fs' % (seen if seen is not None else -1))
try:
    d, _ = s.recvfrom(1024)
    print('flush_phase_reply ok %.3fs' % (time.time() - t))
except Exception as e:
    print('flush_phase_reply fail %.3fs %r' % (time.time() - t, e))
s.close()

print('flush3_rc', flush(), 'arp_now', arp_ips())
st = time.time()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for i in range(5):
    s.sendto(q(10 + i), ('10.0.2.3', 53))
got = 0
deadline = time.time() + 3
while time.time() < deadline:
    s.settimeout(max(0.01, deadline - time.time()))
    try:
        d, _ = s.recvfrom(1024)
        got += 1
    except Exception:
        break
print('burst sent=5 replies=%d lost=%d in %.3fs' % (got, 5 - got, time.time() - st))
s.close()

print('flush4_rc', flush(), 'arp_now', arp_ips())
t = time.time()
try:
    c = socket.create_connection(('10.0.2.3', 80), timeout=6)
    print('tcp_connect ok %.3fs' % (time.time() - t))
    c.close()
except Exception as e:
    print('tcp_connect err %.3fs %r' % (time.time() - t, e))

print('flush5_rc', flush(), 'arp_now', arp_ips())
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
s.connect(('10.0.2.3', 53))
t = time.time()
s.send(q(20))
try:
    d = s.recv(1024)
    print('connected_udp ok %.3fs' % (time.time() - t))
except Exception as e:
    print('connected_udp fail %.3fs %r' % (time.time() - t, e))
s.close()

print('arp_end', arp_ips())
EOF

echo "CN arp_after: $(cat /proc/net/arp 2>&1 | tr '\n' '|')"
echo "DAILY_CN=ok cold-neighbor"
echo "DAILY_DONE=ok"
