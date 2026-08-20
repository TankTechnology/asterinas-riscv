"""A deliberately fixed QMP client for capturing one screendump."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import socket
import stat
import time

from tools.riscv.qemu_uboot_devices import BOCHS_XRGB8888


_MAX_LINE = 64 * 1024
_PPM_HEADER = f"P6\n{BOCHS_XRGB8888.width} {BOCHS_XRGB8888.height}\n255\n".encode()
_MAX_CAPTURE_BYTES = len(_PPM_HEADER) + BOCHS_XRGB8888.width * BOCHS_XRGB8888.height * 3
_OPEN_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_OPEN_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _require_path(value: Path, name: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ValueError(f"{name} must be an absolute Path")
    if ".." in value.parts:
        raise ValueError(f"{name} must not contain parent traversal")
    return value


def _reject_symlink_components(path: Path, root: Path, *, allow_missing_leaf: bool) -> None:
    relative = path.relative_to(root)
    current = root
    for index, part in enumerate(relative.parts):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and index == len(relative.parts) - 1:
                return
            raise ValueError(f"{path} has a missing intermediate component") from None
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"{path} contains a symlink")


def _validate_paths(
    socket_path: Path,
    output_path: Path,
    capture_root: Path,
    *,
    validate_socket: bool = True,
) -> None:
    root = _require_path(capture_root, "capture_root")
    socket_candidate = _require_path(socket_path, "socket_path")
    output_candidate = _require_path(output_path, "output_path")
    if "," in os.fspath(socket_candidate):
        raise ValueError("socket_path must not contain a comma")
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("capture_root must be a non-symlink directory")
    if stat.S_IMODE(root_info.st_mode) != 0o700:
        raise ValueError("capture_root must use private mode 0700")
    resolved_root = root.resolve(strict=True)
    for candidate, name in ((socket_candidate, "socket_path"), (output_candidate, "output_path")):
        try:
            candidate.relative_to(root)
            resolved_candidate = candidate.resolve(strict=False)
            resolved_candidate.relative_to(resolved_root)
        except ValueError:
            raise ValueError(f"{name} must be strictly below capture_root") from None
        if resolved_candidate == resolved_root:
            raise ValueError(f"{name} must not equal capture_root")
    if validate_socket:
        _reject_symlink_components(socket_candidate, root, allow_missing_leaf=False)
    _reject_symlink_components(output_candidate, root, allow_missing_leaf=True)
    if validate_socket:
        socket_info = socket_candidate.lstat()
        if not stat.S_ISSOCK(socket_info.st_mode):
            raise ValueError("socket_path must name an existing Unix socket")
    try:
        output_info = output_candidate.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(output_info.st_mode) or not stat.S_ISREG(output_info.st_mode):
        raise ValueError("output_path must be absent or a regular file")


def _reject_json_constant(_: str) -> object:
    raise ValueError("QMP response contains a non-standard JSON constant")


def _remaining_timeout(connection: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("QMP capture timed out")
    connection.settimeout(remaining)


def _read_message(connection: socket.socket, deadline: float) -> dict[str, object]:
    data = bytearray()
    while True:
        if len(data) >= _MAX_LINE:
            raise ValueError("QMP response line is too long")
        _remaining_timeout(connection, deadline)
        try:
            chunk = connection.recv(1)
        except socket.timeout as error:
            raise TimeoutError("QMP capture timed out") from error
        if time.monotonic() >= deadline:
            raise TimeoutError("QMP capture timed out")
        if not chunk:
            raise ValueError("QMP closed before a complete response")
        data.extend(chunk)
        if chunk == b"\n":
            break
    try:
        text = data[:-1].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("QMP response is not UTF-8") from error
    try:
        message = json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as error:
        raise ValueError("QMP response is invalid JSON") from error
    except RecursionError as error:
        raise ValueError("QMP response exceeds JSON nesting limit") from error
    except ValueError as error:
        raise ValueError("QMP response contains a forbidden JSON constant") from error
    if not isinstance(message, dict):
        raise ValueError("QMP response must be an object")
    return message


def _require_success(connection: socket.socket, deadline: float) -> None:
    response = _read_message(connection, deadline)
    if "error" in response or "event" in response or "return" not in response:
        raise ValueError("QMP command did not return a success object")


def _send_command(connection: socket.socket, command: dict[str, object], deadline: float) -> None:
    _remaining_timeout(connection, deadline)
    try:
        connection.sendall(json.dumps(command, separators=(",", ":")).encode("utf-8") + b"\n")
    except socket.timeout as error:
        raise TimeoutError("QMP capture timed out") from error
    _remaining_timeout(connection, deadline)


def _open_capture_directories(output_path: Path, capture_root: Path) -> tuple[int, int, str]:
    root = _require_path(capture_root, "capture_root")
    output = _require_path(output_path, "output_path")
    relative = output.relative_to(root)
    root_descriptor = os.open(root, _OPEN_DIRECTORY_FLAGS)
    parent_descriptor = -1
    try:
        root_info = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_IMODE(root_info.st_mode) != 0o700:
            raise ValueError("capture_root must be a private directory")
        parent_descriptor = os.dup(root_descriptor)
        for component in relative.parts[:-1]:
            next_descriptor = os.open(component, _OPEN_DIRECTORY_FLAGS, dir_fd=parent_descriptor)
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
        return root_descriptor, parent_descriptor, relative.name
    except OSError as error:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        os.close(root_descriptor)
        raise ValueError("capture output parent is unavailable") from error
    except Exception:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        os.close(root_descriptor)
        raise


def _read_output(parent_descriptor: int, filename: str) -> bytes:
    try:
        descriptor = os.open(filename, _OPEN_FILE_FLAGS, dir_fd=parent_descriptor)
    except OSError as error:
        raise ValueError("QMP screendump output is unavailable") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("QMP screendump output is not a regular file")
        payload = os.read(descriptor, _MAX_CAPTURE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > _MAX_CAPTURE_BYTES:
        raise ValueError("QMP screendump output exceeds the registered display limit")
    return payload


def capture_screendump(
    socket_path: Path,
    output_path: Path,
    *,
    capture_root: Path,
    timeout: float = 5.0,
) -> bytes:
    """Issue QMP's fixed capability/screendump exchange and read its PPM output."""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    _validate_paths(socket_path, output_path, capture_root)
    root_descriptor, parent_descriptor, filename = _open_capture_directories(output_path, capture_root)
    try:
        deadline = time.monotonic() + timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            _remaining_timeout(connection, deadline)
            try:
                connection.connect(os.fspath(socket_path))
            except socket.timeout as error:
                raise TimeoutError("QMP capture timed out") from error
            greeting = _read_message(connection, deadline)
            if "QMP" not in greeting:
                raise ValueError("QMP greeting is missing QMP")
            _send_command(connection, {"execute": "qmp_capabilities"}, deadline)
            _require_success(connection, deadline)
            _send_command(
                connection,
                {"execute": "screendump", "arguments": {"filename": os.fspath(output_path)}},
                deadline,
            )
            _require_success(connection, deadline)
        return _read_output(parent_descriptor, filename)
    finally:
        os.close(parent_descriptor)
        os.close(root_descriptor)
