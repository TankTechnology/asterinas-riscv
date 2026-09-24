# SPDX-License-Identifier: MPL-2.0

"""Check Linux TCP receives into a buffer with an inaccessible tail."""

import ctypes
import mmap
import socket

capacity = 10 * 1024 * 1024
writable = 128 * 1024
buffer = mmap.mmap(-1, capacity, prot=mmap.PROT_READ | mmap.PROT_WRITE)
address = ctypes.addressof(ctypes.c_char.from_buffer(buffer))
libc = ctypes.CDLL(None, use_errno=True)
libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
libc.mprotect.restype = ctypes.c_int
assert libc.mprotect(address + writable, capacity - writable, 0) == 0

listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen(1)
client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client.connect(listener.getsockname())
server, _ = listener.accept()

libc.recvfrom.argtypes = [
    ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p,
]
libc.recvfrom.restype = ctypes.c_ssize_t
libc.read.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
libc.read.restype = ctypes.c_ssize_t
for operation in ("recvfrom", "recvmsg", "read"):
    client.sendall(b"q")
    if operation == "recvfrom":
        received = libc.recvfrom(server.fileno(), address, capacity, 0, None, None)
    elif operation == "recvmsg":
        received = server.recvmsg_into([buffer])[0]
    else:
        received = libc.read(server.fileno(), address, capacity)
    assert received == 1 and buffer[0] == ord("q"), (
        operation, received, ctypes.get_errno()
    )
    print(operation, received)

server.close()
client.close()
listener.close()
assert libc.mprotect(
    address + writable, capacity - writable, mmap.PROT_READ | mmap.PROT_WRITE
) == 0
buffer.close()
