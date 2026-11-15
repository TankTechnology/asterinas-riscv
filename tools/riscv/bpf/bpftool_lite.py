#!/usr/bin/env python3
"""A tiny, dependency-free loader for Asterinas's eBPF subset.

The kernel side lives in kernel/src/bpf: an interpreter, a conservative
verifier, array and hash maps, and attachment to the kernel's own trace
events (`/proc/trace/events`). This tool is the userspace half -- a
"bpftool lite" that speaks `bpf(2)` directly, so it works on a board with no
BPF headers, no libbpf and no clang.

    bpftool_lite.py count --event syscall:sys_enter --seconds 5
    bpftool_lite.py count --event block:block_rq_issue --seconds 5
    bpftool_lite.py verifier
    bpftool_lite.py load --dump

Everything is root-only on the kernel side; run it as root.

The kernel's context for a trace event is 24 bytes:

    0   u64 timestamp_ns
    8   u32 pid (tgid)
    12  u32 tid
    16  u32 event id
    20  u32 cpu
"""

import argparse
import ctypes
import errno
import os
import struct
import sys
import time

SYS_BPF = 280

BPF_MAP_CREATE = 0
BPF_MAP_LOOKUP_ELEM = 1
BPF_MAP_UPDATE_ELEM = 2
BPF_MAP_DELETE_ELEM = 3
BPF_MAP_GET_NEXT_KEY = 4
BPF_PROG_LOAD = 5
BPF_RAW_TRACEPOINT_OPEN = 17

BPF_MAP_TYPE_HASH = 1
BPF_MAP_TYPE_ARRAY = 2

PROG_TYPE_TRACE_EVENT = 0x80000001

libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long


def bpf(cmd, payload):
    """Calls bpf(2) with a packed `union bpf_attr` prefix."""
    buffer = ctypes.create_string_buffer(bytes(payload), len(payload))
    result = libc.syscall(
        ctypes.c_long(SYS_BPF),
        ctypes.c_long(cmd),
        ctypes.cast(buffer, ctypes.c_void_p),
        ctypes.c_long(len(payload)),
    )
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


# --- instructions -----------------------------------------------------------


def insn(code, dst=0, src=0, off=0, imm=0):
    """One 8-byte eBPF instruction."""
    return struct.pack("<BBhi", code, (src << 4) | dst, off, imm)


# ALU64: class 0x07; LDX/STX: 0x61/0x63 (32-bit) and 0x79/0x7b (64-bit).
MOV64_REG = 0xBF
MOV64_IMM = 0xB7
ADD64_IMM = 0x07
AND64_IMM = 0x57
LDX_MEM_W = 0x61
STX_MEM_W = 0x63
LDX_MEM_DW = 0x79
STX_MEM_DW = 0x7B
LD_IMM64 = 0x18
CALL = 0x85
EXIT = 0x95
JEQ_IMM = 0x15

PSEUDO_MAP_FD = 1

R0, R1, R2, R3, R4, R5, R6 = range(7)
R10 = 10  # the frame pointer

# Helpers, with Linux's numbering where one exists.
HELPER_MAP_LOOKUP = 1
HELPER_MAP_UPDATE = 2
HELPER_MAP_DELETE = 3
HELPER_KTIME = 5
HELPER_TRACE_PRINTK = 6
HELPER_SMP_ID = 8
HELPER_PID_TGID = 14


def counter_program(map_fd, key_mask=1023):
    """`array[pid & mask] += 1`, reading the pid from the event context.

    The pid is copied through the program's 512-byte stack because the helper
    wants a pointer to key bytes, and the context itself is read-only but
    directly addressable at offset 8.
    """
    return b"".join(
        [
            insn(MOV64_REG, dst=R6, src=R1),          # r6 = ctx
            insn(LD_IMM64, dst=R1, src=PSEUDO_MAP_FD, imm=map_fd),
            insn(0, 0, 0, 0, 0),                      #   (high half of the fd)
            insn(MOV64_REG, dst=R3, src=R6),          # r3 = ctx
            insn(LDX_MEM_W, dst=R4, src=R3, off=8),   # r4 = *(u32 *)(ctx + 8) = pid
            insn(AND64_IMM, dst=R4, imm=key_mask),
            insn(STX_MEM_W, dst=R10, src=R4, off=-4), # *(u32 *)(r10 - 4) = r4
            insn(MOV64_REG, dst=R2, src=R10),         # r2 = &key
            insn(ADD64_IMM, dst=R2, imm=-4),
            insn(CALL, imm=HELPER_MAP_LOOKUP),        # r0 = map_lookup(map, &key)
            insn(JEQ_IMM, dst=R0, off=4, imm=0),      # if r0 == 0 goto exit
            insn(LDX_MEM_DW, dst=R1, src=R0, off=0),  # r1 = *r0
            insn(ADD64_IMM, dst=R1, imm=1),           # r1 += 1
            insn(STX_MEM_DW, dst=R0, src=R1, off=0),  # *r0 = r1
            insn(MOV64_IMM, dst=R0, imm=0),
            insn(EXIT),
        ]
    )


# --- bpf(2) wrappers --------------------------------------------------------


def map_create(map_type, key_size, value_size, max_entries, name=b"lite"):
    attr = bytearray(48)
    struct.pack_into("<IIIIII", attr, 0, map_type, key_size, value_size,
                     max_entries, 0, 0)
    struct.pack_into("<16s", attr, 28, name[:16])
    return bpf(BPF_MAP_CREATE, attr)


def map_lookup(fd, key):
    key_buffer = ctypes.create_string_buffer(key, len(key))
    value = ctypes.create_string_buffer(0x1000)
    attr = bytearray(32)
    struct.pack_into("<IIQQQ", attr, 0, fd, 0,
                     ctypes.addressof(key_buffer),
                     ctypes.addressof(value), 0)
    bpf(BPF_MAP_LOOKUP_ELEM, attr)
    return value.raw


def map_update(fd, key, value, flags=0):
    key_buffer = ctypes.create_string_buffer(key, len(key))
    value_buffer = ctypes.create_string_buffer(value, len(value))
    attr = bytearray(32)
    struct.pack_into("<IIQQQ", attr, 0, fd, 0,
                     ctypes.addressof(key_buffer),
                     ctypes.addressof(value_buffer), flags)
    return bpf(BPF_MAP_UPDATE_ELEM, attr)


def map_delete(fd, key):
    key_buffer = ctypes.create_string_buffer(key, len(key))
    attr = bytearray(32)
    struct.pack_into("<IIQQQ", attr, 0, fd, 0, ctypes.addressof(key_buffer), 0, 0)
    return bpf(BPF_MAP_DELETE_ELEM, attr)


def map_next_key(fd, key=None):
    """Returns the next key, or None when the iteration is over."""
    key_buffer = ctypes.create_string_buffer(key, len(key)) if key else None
    next_buffer = ctypes.create_string_buffer(64)
    attr = bytearray(32)
    struct.pack_into(
        "<IIQQQ", attr, 0, fd, 0,
        0 if key_buffer is None else ctypes.addressof(key_buffer),
        ctypes.addressof(next_buffer), 0,
    )
    try:
        bpf(BPF_MAP_GET_NEXT_KEY, attr)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        raise
    return next_buffer.raw


def prog_load(code, name=b"lite"):
    insns = ctypes.create_string_buffer(code, len(code))
    license_buffer = ctypes.create_string_buffer(b"GPL", 4)
    name_bytes = name[:16].ljust(16, b"\0")
    attr = bytearray(72)
    struct.pack_into("<IIQQ", attr, 0, PROG_TYPE_TRACE_EVENT,
                     len(code) // 8, ctypes.addressof(insns),
                     ctypes.addressof(license_buffer))
    struct.pack_into("<16s", attr, 48, name_bytes)
    return bpf(BPF_PROG_LOAD, attr)


def raw_tracepoint_open(name, prog_fd):
    name_buffer = ctypes.create_string_buffer(name.encode(), len(name) + 1)
    attr = bytearray(16)
    struct.pack_into("<QI", attr, 0, ctypes.addressof(name_buffer), prog_fd)
    return bpf(BPF_RAW_TRACEPOINT_OPEN, attr)


# --- commands ---------------------------------------------------------------


def cmd_count_live(args):
    """Same as `count`, but reads the map before detaching."""
    mask = 1023
    map_fd = map_create(BPF_MAP_TYPE_ARRAY, 4, 8, mask + 1, b"counts")
    prog_fd = prog_load(counter_program(map_fd, mask), b"count")
    link_fd = raw_tracepoint_open(args.event, prog_fd)

    with open("/proc/trace/bpf") as handle:
        sys.stderr.write(handle.read())

    time.sleep(args.seconds)
    rows = []
    for key in range(mask + 1):
        raw = map_lookup(map_fd, struct.pack("<I", key))
        count = struct.unpack("<Q", raw[:8])[0]
        if count:
            rows.append((count, key))
    rows.sort(reverse=True)

    for count, pid in rows[: args.top]:
        name = process_name(pid)
        print(f"{count:>10} {pid:>6} {name}")

    os.close(link_fd)
    os.close(prog_fd)
    os.close(map_fd)
    return 0


def process_name(pid):
    try:
        with open(f"/proc/{pid}/comm") as handle:
            return handle.read().strip()
    except OSError:
        return "-"


def cmd_verifier(args):
    """Loads one good and three bad programs, checking the verifier's verdicts."""
    map_fd = map_create(BPF_MAP_TYPE_ARRAY, 4, 8, 1024, b"check")

    def attempt(description, code, expect_ok):
        try:
            fd = prog_load(code, b"check")
        except OSError as error:
            verdict = f"refused ({os.strerror(error.errno)})"
            ok = not expect_ok
            detail = "" if ok else "  <-- expected to load"
        else:
            verdict = "loaded"
            ok = expect_ok
            detail = "" if ok else "  <-- expected to be refused"
            os.close(fd)
        print(f"{'ok  ' if ok else 'FAIL'} {description}: {verdict}{detail}")
        return ok

    good = counter_program(map_fd)
    loop = bytearray(good)
    # The conditional jump at slot 10 targets `exit`; point it backwards.
    struct.pack_into("<h", loop, 10 * 8 + 2, -6)
    store = bytearray(good)
    # Drop the stack store: the key is then read uninitialised.
    struct.pack_into("<BBhi", store, 6 * 8, 0, 0, 0, 0)
    no_exit = good[:-8]

    results = [
        attempt("a counter program", good, True),
        attempt("a backward jump", bytes(loop), False),
        attempt("an uninitialised stack read", bytes(store), False),
        attempt("a missing exit", no_exit, False),
    ]
    os.close(map_fd)
    return 0 if all(results) else 1


def cmd_load(args):
    """Loads the counter program and prints what the kernel says about it."""
    map_fd = map_create(BPF_MAP_TYPE_ARRAY, 4, 8, 1024, b"counts")
    prog_fd = prog_load(counter_program(map_fd), b"count")
    print(f"map_fd={map_fd} prog_fd={prog_fd}")
    with open("/proc/trace/bpf") as handle:
        sys.stdout.write(handle.read())
    os.close(prog_fd)
    os.close(map_fd)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = parser.add_subparsers(dest="command", required=True)

    count = subcommands.add_parser("count", help="count events per pid")
    count.add_argument("--event", default="syscall:sys_enter",
                       help="subsystem:event, as listed in /proc/trace/available_events")
    count.add_argument("--seconds", type=float, default=5.0)
    count.add_argument("--top", type=int, default=12)
    count.set_defaults(run=cmd_count_live)

    verifier = subcommands.add_parser("verifier", help="check the verifier's verdicts")
    verifier.set_defaults(run=cmd_verifier)

    load = subcommands.add_parser("load", help="load a program and show /proc/trace/bpf")
    load.set_defaults(run=cmd_load)

    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
