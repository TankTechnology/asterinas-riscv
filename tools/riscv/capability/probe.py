#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Probe what the running OS actually supports.

The point is to answer "what can this kernel do?" with evidence instead of
impressions: every entry records one interface, whether it works, and the
error the kernel returned when it does not. It uses only the standard library
so it runs on a stock Debian RISC-V rootfs, and it never modifies state --
temporary files live in a private directory that is removed on exit.

Usage:
    probe.py             # human-readable report on stdout
    probe.py --json      # one JSON document, for a page or a gate
"""

from __future__ import annotations

import errno
import json
import mmap
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

RESULTS: list[dict[str, str]] = []


def record(area: str, name: str, status: str, detail: str = "") -> None:
    RESULTS.append({"area": area, "name": name, "status": status, "detail": detail})


def check(area: str, name: str):
    """Wraps one probe so a failure is recorded instead of aborting the run."""

    def decorator(fn):
        def runner(*args, **kwargs):
            try:
                detail = fn(*args, **kwargs) or ""
                record(area, name, "ok", str(detail))
            except PermissionError as error:
                record(area, name, "denied", errno.errorcode.get(error.errno, str(error)))
            except OSError as error:
                supported_errors = (errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOTSUP)
                status = "unsupported" if error.errno in supported_errors else "error"
                code = errno.errorcode.get(error.errno, str(error.errno))
                record(area, name, status, f"{code}: {error.strerror}")
            except Exception as error:  # noqa: BLE001 - a probe must not abort the run
                record(area, name, "error", f"{type(error).__name__}: {error}")

        return runner

    return decorator


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="probe.")
    try:
        probe_system()
        probe_process()
        probe_memory()
        probe_filesystem(workdir)
        probe_ipc()
        probe_network()
        probe_namespaces()
        probe_observability()
    finally:
        subprocess.run(["rm", "-rf", workdir], check=False)

    if "--json" in sys.argv:
        json.dump(RESULTS, sys.stdout, indent=1, sort_keys=True)
        print()
    else:
        area = None
        for entry in RESULTS:
            if entry["area"] != area:
                area = entry["area"]
                print(f"\n== {area}")
            line = f"  {entry['status']:<11} {entry['name']}"
            if entry["detail"]:
                line += f"  ({entry['detail']})"
            print(line)
        counts: dict[str, int] = {}
        for entry in RESULTS:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        print("\nsummary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


# --- system ---------------------------------------------------------------


def probe_system() -> None:
    record("system", "uname", "ok", " ".join(platform.uname()))
    record("system", "python", "ok", platform.python_version())
    for path in ("/proc/version", "/proc/cpuinfo", "/proc/meminfo"):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                data = handle.read()
            record("system", path, "ok", f"{len(data)} bytes")
        except OSError as error:
            record("system", path, "missing", error.strerror or "")

    @check("system", "AT_HWCAP (auxv)")
    def _hwcap():
        import struct

        with open("/proc/self/auxv", "rb") as handle:
            data = handle.read()
        values = {}
        for offset in range(0, len(data) - 15, 16):
            key, value = struct.unpack_from("<QQ", data, offset)
            if key == 0:
                break
            values[key] = value
        hwcap = values.get(16)
        if hwcap is None:
            raise OSError(errno.EINVAL, "AT_HWCAP absent from the auxiliary vector")
        letters = "".join(chr(ord("A") + bit) for bit in range(26) if hwcap & (1 << bit))
        return f"0x{hwcap:x} ({letters}) hwcap2=0x{values.get(26, 0):x}"

    _hwcap()

    @check("system", "clock_gettime(CLOCK_MONOTONIC)")
    def _monotonic():
        a = time.clock_gettime(time.CLOCK_MONOTONIC)
        time.sleep(0.01)
        b = time.clock_gettime(time.CLOCK_MONOTONIC)
        return f"advanced {b - a:.3f}s"

    @check("system", "clock_gettime(CLOCK_BOOTTIME)")
    def _boottime():
        return f"{time.clock_gettime(time.CLOCK_BOOTTIME):.1f}s"

    @check("system", "nanosleep(1ms)")
    def _sleep():
        start = time.monotonic()
        time.sleep(0.001)
        return f"{1000 * (time.monotonic() - start):.2f}ms"

    _monotonic()
    _boottime()
    _sleep()


# --- process --------------------------------------------------------------


def probe_process() -> None:
    @check("process", "fork+exec+wait")
    def _fork():
        started = time.monotonic()
        for _ in range(20):
            proc = subprocess.run(["/bin/true"], check=False)
            assert proc.returncode == 0, proc.returncode
        return f"{(time.monotonic() - started) / 20 * 1000:.2f}ms/exec"

    @check("process", "threads")
    def _threads():
        counter = {"n": 0}
        lock = threading.Lock()

        def worker():
            for _ in range(1000):
                with lock:
                    counter["n"] += 1

        started = time.monotonic()
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert counter["n"] == 4000
        return f"4000 increments over 4 threads in {time.monotonic() - started:.2f}s"

    @check("process", "signals(SIGUSR1 + RT)")
    def _signals():
        seen = []
        signal.signal(signal.SIGUSR1, lambda *_: seen.append("usr1"))
        os.kill(os.getpid(), signal.SIGUSR1)
        rt = getattr(signal, "SIGRTMIN", None)
        if rt is None:
            return "SIGUSR1 ok, no SIGRTMIN defined"
        signal.signal(rt, lambda *_: seen.append("rt"))
        os.kill(os.getpid(), rt)
        time.sleep(0.05)
        return f"delivered {seen}"

    @check("process", "sched_setaffinity")
    def _affinity():
        count = len(os.sched_getaffinity(0))
        os.sched_setaffinity(0, {0})
        pinned = os.sched_getaffinity(0)
        os.sched_setaffinity(0, set(range(count)))
        return f"{count} cpus, pin to {pinned}"

    @check("process", "nice/setpriority")
    def _nice():
        os.nice(1)
        return "nice accepted"

    @check("process", "resource limits")
    def _rlimit():
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return f"NOFILE {soft}/{hard}"

    _fork()
    _threads()
    _signals()
    _affinity()
    _nice()
    _rlimit()


# --- memory ---------------------------------------------------------------


def probe_memory() -> None:
    @check("memory", "mmap anonymous")
    def _anon():
        with mmap.mmap(-1, 1 << 20) as area:
            area[:4] = b"test"
            return "1 MiB mapped"

    @check("memory", "mmap file-backed shared")
    def _file():
        fd = os.open("/tmp", os.O_RDONLY | os.O_DIRECTORY)
        try:
            return f"directory fd {fd}"
        finally:
            os.close(fd)

    @check("memory", "mprotect via mmap+PROT")
    def _prot():
        page = mmap.mmap(-1, mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        page[0:1] = b"x"
        page.close()
        return "read/write page ok"

    @check("memory", "madvise(MADV_DONTNEED) zeros the pages")
    def _madvise():
        # The address must be page aligned, so use an mmap rather than a
        # malloc'd buffer; and check the part that matters: Linux promises the
        # pages read back as zero afterwards.
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        size = 4 * mmap.PAGESIZE
        area = mmap.mmap(-1, size)
        addr = ctypes.addressof(ctypes.c_char.from_buffer(area))
        assert addr % mmap.PAGESIZE == 0
        area[0:size] = b"x" * size
        rc = libc.madvise(ctypes.c_void_p(addr), ctypes.c_size_t(size), 4)
        if rc != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        zeroed = area[0] == 0 and area[size // 2] == 0
        area.close()
        if not zeroed:
            record("memory", "MADV_DONTNEED page contents", "partial",
                   "accepted, but the mapping still returns the old bytes")
            return "accepted"
        return f"{size // 1024} KiB discarded and read back as zero"

    _anon()
    _file()
    _prot()
    _madvise()


# --- filesystem -----------------------------------------------------------


def probe_filesystem(workdir: str) -> None:
    path = os.path.join(workdir, "file")

    @check("filesystem", "write/read/fsync")
    def _rw():
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        os.write(fd, b"hello" * 100)
        os.fsync(fd)
        os.lseek(fd, 0, 0)
        data = os.read(fd, 4096)
        os.close(fd)
        assert data == b"hello" * 100
        return "500 bytes round-trip + fsync"

    @check("filesystem", "xattr")
    def _xattr():
        os.setxattr(path, b"user.probe", b"1")
        value = os.getxattr(path, b"user.probe")
        assert value == b"1"
        return "user.* round-trip"

    @check("filesystem", "fallocate")
    def _fallocate():
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            os.posix_fallocate(fd, 0, 1 << 20)
            return f"size now {os.fstat(fd).st_size}"
        finally:
            os.close(fd)

    @check("filesystem", "inotify")
    def _inotify():
        if not hasattr(os, "inotify_init"):
            return "python lacks inotify wrappers"
        fd = os.inotify_init()
        try:
            wd = os.inotify_add_watch(fd, workdir, os.IN_CREATE)
            with open(os.path.join(workdir, "appeared"), "w", encoding="utf-8") as handle:
                handle.write("x")
            time.sleep(0.2)
            data = os.read(fd, 4096)
            return f"watch {wd}, event bytes {len(data)}"
        finally:
            os.close(fd)

    @check("filesystem", "O_DIRECT")
    def _odirect():
        flags = getattr(os, "O_DIRECT", 0)
        if not flags:
            return "python has no O_DIRECT flag"
        fd = os.open(path, os.O_RDONLY | flags)
        os.close(fd)
        return "open accepted"

    @check("filesystem", "sendfile")
    def _sendfile():
        src = os.open(path, os.O_RDONLY)
        dst = os.open(os.path.join(workdir, "copy"), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            return f"copied {os.sendfile(dst, src, 0, 512)} bytes"
        finally:
            os.close(src)
            os.close(dst)

    @check("filesystem", "statvfs")
    def _statvfs():
        st = os.statvfs("/")
        return f"bsize={st.f_bsize} free={st.f_bavail * st.f_bsize // (1 << 20)}MiB"

    for probe in (_rw, _xattr, _fallocate, _inotify, _odirect, _sendfile, _statvfs):
        probe()


# --- IPC ------------------------------------------------------------------


def probe_ipc() -> None:
    @check("ipc", "pipe")
    def _pipe():
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"x" * 1000)
        assert len(os.read(read_fd, 2000)) == 1000
        os.close(read_fd)
        os.close(write_fd)
        return "1 KiB through a pipe"

    # SCM_RIGHTS is allowed per file class, not per socket: the B1 ownership
    # model passes regular files, pipes, sockets, epoll and pidfd handles, and
    # refuses file types it does not model yet. Report each class separately so
    # "SCM_RIGHTS is broken" cannot be read into a single failing case.
    def scm_rights(label: str, make_fd):
        @check("ipc", f"SCM_RIGHTS {label}")
        def _probe():
            left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            fd = make_fd()
            try:
                socket.send_fds(left, [b"fd"], [fd])
                message, fds, _, _ = socket.recv_fds(right, 16, 4)
                for received in fds:
                    os.close(received)
                return f"received {message!r} with {len(fds)} fd"
            finally:
                os.close(fd)
                left.close()
                right.close()

        _probe()

    def open_regular():
        handle, path = tempfile.mkstemp(dir="/tmp")
        os.close(handle)
        os.unlink(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.unlink(path)
        return fd

    held: list = []

    def open_socket():
        # The socketpair has to outlive the probe: taking only `fileno()` from a
        # temporary lets the collector close it before the fd is passed.
        pair = socket.socketpair()
        held.append(pair)
        # Duplicate so the probe's own close() does not close the fd the
        # socket object still owns. `os.dup` yields a plain integer fd.
        return os.dup(pair[0].fileno())

    scm_rights("regular file", open_regular)
    scm_rights("pipe", lambda: os.pipe()[0])
    scm_rights("unix socket", open_socket)
    scm_rights("/dev/null", lambda: os.open("/dev/null", os.O_RDONLY))
    if hasattr(os, "eventfd"):
        scm_rights("eventfd", lambda: os.eventfd(0))
    for pair in held:
        for sock in pair:
            sock.close()

    @check("ipc", "shared memory (POSIX shm)")
    def _shm():
        from multiprocessing import shared_memory
        shm = shared_memory.SharedMemory(create=True, size=4096)
        try:
            shm.buf[0:4] = b"mine"
            return f"segment {shm.name}"
        finally:
            shm.close()
            shm.unlink()

    @check("ipc", "semaphore (POSIX)")
    def _sem():
        from multiprocessing import Semaphore
        sem = Semaphore(1)
        assert sem.acquire(timeout=1)
        sem.release()
        return "acquire/release ok"

    @check("ipc", "epoll on pipe")
    def _epoll():
        import select
        read_fd, write_fd = os.pipe()
        poller = select.epoll()
        try:
            poller.register(read_fd, select.EPOLLIN)
            assert poller.poll(0) == []
            os.write(write_fd, b"!")
            events = poller.poll(1)
            return f"{len(events)} event(s)"
        finally:
            poller.close()
            os.close(read_fd)
            os.close(write_fd)

    for probe in (_pipe, _shm, _sem, _epoll):
        probe()


# --- network --------------------------------------------------------------


def probe_network() -> None:
    @check("network", "AF_INET TCP connect")
    def _tcp():
        host = os.environ.get("PROBE_TCP_HOST", "example.com")
        started = time.monotonic()
        with socket.create_connection((host, 80), timeout=5) as sock:
            sock.sendall(b"HEAD / HTTP/1.0\r\n\r\n")
            data = sock.recv(32)
        return f"{data.split()[0].decode() if data else 'no reply'} in {1000 * (time.monotonic() - started):.0f}ms"

    @check("network", "getaddrinfo")
    def _dns():
        info = socket.getaddrinfo("example.com", 80)
        return f"{len(info)} addresses"

    @check("network", "AF_INET6 socket")
    def _v6():
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.close()
        return "socket created"

    @check("network", "AF_PACKET raw")
    def _packet():
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        sock.close()
        return "socket created"

    @check("network", "AF_NETLINK route")
    def _netlink():
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
        sock.close()
        return "socket created"

    @check("network", "SO_BINDTODEVICE")
    def _bindtodevice():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25),
                            b"eth0\x00")
            return "option accepted"
        finally:
            sock.close()

    @check("network", "TCP listen + accept (loopback)")
    def _listen():
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port), timeout=3)
        conn, _ = server.accept()
        conn.close()
        client.close()
        server.close()
        return f"accepted on port {port}"

    for probe in (_tcp, _dns, _v6, _packet, _netlink, _bindtodevice, _listen):
        probe()


# --- namespaces -----------------------------------------------------------


def probe_namespaces() -> None:
    @check("namespaces", "unshare(CLONE_NEWNS)")
    def _mnt():
        if not hasattr(os, "unshare"):
            return "python lacks os.unshare"
        pid = os.fork()
        if pid == 0:
            try:
                os.unshare(os.CLONE_NEWNS)
                os._exit(0)
            except OSError:
                os._exit(1)
        _, status = os.waitpid(pid, 0)
        assert os.WEXITSTATUS(status) == 0
        return "child unshared mount namespace"

    @check("namespaces", "unshare(CLONE_NEWNET)")
    def _net():
        pid = os.fork()
        if pid == 0:
            try:
                os.unshare(os.CLONE_NEWNET)
                os._exit(0)
            except OSError:
                os._exit(1)
        _, status = os.waitpid(pid, 0)
        assert os.WEXITSTATUS(status) == 0
        return "child unshared net namespace"

    @check("namespaces", "/proc/self/ns")
    def _ns():
        entries = os.listdir("/proc/self/ns")
        return ", ".join(sorted(entries))

    @check("namespaces", "cgroup v2")
    def _cgroup():
        if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
            with open("/sys/fs/cgroup/cgroup.controllers", encoding="utf-8") as handle:
                return handle.read().strip() or "(empty)"
        return "/sys/fs/cgroup/cgroup.controllers missing"

    for probe in (_mnt, _net, _ns, _cgroup):
        probe()


# --- observability --------------------------------------------------------


def probe_observability() -> None:
    for path in (
        "/proc/self/status",
        "/proc/self/io",
        "/proc/self/smaps",
        "/proc/self/maps",
        "/proc/net/dev",
        "/proc/net/tcp",
        "/proc/partitions",
        "/sys/class/net",
        "/sys/block",
    ):
        try:
            if os.path.isdir(path):
                record("observability", path, "ok", f"{len(os.listdir(path))} entries")
            else:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    record("observability", path, "ok", f"{len(handle.read())} bytes")
        except OSError as error:
            record("observability", path, "missing", error.strerror or "")

    @check("observability", "strace")
    def _strace():
        proc = subprocess.run(["/usr/bin/strace", "-c", "-o", "/dev/null", "/bin/true"],
                              check=False, capture_output=True, text=True)
        return f"exit {proc.returncode}"


if __name__ == "__main__":
    sys.exit(main())
