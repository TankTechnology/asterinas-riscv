#!/usr/bin/env python3
"""A tiny, dependency-free loader and analysis tool for Asterinas's eBPF subset.

The kernel side lives in kernel/src/bpf: an interpreter, a conservative
verifier, array and hash maps, and attachment to the kernel's own trace
events (`/proc/trace/events`). This tool is the userspace half -- a
"bpftool lite" that speaks `bpf(2)` directly, so it works on a board with no
BPF headers, no libbpf and no clang.

    bpftool_lite.py verifier
    bpftool_lite.py count    --event syscall:sys_enter --seconds 5
    bpftool_lite.py syscalls --event syscall:sys_enter --seconds 5
    bpftool_lite.py blocks   --event block:block_rq_issue --seconds 5
    bpftool_lite.py load

Everything is root-only on the kernel side; run it as root.

The kernel's context for a trace event is 48 bytes:

    0   u64 timestamp_ns
    8   u32 pid (tgid)
    12  u32 tid
    16  u32 event id
    20  u32 cpu
    24  u64 arg0    event-specific (syscall number, sectors, address, pc, ...)
    32  u64 arg1
    40  u64 arg2
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


MOV64_REG = 0xBF
MOV64_IMM = 0xB7
ADD64_IMM = 0x07
AND64_IMM = 0x57
OR64_REG = 0x4F
LSH64_IMM = 0x67
LDX_MEM_W = 0x61
STX_MEM_W = 0x63
LDX_MEM_DW = 0x79
STX_MEM_DW = 0x7B
LD_IMM64 = 0x18
CALL = 0x85
EXIT = 0x95
JEQ_IMM = 0x15
JNE_IMM = 0x55
JLT_IMM = 0xA5

PSEUDO_MAP_FD = 1

R0, R1, R2, R3, R4, R5, R6, R7, R8, R9 = range(10)
R10 = 10  # the frame pointer

# Helpers, with Linux's numbering where one exists.
HELPER_MAP_LOOKUP = 1
HELPER_MAP_UPDATE = 2
HELPER_MAP_DELETE = 3
HELPER_KTIME = 5
HELPER_TRACE_PRINTK = 6
HELPER_SMP_ID = 8
HELPER_PID_TGID = 14

# Context offsets.
CTX_PID = 8
CTX_ARG0 = 24
CTX_ARG1 = 32


class Assembly:
    """A label-based assembler for the subset the kernel accepts.

    Labels keep the branch offsets readable; the kernel's verifier accepts
    only forward jumps, so a target must come later in the program.
    """

    def __init__(self):
        self.items = []

    def label(self, name):
        self.items.append(("label", name))
        return self

    def i(self, code, dst=0, src=0, off=0, imm=0, target=None):
        self.items.append(
            (
                "insn",
                {"code": code, "dst": dst, "src": src, "off": off,
                 "imm": imm, "target": target},
            )
        )
        return self

    def map_load(self, reg, map_fd):
        """`reg = map`, the two-slot pseudo load."""
        return self.i(LD_IMM64, dst=reg, src=PSEUDO_MAP_FD, imm=map_fd)

    def build(self):
        positions = {}
        cursor = 0
        for kind, value in self.items:
            if kind == "label":
                positions[value] = cursor
            else:
                cursor += 2 if value["code"] == LD_IMM64 else 1

        out = bytearray()
        cursor = 0
        for kind, value in self.items:
            if kind == "label":
                continue
            if value["code"] == LD_IMM64:
                out += insn(value["code"], value["dst"], value["src"],
                            value["off"], value["imm"])
                out += insn(0)
                cursor += 2
                continue
            off = value["off"]
            if value["target"] is not None:
                off = positions[value["target"]] - (cursor + 1)
                if off <= 0:
                    raise ValueError(f"the jump to {value['target']} is not forward")
            out += insn(value["code"], value["dst"], value["src"], off, value["imm"])
            cursor += 1
        return bytes(out)


def counter_program(map_fd, key_mask=1023):
    """`array[pid & mask] += 1`."""
    asm = Assembly()
    asm.i(MOV64_REG, dst=R6, src=R1)                  # r6 = ctx
    asm.i(LDX_MEM_W, dst=R4, src=R6, off=CTX_PID)     # r4 = pid
    asm.i(AND64_IMM, dst=R4, imm=key_mask)
    asm.i(STX_MEM_W, dst=R10, src=R4, off=-4)         # *(u32 *)(r10 - 4) = pid
    asm.i(MOV64_REG, dst=R2, src=R10)
    asm.i(ADD64_IMM, dst=R2, imm=-4)
    asm.map_load(R1, map_fd)
    asm.i(CALL, imm=HELPER_MAP_LOOKUP)
    asm.i(JEQ_IMM, dst=R0, target="exit", imm=0)
    asm.i(LDX_MEM_DW, dst=R1, src=R0, off=0)
    asm.i(ADD64_IMM, dst=R1, imm=1)
    asm.i(STX_MEM_DW, dst=R0, src=R1, off=0)
    asm.i(MOV64_IMM, dst=R0, imm=0)
    asm.label("exit")
    asm.i(EXIT)
    return asm.build()


def histogram_program(map_fd, kind):
    """`hash[(pid << 32) | value] += 1`, creating the entry on first sight.

    `kind` picks what `value` is: the syscall number (`arg0`), or a bucket of
    a block request's size in sectors. Bucketing is a comparison ladder
    because the verifier allows no loops.
    """
    asm = Assembly()
    asm.i(MOV64_REG, dst=R6, src=R1)                  # r6 = ctx
    asm.i(LDX_MEM_W, dst=R7, src=R6, off=CTX_PID)     # r7 = pid
    asm.i(LDX_MEM_DW, dst=R8, src=R6, off=CTX_ARG0)   # r8 = arg0

    if kind == "block":
        # Sector limits: 8 = 4 KiB, 16 = 8 KiB, 64 = 32 KiB, 256 = 128 KiB,
        # 2048 = 1 MiB.
        for bucket, limit in enumerate((8, 16, 64, 256, 2048)):
            asm.i(MOV64_IMM, dst=R9, imm=bucket)
            asm.i(JLT_IMM, dst=R8, target="store", imm=limit)
        asm.i(MOV64_IMM, dst=R9, imm=5)
        asm.label("store")
        asm.i(MOV64_REG, dst=R8, src=R9)
        # Fold the request type (read/write/flush) into the key too:
        # key = (pid << 40) | (kind << 32) | bucket.
        asm.i(LDX_MEM_DW, dst=R9, src=R6, off=CTX_ARG1)
        asm.i(LSH64_IMM, dst=R9, imm=32)
        asm.i(OR64_REG, dst=R8, src=R9)
        asm.i(LSH64_IMM, dst=R7, imm=40)
        asm.i(OR64_REG, dst=R7, src=R8)
    else:
        asm.i(LSH64_IMM, dst=R7, imm=32)
        asm.i(OR64_REG, dst=R7, src=R8)               # key = (pid << 32) | number
    asm.i(STX_MEM_DW, dst=R10, src=R7, off=-8)        # key at r10 - 8
    asm.i(MOV64_IMM, dst=R0, imm=0)
    asm.i(STX_MEM_DW, dst=R10, src=R0, off=-16)       # zero value at r10 - 16

    asm.map_load(R1, map_fd)
    asm.i(MOV64_REG, dst=R2, src=R10)
    asm.i(ADD64_IMM, dst=R2, imm=-8)
    asm.i(CALL, imm=HELPER_MAP_LOOKUP)
    asm.i(JNE_IMM, dst=R0, target="increment", imm=0)

    # The key is new: create it, then look it up again.
    asm.map_load(R1, map_fd)
    asm.i(MOV64_REG, dst=R2, src=R10)
    asm.i(ADD64_IMM, dst=R2, imm=-8)
    asm.i(MOV64_REG, dst=R3, src=R10)
    asm.i(ADD64_IMM, dst=R3, imm=-16)
    asm.i(MOV64_IMM, dst=R4, imm=0)                   # BPF_ANY
    asm.i(CALL, imm=HELPER_MAP_UPDATE)
    asm.map_load(R1, map_fd)
    asm.i(MOV64_REG, dst=R2, src=R10)
    asm.i(ADD64_IMM, dst=R2, imm=-8)
    asm.i(CALL, imm=HELPER_MAP_LOOKUP)
    asm.i(JEQ_IMM, dst=R0, target="exit", imm=0)

    asm.label("increment")
    asm.i(LDX_MEM_DW, dst=R1, src=R0, off=0)
    asm.i(ADD64_IMM, dst=R1, imm=1)
    asm.i(STX_MEM_DW, dst=R0, src=R1, off=0)
    asm.i(MOV64_IMM, dst=R0, imm=0)
    asm.label("exit")
    asm.i(EXIT)
    return asm.build()


# --- bpf(2) wrappers --------------------------------------------------------


def map_create(map_type, key_size, value_size, max_entries, name=b"lite"):
    attr = bytearray(48)
    struct.pack_into("<IIIIII", attr, 0, map_type, key_size, value_size,
                     max_entries, 0, 0)
    struct.pack_into("<16s", attr, 28, name[:16])
    return bpf(BPF_MAP_CREATE, attr)


def map_lookup(fd, key, value_size=8):
    key_buffer = ctypes.create_string_buffer(key, len(key))
    value = ctypes.create_string_buffer(0x1000)
    attr = bytearray(32)
    struct.pack_into("<IIQQQ", attr, 0, fd, 0,
                     ctypes.addressof(key_buffer),
                     ctypes.addressof(value), 0)
    bpf(BPF_MAP_LOOKUP_ELEM, attr)
    return value.raw[:value_size]


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


def map_next_key(fd, key=None, key_size=8):
    """The next key, or None when the iteration is over."""
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
    return next_buffer.raw[:key_size]


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


# --- names ------------------------------------------------------------------

# The RISC-V (asm-generic) syscall numbers, for the numbers that show up on a
# busy desktop. Anything else prints as a bare number.
SYSCALLS = {
    17: "getcwd", 19: "eventfd2", 23: "dup", 25: "fcntl", 29: "ioctl",
    35: "unlinkat", 46: "ftruncate", 48: "faccessat", 56: "openat",
    57: "close", 59: "pipe2", 61: "getdents64", 62: "lseek", 63: "read",
    64: "write", 65: "readv", 66: "writev", 67: "pread64", 68: "pwrite64",
    72: "pselect6", 73: "ppoll", 74: "signalfd4", 78: "readlinkat",
    79: "newfstatat", 80: "fstat", 82: "fsync", 85: "timerfd_create",
    86: "timerfd_settime", 93: "exit", 94: "exit_group", 96: "set_tid_address",
    98: "futex", 101: "nanosleep", 113: "clock_gettime", 114: "clock_getres",
    115: "clock_nanosleep", 124: "sched_yield", 129: "kill", 130: "tkill",
    131: "tgkill", 134: "rt_sigaction", 135: "rt_sigprocmask",
    139: "rt_sigreturn", 153: "times", 160: "uname", 167: "prctl",
    172: "getpid", 173: "getppid", 174: "getuid", 176: "geteuid",
    178: "gettid", 198: "socket", 200: "bind", 201: "listen", 203: "connect",
    206: "sendto", 207: "recvfrom", 208: "setsockopt", 209: "getsockopt",
    211: "sendmsg", 212: "recvmsg", 213: "shutdown", 214: "brk",
    215: "munmap", 216: "mremap", 220: "clone", 221: "execve", 222: "mmap",
    226: "mprotect", 233: "madvise", 260: "wait4", 261: "prlimit64",
    276: "renameat2", 278: "getrandom", 279: "memfd_create", 280: "bpf",
    291: "statx", 437: "openat2", 438: "pidfd_getfd", 441: "epoll_pwait2",
    455: "futex_waitv",
}

BLOCK_BUCKETS = [
    "<4 KiB",
    "4-8 KiB",
    "8-32 KiB",
    "32-128 KiB",
    "128 KiB-1 MiB",
    ">=1 MiB",
]

BIO_TYPES = ["read", "write", "flush"]


def process_name(pid):
    try:
        with open(f"/proc/{pid}/comm") as handle:
            return handle.read().strip()
    except OSError:
        return "?"


# --- commands ---------------------------------------------------------------


def run_with(args, map_type, key_size, value_size, entries, code_fn, dump):
    """Creates the map, loads the program, attaches it, samples, then dumps."""
    if os.geteuid() != 0:
        print("this must run as root: the kernel loads programs for root only",
              file=sys.stderr)
        return 1

    map_fd = map_create(map_type, key_size, value_size, entries, b"litetool")
    prog_fd = prog_load(code_fn(map_fd), b"litetool")
    link_fd = raw_tracepoint_open(args.event, prog_fd)
    print(f"attached to {args.event} (map_fd={map_fd} prog_fd={prog_fd})",
          file=sys.stderr)

    time.sleep(args.seconds)
    dump(map_fd)
    with open("/proc/trace/bpf") as handle:
        sys.stderr.write(handle.read())

    for fd in (link_fd, prog_fd, map_fd):
        os.close(fd)
    return 0


def cmd_count(args):
    entries = 1024

    def dump(map_fd):
        rows = []
        for pid in range(entries):
            raw = map_lookup(map_fd, struct.pack("<I", pid))
            count = struct.unpack("<Q", raw[:8])[0]
            if count:
                rows.append((count, pid))
        rows.sort(reverse=True)
        print(f"{'events':>10} {'pid':>6}  comm")
        for count, pid in rows[: args.top]:
            print(f"{count:>10} {pid:>6}  {process_name(pid)}")

    return run_with(args, BPF_MAP_TYPE_ARRAY, 4, 8, entries,
                    lambda fd: counter_program(fd, entries - 1), dump)


def cmd_histogram(args, kind):
    entries = 512

    def dump(map_fd):
        rows = []
        key = None
        while True:
            key = map_next_key(map_fd, key)
            if key is None:
                break
            count = struct.unpack("<Q", map_lookup(map_fd, key))[0]
            packed = struct.unpack("<Q", key)[0]
            if kind == "block":
                rows.append((count, packed >> 40, (packed >> 32) & 0xFF, packed & 0xFF))
            else:
                rows.append((count, packed >> 32, packed & 0xFFFFFFFF, 0))
        rows.sort(reverse=True)
        if kind == "syscall":
            print(f"{'calls':>10} {'pid':>6}  {'syscall':<20} comm")
            for count, pid, number, _ in rows[: args.top]:
                name = SYSCALLS.get(number, f"syscall {number}")
                print(f"{count:>10} {pid:>6}  {name:<20} {process_name(pid)}")
        else:
            print(f"{'requests':>10} {'pid':>6}  {'io':<6} {'size':<14} comm")
            for count, pid, kind_bits, bucket in rows[: args.top]:
                io = BIO_TYPES[kind_bits] if kind_bits < len(BIO_TYPES) else "?"
                name = BLOCK_BUCKETS[bucket] if bucket < len(BLOCK_BUCKETS) else "?"
                print(f"{count:>10} {pid:>6}  {io:<6} {name:<14} "
                      f"{process_name(pid)}")

    return run_with(args, BPF_MAP_TYPE_HASH, 8, 8, entries,
                    lambda fd: histogram_program(fd, kind), dump)


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
    # The conditional jump near the end targets `exit`; point it backwards.
    for index in range(len(good) // 8):
        code = good[index * 8]
        if code == JEQ_IMM:
            struct.pack_into("<h", loop, index * 8 + 2, -6)
            break
    store = bytearray(good)
    struct.pack_into("<BBhi", store, 3 * 8, 0, 0, 0, 0)  # drop the stack store
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
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = parser.add_subparsers(dest="command", required=True)

    def sampling(name, help_text, default_event="syscall:sys_enter"):
        command = subcommands.add_parser(name, help=help_text)
        command.add_argument("--event", default=default_event,
                             help="subsystem:event, as listed in "
                                  "/proc/trace/available_events")
        command.add_argument("--seconds", type=float, default=5.0)
        command.add_argument("--top", type=int, default=15)
        return command

    count = sampling("count", "count events per process")
    count.set_defaults(run=cmd_count)

    syscalls = sampling("syscalls", "count events per process and syscall number")
    syscalls.set_defaults(run=lambda args: cmd_histogram(args, "syscall"))

    blocks = sampling("blocks", "histogram of block request sizes per process",
                      default_event="block:block_rq_issue")
    blocks.set_defaults(run=lambda args: cmd_histogram(args, "block"))

    verifier = subcommands.add_parser("verifier", help="check the verifier's verdicts")
    verifier.set_defaults(run=cmd_verifier)

    load = subcommands.add_parser("load", help="load a program and show /proc/trace/bpf")
    load.set_defaults(run=cmd_load)

    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
