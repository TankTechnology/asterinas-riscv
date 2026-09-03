#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Own one bounded TCP bridge from a Megrez-visible port to Clash."""

from __future__ import annotations

import ipaddress
import math
import os
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO, Protocol


MAX_STDERR_BYTES = 64 * 1024


class ProxyBridgeError(RuntimeError):
    """A stable proxy bridge lifecycle failure."""


class BridgeProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


def _validate_ipv4(value: str, role: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{role} must be a canonical IPv4 address")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError(f"{role} must be a canonical IPv4 address") from error
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{role} must be a canonical IPv4 address")


def _validate_port(value: int, role: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"{role} must be an integer between 1 and 65535")


def _validate_timeout(value: float, role: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{role} must be a finite positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{role} must be a finite positive number")


@dataclass(frozen=True)
class ProxyBridgeConfig:
    """Immutable addresses and deadlines for one owned bridge."""

    listen_address: str = "10.100.19.216"
    listen_port: int = 17893
    upstream_address: str = "127.0.0.1"
    upstream_port: int = 17892
    startup_timeout: float = 5.0
    shutdown_timeout: float = 2.0

    def __post_init__(self) -> None:
        _validate_ipv4(self.listen_address, "listen address")
        _validate_port(self.listen_port, "listen port")
        _validate_ipv4(self.upstream_address, "upstream address")
        _validate_port(self.upstream_port, "upstream port")
        _validate_timeout(self.startup_timeout, "startup timeout")
        _validate_timeout(self.shutdown_timeout, "shutdown timeout")


def probe_tcp_endpoint(address: str, port: int, timeout: float) -> bool:
    """Return whether a bounded TCP connect reaches one endpoint."""

    try:
        connection = socket.create_connection((address, port), timeout=timeout)
    except OSError:
        return False
    connection.close()
    return True


class ProxyBridge:
    """Start, verify, summarize, and reap one socat process group."""

    def __init__(
        self,
        config: ProxyBridgeConfig,
        *,
        process_factory: Callable[..., BridgeProcess] = subprocess.Popen,
        endpoint_probe: Callable[[str, int, float], bool] = probe_tcp_endpoint,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        stderr_file: BinaryIO | None = None,
        terminate_group: Callable[[int, int], None] = os.killpg,
    ) -> None:
        self.config = config
        self._process_factory = process_factory
        self._endpoint_probe = endpoint_probe
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._terminate_group = terminate_group
        self._owns_stderr = stderr_file is None
        self.stderr_file = stderr_file or tempfile.SpooledTemporaryFile(
            max_size=MAX_STDERR_BYTES,
            mode="w+b",
        )
        self._process: BridgeProcess | None = None
        self._ready = False
        self._pid: int | None = None
        self._exit_status: int | None = None
        self._stderr_hex = ""

    def __enter__(self) -> ProxyBridge:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        del exc_info
        self.close()

    @property
    def running(self) -> bool:
        return (
            self._ready
            and self._process is not None
            and self._process.poll() is None
        )

    def _endpoint(self, *, upstream: bool) -> tuple[str, int]:
        if upstream:
            return self.config.upstream_address, self.config.upstream_port
        return self.config.listen_address, self.config.listen_port

    def _probe(self, *, upstream: bool, timeout: float) -> bool:
        address, port = self._endpoint(upstream=upstream)
        return self._endpoint_probe(address, port, min(timeout, 1.0))

    def start(self) -> ProxyBridge:
        if self.running:
            return self
        if self._process is not None:
            raise ProxyBridgeError("proxy-bridge-state-invalid")
        if not self._probe(upstream=True, timeout=1.0):
            raise ProxyBridgeError("proxy-upstream-unavailable")
        if self._probe(upstream=False, timeout=1.0):
            raise ProxyBridgeError("proxy-listener-in-use")

        argv = (
            "socat",
            (
                f"TCP-LISTEN:{self.config.listen_port},"
                f"bind={self.config.listen_address},reuseaddr,fork"
            ),
            f"TCP:{self.config.upstream_address}:{self.config.upstream_port}",
        )
        try:
            process = self._process_factory(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self.stderr_file,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as error:
            reason = error.errno if error.errno is not None else "unknown"
            raise ProxyBridgeError(f"proxy-bridge-spawn:{reason}") from error
        self._process = process
        self._pid = process.pid
        deadline = self._monotonic() + self.config.startup_timeout
        failure: str | None = None
        while True:
            status = process.poll()
            if status is not None:
                self._exit_status = status
                failure = f"proxy-bridge-exited:{status}"
                break
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                failure = "proxy-bridge-startup-timeout"
                break
            if self._probe(upstream=False, timeout=remaining):
                self._ready = True
                return self
            self._sleeper(min(0.05, remaining))

        self.close()
        raise ProxyBridgeError(failure)

    def _capture_stderr(self) -> None:
        try:
            position = self.stderr_file.tell()
            self.stderr_file.seek(0)
            data = self.stderr_file.read(MAX_STDERR_BYTES)
            self.stderr_file.seek(position)
        except (OSError, ValueError):
            data = b""
        self._stderr_hex = bytes(data).hex()

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        self._ready = False
        status = process.poll()
        if status is None:
            self._terminate_group(process.pid, signal.SIGTERM)
            try:
                status = process.wait(timeout=self.config.shutdown_timeout)
            except (subprocess.TimeoutExpired, TimeoutError):
                self._terminate_group(process.pid, signal.SIGKILL)
                try:
                    status = process.wait(timeout=self.config.shutdown_timeout)
                except (subprocess.TimeoutExpired, TimeoutError) as error:
                    self._capture_stderr()
                    raise ProxyBridgeError("proxy-bridge-reap-timeout") from error
        self._exit_status = status
        self._capture_stderr()

    def summary(self) -> dict[str, object]:
        self._capture_stderr()
        status = self._exit_status
        if self._process is not None:
            status = self._process.poll()
        return {
            "schema_version": 1,
            "listen": f"{self.config.listen_address}:{self.config.listen_port}",
            "upstream": (
                f"{self.config.upstream_address}:{self.config.upstream_port}"
            ),
            "pid": self._pid,
            "ready": self._ready,
            "exit_status": status,
            "stderr_hex": self._stderr_hex,
        }
