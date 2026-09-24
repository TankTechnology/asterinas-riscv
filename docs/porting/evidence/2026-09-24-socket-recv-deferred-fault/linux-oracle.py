# SPDX-License-Identifier: MPL-2.0

"""Probe Linux receives with one writable user page and an inaccessible next page."""

import ctypes
import errno
import mmap
import socket

capacity = 8192
valid_prefix = 4096
buffer = mmap.mmap(-1, capacity, prot=mmap.PROT_READ | mmap.PROT_WRITE)
address = ctypes.addressof(ctypes.c_char.from_buffer(buffer))
libc = ctypes.CDLL(None, use_errno=True)
libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
libc.mprotect.restype = ctypes.c_int
assert libc.mprotect(address + valid_prefix, valid_prefix, 0) == 0
libc.recvfrom.argtypes = [
    ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p,
]
libc.recvfrom.restype = ctypes.c_ssize_t
libc.read.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
libc.read.restype = ctypes.c_ssize_t

udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.bind(("127.0.0.1", 0))
udp_sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen(1)
tcp_sender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
tcp_sender.connect(listener.getsockname())
tcp, _ = listener.accept()

for kind, receiver, send in (
    ("udp", udp, lambda data: udp_sender.sendto(data, udp.getsockname())),
    ("tcp", tcp, tcp_sender.sendall),
):
    for operation in ("recvfrom", "recvmsg", "read"):
        send(b"q")
        ctypes.set_errno(0)
        if operation == "recvfrom":
            received = libc.recvfrom(receiver.fileno(), address, capacity, 0, None, None)
        elif operation == "recvmsg":
            received = receiver.recvmsg_into([buffer])[0]
        else:
            received = libc.read(receiver.fileno(), address, capacity)
        assert received == 1 and buffer[0] == ord("q")
        print(kind, operation, received)

udp_sender.sendto(b"x" * 5000, udp.getsockname())
ctypes.set_errno(0)
result = libc.recvfrom(udp.fileno(), address, capacity, 0, None, None)
assert result == -1 and ctypes.get_errno() == errno.EFAULT
print("udp long", result, ctypes.get_errno())

for item in (tcp, tcp_sender, listener, udp, udp_sender):
    item.close()
assert libc.mprotect(
    address + valid_prefix, valid_prefix, mmap.PROT_READ | mmap.PROT_WRITE
) == 0
buffer.close()
