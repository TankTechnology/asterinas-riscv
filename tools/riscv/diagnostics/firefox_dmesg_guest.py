#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Guest-side bounded drain for unmodified ``dmesg --follow-new --raw``."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import threading
import time
import zlib


MAX_DMESG_BYTES = 4 * 1024 * 1024
FRAME_TEXT_BYTES = 600
PHASES = frozenset(
    (
        "collector_started",
        "request_selected",
        "request_enter",
        "snapshot_before_start",
        "snapshot_before_end",
        "snapshot_during_start",
        "snapshot_during_end",
        "request_return",
        "request_error",
        "snapshot_after_start",
        "snapshot_after_end",
        "collector_stopping",
    )
)


def _positive(value):
    return type(value) is int and value > 0


def _read_available(stream, size):
    """Read one currently available pipe buffer instead of filling ``size``."""
    read1 = getattr(stream, "read1", None)
    return read1(size) if read1 is not None else stream.read(size)


def _marker(phase, request_id, firefox_pid, client_pid):
    if (
        phase not in PHASES
        or type(request_id) is not int
        or request_id < 0
        or (
            request_id == 0 and phase not in ("collector_started", "collector_stopping")
        )
        or not _positive(firefox_pid)
        or not _positive(client_pid)
    ):
        raise ValueError("invalid Firefox dmesg marker")
    return (
        "<14>ASTERINAS_FF_KLOG version=1 "
        f"phase={phase} request_id={request_id} "
        f"firefox_pid={firefox_pid} client_pid={client_pid}"
    ).encode("ascii")


def write_marker(phase, request_id, firefox_pid, client_pid):
    """Write one scalar marker from a snapshot worker."""
    message = _marker(phase, int(request_id), int(firefox_pid), int(client_pid))
    descriptor = os.open("/dev/kmsg", os.O_WRONLY | os.O_CLOEXEC)
    try:
        count = os.write(descriptor, message)
        if count != len(message):
            raise OSError("short /dev/kmsg write")
    finally:
        os.close(descriptor)


class BoundedDmesg:
    """Drain continuously, retain a prefix, and report any discarded suffix."""

    def __init__(
        self,
        *,
        firefox_pid,
        client_pid,
        limit=MAX_DMESG_BYTES,
        process_factory=subprocess.Popen,
        open_kmsg=None,
        write_kmsg=os.write,
        close_kmsg=os.close,
        emit=None,
        ready_timeout=5.0,
        cleanup_timeout=2.0,
    ):
        if type(limit) is not int or not 0 < limit <= MAX_DMESG_BYTES:
            raise ValueError("invalid dmesg retention limit")
        if not _positive(firefox_pid) or not _positive(client_pid):
            raise ValueError("invalid dmesg process identity")
        if ready_timeout <= 0 or cleanup_timeout <= 0:
            raise ValueError("invalid dmesg timeout")
        self.firefox_pid = firefox_pid
        self.client_pid = client_pid
        self.request_id = 0
        self.limit = limit
        self._process_factory = process_factory
        self._open_kmsg = open_kmsg or (
            lambda: os.open("/dev/kmsg", os.O_WRONLY | os.O_CLOEXEC)
        )
        self._write_kmsg = write_kmsg
        self._close_kmsg = close_kmsg
        self._emit = emit or (lambda line: print(line, flush=True))
        self._ready_timeout = ready_timeout
        self._cleanup_timeout = cleanup_timeout
        self._process = None
        self._thread = None
        self._kmsg_fd = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._retained = bytearray()
        self._raw_bytes = 0
        self._discarded_bytes = 0
        self._discarded_lines = 0
        self._early_exit = False
        self._emitted = False

    @property
    def raw_bytes(self):
        with self._lock:
            return self._raw_bytes

    @property
    def early_exit(self):
        with self._lock:
            return self._early_exit

    def start(self, command):
        if self._process is not None or not isinstance(command, (list, tuple)):
            raise ValueError("dmesg collector already started or command is invalid")
        self._kmsg_fd = self._open_kmsg()
        try:
            self._process = self._process_factory(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
            if self._process.stdout is None or not _positive(self._process.pid):
                raise OSError("dmesg child has no readable stdout or PID")
            self._thread = threading.Thread(
                target=self._drain, name="asterinas-dmesg-drain", daemon=True
            )
            self._thread.start()
            deadline = time.monotonic() + self._ready_timeout
            while not self._ready.is_set():
                if self._process.poll() is not None:
                    raise OSError("dmesg exited before readiness marker")
                self.mark("collector_started")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("dmesg did not observe its readiness marker")
                self._ready.wait(min(0.05, remaining))
        except BaseException:
            self._stop_process()
            if self._kmsg_fd is not None:
                self._close_kmsg(self._kmsg_fd)
                self._kmsg_fd = None
            raise

    def _drain(self):
        needle = b"ASTERINAS_FF_KLOG version=1 phase=collector_started "
        tail = b""
        while True:
            chunk = _read_available(self._process.stdout, 65536)
            if not chunk:
                break
            if needle in tail + chunk:
                self._ready.set()
            tail = (tail + chunk)[-len(needle) :]
            with self._lock:
                self._raw_bytes += len(chunk)
                kept = min(len(chunk), self.limit - len(self._retained))
                self._retained.extend(chunk[:kept])
                discarded = chunk[kept:]
                self._discarded_bytes += len(discarded)
                self._discarded_lines += discarded.count(b"\n")
        if not self._stopping.is_set():
            with self._lock:
                self._early_exit = True

    def set_request(self, request_id):
        if not _positive(request_id) or self.request_id:
            raise ValueError("selected request must be set exactly once")
        self.request_id = request_id

    def mark(self, phase):
        if self._kmsg_fd is None:
            raise RuntimeError("dmesg collector is not running")
        message = _marker(phase, self.request_id, self.firefox_pid, self.client_pid)
        count = self._write_kmsg(self._kmsg_fd, message)
        if count != len(message):
            raise OSError("short /dev/kmsg write")

    def _stop_process(self):
        process = self._process
        if process is None:
            return None
        if process.poll() is not None:
            with self._lock:
                self._early_exit = True
        self._stopping.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=self._cleanup_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=self._cleanup_timeout)
        if self._thread is not None:
            self._thread.join(timeout=self._cleanup_timeout)
            if self._thread.is_alive():
                with self._lock:
                    self._early_exit = True
        return process.poll()

    def stop_and_emit(self):
        if self._process is None or self._emitted:
            raise RuntimeError("dmesg collector is absent or already emitted")
        returncode = self._stop_process()
        if self._kmsg_fd is not None:
            self._close_kmsg(self._kmsg_fd)
            self._kmsg_fd = None
        with self._lock:
            raw = bytes(self._retained)
            metadata = {
                "version": 1,
                "raw_bytes": self._raw_bytes,
                "retained_bytes": len(raw),
                "discarded_bytes": self._discarded_bytes,
                "discarded_lines": self._discarded_lines,
                "early_exit": self._early_exit,
                "returncode": returncode,
            }
        encoded = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
        parts = [
            encoded[index : index + FRAME_TEXT_BYTES]
            for index in range(0, len(encoded), FRAME_TEXT_BYTES)
        ]
        digest = hashlib.sha256(raw).hexdigest()
        lines = [
            "A_FF_DMESG_META "
            + json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        ]
        lines.extend(
            f"A_FF_DMESG part={index}/{len(parts)} sha256={digest} data={part}"
            for index, part in enumerate(parts)
        )
        for line in lines:
            self._emit(line)
        self._emitted = True
        return lines
