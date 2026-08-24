#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded, signal-safe host runtime primitives for the Debian rootfs gate."""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import select
import signal
import socket
import stat
import time
from pathlib import Path
from types import TracebackType
from typing import Callable, Mapping, Protocol, Sequence


class GateTermination(RuntimeError):
    """A scoped termination request deferred until gate cleanup is complete."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"gate terminated by signal {signum}")


class MonitorError(RuntimeError):
    """A bounded HMP operation failed."""


class EarlyProcessExit(RuntimeError):
    """The launched process exited before the requested serial marker."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        super().__init__(f"gate process exited early with status {returncode}")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("operation deadline expired")
    return remaining


def _safe_name(name: str) -> str:
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ValueError(f"unsafe output name: {name!r}")
    if "\x00" in name:
        raise ValueError("output name contains NUL")
    return name


class PinnedOutputDirectory:
    """A no-follow directory handle used for all gate output mutations."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        path_flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        self._path_fd = os.open(self.path, path_flags)
        self._operation_fd = -1
        try:
            operation_flags = (
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            self._operation_fd = os.open(self.path, operation_flags)
            path_status = os.fstat(self._path_fd)
            operation_status = os.fstat(self._operation_fd)
            if (path_status.st_dev, path_status.st_ino) != (
                operation_status.st_dev,
                operation_status.st_ino,
            ):
                raise OSError("output directory changed while being pinned")
        except BaseException:
            if self._operation_fd >= 0:
                os.close(self._operation_fd)
            os.close(self._path_fd)
            raise

    def __enter__(self) -> PinnedOutputDirectory:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        for attribute in ("_operation_fd", "_path_fd"):
            fd = getattr(self, attribute, -1)
            if fd >= 0:
                os.close(fd)
                setattr(self, attribute, -1)

    def invalidate(self, *names: str) -> None:
        for candidate in names:
            name = _safe_name(candidate)
            try:
                os.unlink(name, dir_fd=self._operation_fd)
            except FileNotFoundError:
                continue
        os.fsync(self._operation_fd)

    def _temporary_name(self, destination: str) -> str:
        return f".gate-{destination}-{secrets.token_hex(8)}.tmp"

    def _publish(
        self,
        name: str,
        writer: Callable[[int], None],
        *,
        mode: int = 0o600,
    ) -> None:
        destination = _safe_name(name)
        temporary = self._temporary_name(destination)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(temporary, flags, mode, dir_fd=self._operation_fd)
        try:
            writer(fd)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self._operation_fd)
            except FileNotFoundError:
                pass
            raise
        else:
            os.close(fd)
        try:
            os.replace(
                temporary,
                destination,
                src_dir_fd=self._operation_fd,
                dst_dir_fd=self._operation_fd,
            )
            os.fsync(self._operation_fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=self._operation_fd)
            except FileNotFoundError:
                pass
            raise

    def atomic_write(self, name: str, contents: bytes, *, mode: int = 0o600) -> None:
        payload = memoryview(contents)

        def write_all(fd: int) -> None:
            written = 0
            while written < len(payload):
                written += os.write(fd, payload[written:])

        self._publish(name, write_all, mode=mode)

    def atomic_copy(self, name: str, source: Path | str, *, mode: int = 0o600) -> None:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            source_status = os.fstat(source_fd)
            if not stat.S_ISREG(source_status.st_mode):
                raise ValueError("copy source must be a regular file")

            def copy_contents(destination_fd: int) -> None:
                while chunk := os.read(source_fd, 1024 * 1024):
                    view = memoryview(chunk)
                    written = 0
                    while written < len(view):
                        written += os.write(destination_fd, view[written:])

            self._publish(name, copy_contents, mode=mode)
        finally:
            os.close(source_fd)

    def sha256(self, name: str) -> str:
        fd = os.open(
            _safe_name(name),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=self._operation_fd,
        )
        digest = hashlib.sha256()
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("hash target must be a regular file")
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
        finally:
            os.close(fd)
        return digest.hexdigest()


class GateProcess:
    """A process leader whose session/process-group identity is retained."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.pgid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        try:
            waited_pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return self.returncode
        if waited_pid:
            self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, deadline: float) -> int:
        while self.poll() is None:
            time.sleep(min(0.01, _remaining(deadline)))
        assert self.returncode is not None
        return self.returncode

    def _group_has_live_members(self) -> bool:
        proc = Path("/proc")
        if proc.is_dir():
            for entry in proc.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    _, separator, remainder = (
                        (entry / "stat").read_text(encoding="ascii").rpartition(") ")
                    )
                    if not separator:
                        continue
                    fields = remainder.split()
                    if int(fields[2]) == self.pgid and fields[0] != "Z":
                        return True
                except (FileNotFoundError, PermissionError, ValueError, IndexError):
                    continue
            return False
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return False
        return True

    def _wait_group(self, deadline: float) -> bool:
        while self._group_has_live_members():
            self.poll()
            try:
                time.sleep(min(0.01, _remaining(deadline)))
            except TimeoutError:
                return False
        self.poll()
        return True

    def terminate_group(self, term_deadline: float, kill_deadline: float) -> None:
        if self._group_has_live_members():
            try:
                os.killpg(self.pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if not self._wait_group(term_deadline):
            try:
                os.killpg(self.pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not self._wait_group(kill_deadline):
                raise TimeoutError("process group survived SIGKILL deadline")
        if self.poll() is None:
            self.wait(kill_deadline)


def launch_process(
    argv: Sequence[str],
    *,
    stdio_fd: int | None = None,
    environment: Mapping[str, str] | None = None,
) -> GateProcess:
    """Launch a process in a new session with normal termination signal masks."""

    if not argv or any(not isinstance(argument, str) for argument in argv):
        raise ValueError("argv must contain strings")
    file_actions: list[tuple[int, ...]] = []
    if stdio_fd is not None:
        for destination in (0, 1, 2):
            file_actions.append((os.POSIX_SPAWN_DUP2, stdio_fd, destination))
        if stdio_fd not in (0, 1, 2):
            file_actions.append((os.POSIX_SPAWN_CLOSE, stdio_fd))
    launcher_argv = ("/usr/bin/setsid", "--", *argv)
    pid = os.posix_spawn(
        launcher_argv[0],
        launcher_argv,
        dict(os.environ if environment is None else environment),
        file_actions=file_actions,
        setsigmask=(),
        setsigdef=(signal.SIGHUP, signal.SIGTERM, signal.SIGINT, signal.SIGPIPE),
    )
    return GateProcess(pid)


class SerialConsole:
    """A capped serial transcript reader using caller-provided absolute deadlines."""

    def __init__(
        self,
        fd: int,
        *,
        process: GateProcess | None = None,
        max_bytes: int,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.fd = fd
        self.process = process
        self.max_bytes = max_bytes
        self._transcript = bytearray()
        os.set_blocking(fd, False)

    @property
    def transcript(self) -> bytes:
        return bytes(self._transcript)

    def send(self, payload: bytes, deadline: float) -> None:
        if not payload:
            raise ValueError("serial command must not be empty")
        view = memoryview(payload)
        sent = 0
        while sent < len(view):
            try:
                _, writable, _ = select.select([], [self.fd], [], _remaining(deadline))
            except TimeoutError as error:
                raise TimeoutError("serial command deadline expired") from error
            if not writable:
                continue
            try:
                sent += os.write(self.fd, view[sent:])
            except BlockingIOError:
                continue

    def _read(self, deadline: float) -> bytes | None:
        ready, _, _ = select.select([self.fd], [], [], _remaining(deadline))
        if not ready:
            return None
        try:
            return os.read(self.fd, 4096)
        except OSError as error:
            if error.errno == errno.EIO:
                return b""
            raise

    def _append(self, chunk: bytes) -> None:
        if len(self._transcript) + len(chunk) > self.max_bytes:
            raise BufferError("serial transcript exceeds byte cap")
        self._transcript.extend(chunk)

    def wait_for(self, marker: bytes, deadline: float) -> bytes:
        if not marker:
            raise ValueError("serial marker must not be empty")
        while marker not in self._transcript:
            if self.process is not None and self.process.poll() is not None:
                raise EarlyProcessExit(self.process.returncode or 0)
            try:
                chunk = self._read(deadline)
            except TimeoutError as error:
                raise TimeoutError(f"serial marker not seen: {marker!r}") from error
            if chunk is None:
                continue
            if not chunk:
                if self.process is not None and self.process.poll() is not None:
                    raise EarlyProcessExit(self.process.returncode or 0)
                raise EOFError("serial console closed before marker")
            self._append(chunk)
        return self.transcript

    def drain(self, deadline: float) -> bytes:
        initial_length = len(self._transcript)
        while True:
            try:
                chunk = self._read(deadline)
            except TimeoutError:
                break
            if chunk is None:
                continue
            if not chunk:
                break
            self._append(chunk)
        return bytes(self._transcript[initial_length:])


class HmpMonitor:
    """A bounded QEMU human-monitor client."""

    PROMPT = b"(qemu) "

    def __init__(self, connection: socket.socket, max_response_bytes: int) -> None:
        self._connection = connection
        self._max_response_bytes = max_response_bytes

    @classmethod
    def connect(
        cls,
        path: Path | str,
        deadline: float,
        *,
        max_response_bytes: int,
    ) -> HmpMonitor:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        last_error: OSError | None = None
        while True:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.setblocking(False)
            result = connection.connect_ex(str(path))
            if result in (0, errno.EINPROGRESS, errno.EAGAIN):
                if result:
                    _, writable, _ = select.select(
                        [], [connection], [], _remaining(deadline)
                    )
                    if not writable:
                        connection.close()
                        raise MonitorError("HMP connect deadline expired")
                    result = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if result == 0:
                    monitor = cls(connection, max_response_bytes)
                    try:
                        monitor._read_prompt(deadline)
                    except BaseException:
                        monitor.close()
                        raise
                    return monitor
            last_error = OSError(result, os.strerror(result))
            connection.close()
            try:
                time.sleep(min(0.005, _remaining(deadline)))
            except TimeoutError as error:
                raise MonitorError(f"HMP connect failed: {last_error}") from error

    def close(self) -> None:
        if self._connection.fileno() >= 0:
            self._connection.close()

    def _read_prompt(self, deadline: float) -> bytes:
        response = bytearray()
        while self.PROMPT not in response:
            try:
                readable, _, _ = select.select(
                    [self._connection], [], [], _remaining(deadline)
                )
            except TimeoutError as error:
                raise MonitorError("HMP prompt deadline expired") from error
            if not readable:
                continue
            chunk = self._connection.recv(4096)
            if not chunk:
                raise MonitorError("HMP closed before prompt")
            if len(response) + len(chunk) > self._max_response_bytes:
                raise MonitorError("HMP response exceeds byte cap")
            response.extend(chunk)
        return bytes(response)

    def command(self, command: str, deadline: float) -> bytes:
        if not command or "\n" in command or "\r" in command:
            raise ValueError("HMP command must be one nonempty line")
        payload = command.encode("ascii") + b"\n"
        sent = 0
        while sent < len(payload):
            try:
                _, writable, _ = select.select(
                    [], [self._connection], [], _remaining(deadline)
                )
            except TimeoutError as error:
                raise MonitorError("HMP command deadline expired") from error
            if writable:
                sent += self._connection.send(payload[sent:])
        return self._read_prompt(deadline)


class TerminationSignalState:
    """Defer one HUP/TERM until the protected lifecycle has completed."""

    SIGNALS = (signal.SIGHUP, signal.SIGTERM)

    def __init__(self) -> None:
        self.pending: int | None = None
        self._previous: dict[int, signal.Handlers] = {}

    def _handle(self, signum: int, frame: object) -> None:
        del frame
        if self.pending is None:
            self.pending = signum
            return
        os._exit(128 + signum)

    def __enter__(self) -> TerminationSignalState:
        for signum in self.SIGNALS:
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, exc_value, traceback
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        if self.pending is not None:
            raise GateTermination(self.pending)
        return False


class _ClosableMonitor(Protocol):
    def close(self) -> None: ...


class _TerminableProcess(Protocol):
    def terminate_group(self, term_deadline: float, kill_deadline: float) -> None: ...


class _DrainableSerial(Protocol):
    def drain(self, deadline: float) -> bytes: ...


def teardown_gate(
    monitor: _ClosableMonitor | None,
    process: _TerminableProcess,
    serial: _DrainableSerial,
    *,
    term_deadline: float,
    kill_deadline: float,
    drain_deadline: float,
) -> bytes:
    """Perform the frozen monitor, process-group, serial teardown order."""

    failure: BaseException | None = None
    if monitor is not None:
        try:
            monitor.close()
        except BaseException as error:
            failure = error
    try:
        process.terminate_group(term_deadline, kill_deadline)
    except BaseException as error:
        if failure is None:
            failure = error
    try:
        drained = serial.drain(drain_deadline)
    except BaseException as error:
        if failure is None:
            failure = error
        drained = b""
    if failure is not None:
        raise failure
    return drained
