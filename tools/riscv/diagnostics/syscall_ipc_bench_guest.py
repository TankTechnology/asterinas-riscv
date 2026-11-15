# SPDX-License-Identifier: MPL-2.0
# Status: manual -- guest-side syscall/IPC benchmark pushed by megrez_desktop_baseline.py. See tools/riscv/LAYOUT.md.
"""Measure syscall and IPC cost on the Megrez guest, self-timed.

Each family is timed in the guest so the only thing crossing the serial line is
the result. A control loop with no syscall is included: without it a per-call
cost of a few microseconds cannot be separated from the Python loop itself,
which matters because the interesting range here is single-digit microseconds.

Run as: python3 /tmp/w1_guest.py
"""

import os
import socket
import time


def bench(name, func, count):
    start = time.time()
    for _ in range(count):
        func()
    print('M_%s_us' % name, round((time.time() - start) / count * 1e6, 2))


nop = len
bench('ctrl', lambda: nop(()), 3000)
bench('getpid', os.getpid, 3000)
bench('getuid', os.getuid, 3000)
bench('clock', lambda: time.clock_gettime(time.CLOCK_MONOTONIC), 3000)

fd = os.open('/dev/null', os.O_WRONLY)
bench('write', lambda: os.write(fd, b'x'), 2000)

read_fd, write_fd = os.pipe()
bench('pipe', lambda: (os.write(write_fd, b'x'), os.read(read_fd, 1)), 2000)

a, b = socket.socketpair()
bench('sockpair', lambda: (a.send(b'x'), b.recv(1)), 2000)

# A round trip through a second process: two task switches per iteration on top
# of the socket work, which is what a desktop toolkit actually pays.
a, b = socket.socketpair()
pid = os.fork()
if pid == 0:
    while True:
        data = a.recv(1)
        if not data:
            break
        a.send(data)
    os._exit(0)
start = time.time()
for _ in range(1000):
    b.send(b'x')
    b.recv(1)
print('M_sockrtt_us', round((time.time() - start) / 1000 * 1e6, 1))
os.kill(pid, 9)
os.waitpid(pid, 0)
print('ZZBENCHDONE')
