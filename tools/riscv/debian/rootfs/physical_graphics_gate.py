#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Guest witness for physical keyboard, pointer, and Firefox interaction."""

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import hashlib
import http.server
import math
import os
from pathlib import Path
import re
import selectors
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit
import zlib

if Path("/usr/lib/asterinas/browser_m5_marionette_gate.py").is_file():
    sys.path.insert(0, "/usr/lib/asterinas")
    from browser_m5_marionette_gate import (  # type: ignore[import-not-found]
        _connect,
        GateError as MarionetteGateError,
    )
else:
    from tools.riscv.debian.rootfs.browser_m5_marionette_gate import (
        _connect,
        GateError as MarionetteGateError,
    )


EV_KEY = 1
EV_REL = 2
EV_ABS = 3
REL_X = 0
REL_Y = 1
ABS_X = 0
ABS_Y = 1
BTN_MISC = 0x100
BTN_LEFT = 0x110
MAX_EVENT_COUNT = 4096
MAX_SCREENSHOT_BYTES = 512 * 1024
MAX_DECODED_SCREENSHOT_BYTES = 16 * 1024 * 1024
EXPECTED_SCREENSHOT_WIDTH = 1920
EXPECTED_SCREENSHOT_HEIGHT = 1080
DEFAULT_SETUP_TIMEOUT = 300.0
MAX_SETUP_TIMEOUT = 900.0
DOCUMENT_POLL_SECONDS = 0.25
INPUT_EVENT_STRUCT = struct.Struct("=qqHHi")
NONCE_PATTERN = re.compile(r"^[0-9a-f]{16}$")
DOM_STAGES = ("waiting", "key", "pointer", "complete")
PAGE_URL = "file:///usr/share/asterinas/physical-graphics/index.html"
PAGE_PATH = Path("/usr/share/asterinas/physical-graphics/index.html")
SNAPSHOT_SCRIPT = r"""const output = document.getElementById('interaction-state');
if (output === null) return null;
try {
  return JSON.parse(output.textContent);
} catch (_error) {
  return null;
}"""
SNAPSHOT_PARAMETERS = {
    "script": SNAPSHOT_SCRIPT,
    "args": [],
    # The named sandbox uses Firefox's responsive, isolated automation realm.
    # Read the page's serialized state from the DOM because Xrays intentionally
    # hide page-defined globals such as the snapshot helper.
    "newSandbox": True,
    "sandbox": "default",
    "line": 1,
    "filename": "asterinas-physical-graphics-gate",
}
SNAPSHOT_FIELDS = {
    "cycle",
    "nonce",
    "trustedKey",
    "trustedInput",
    "trustedPointer",
    "trustedClick",
    "clickCount",
    "color",
}
FOCUS_SCRIPT = r"""if (document.URL !== arguments[0] || document.readyState !== 'complete') {
  return 'loading';
}
const input = document.querySelector('#interaction-nonce');
if (input === null) return 'missing';
input.focus();
return document.activeElement === input ? 'focused' : 'not-focused';"""
FOCUS_PARAMETERS = {
    "script": FOCUS_SCRIPT,
    "args": [],
    "newSandbox": True,
    "sandbox": "default",
    "line": 1,
    "filename": "asterinas-physical-graphics-prepare",
}
VIEWPORT_SCRIPT = "return {width: window.innerWidth, height: window.innerHeight};"
VIEWPORT_PARAMETERS = {
    "script": VIEWPORT_SCRIPT,
    "args": [],
    "newSandbox": True,
    "sandbox": "default",
    "line": 1,
    "filename": "asterinas-physical-graphics-viewport",
}


class GateError(RuntimeError):
    """The physical interaction evidence violated its bounded contract."""


class MarionetteClient(Protocol):
    def command(self, name: str, parameters: object | None = None) -> object: ...

    def set_timeout(self, timeout: float) -> None: ...

    def close(self) -> None: ...


class EventSource(Protocol):
    def drain(self) -> None: ...

    def poll(self, timeout: float) -> list[bytes]: ...

    def close(self) -> None: ...


class DomStageSource(Protocol):
    def read_title(self, timeout: float) -> str: ...


def dom_stage_title(*, nonce: str, cycle: int, stage: str) -> str:
    if NONCE_PATTERN.fullmatch(nonce) is None:
        raise GateError("physical-graphics-title-nonce")
    if type(cycle) is not int or cycle not in (1, 2, 3):
        raise GateError("physical-graphics-title-cycle")
    if stage not in DOM_STAGES:
        raise GateError("physical-graphics-title-stage")
    return f"ASTERINAS_PHYSICAL cycle={cycle} stage={stage} nonce={nonce}"


class X11WindowTitleSource:
    """Read the active Firefox window title without another content-actor RPC."""

    def __init__(self, firefox_pid: int) -> None:
        if firefox_pid <= 1:
            raise GateError("physical-graphics-title-firefox-pid")
        self._firefox_pid = firefox_pid
        self._window_id: str | None = None
        self._environment = {
            **os.environ,
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
            "XAUTHORITY": os.environ.get("XAUTHORITY", "/home/asterinas/.Xauthority"),
        }

    def _command(self, arguments: list[str], deadline: float) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GateError("physical-graphics-title-timeout")
        operation = arguments[0]
        try:
            result = subprocess.run(
                ["/usr/bin/xdotool", *arguments],
                env=self._environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=remaining,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GateError(
                f"physical-graphics-title-query:{operation}:exception="
                f"{type(error).__name__}"
            ) from error
        if result.returncode != 0:
            stderr = base64.urlsafe_b64encode(
                result.stderr.encode("utf-8", errors="replace")[:192]
            ).decode("ascii")
            raise GateError(
                f"physical-graphics-title-query:{operation}:"
                f"exit={result.returncode}:stderr={stderr}"
            )
        return result.stdout.rstrip("\n")

    def read_title(self, timeout: float) -> str:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise GateError("physical-graphics-title-timeout")
        deadline = time.monotonic() + timeout
        if self._window_id is None:
            # Matchbox does not advertise the EWMH _NET_ACTIVE_WINDOW root
            # property that `getactivewindow` requires.  XGetInputFocus is a
            # core X11 query and identifies the window receiving our hardware
            # keyboard events without depending on that optional WM contract.
            window_id = self._command(["getwindowfocus"], deadline)
            if (
                not window_id.isascii()
                or not window_id.isdecimal()
                or int(window_id) <= 0
            ):
                raise GateError("physical-graphics-title-window")
            owner = self._command(["getwindowpid", window_id], deadline)
            if owner != str(self._firefox_pid):
                raise GateError("physical-graphics-title-window-owner")
            self._window_id = window_id
        title = self._command(["getwindowname", self._window_id], deadline)
        if (
            not title
            or len(title.encode("utf-8")) > 512
            or "\r" in title
            or "\n" in title
        ):
            raise GateError("physical-graphics-title-value")
        return title


class DomStageServer:
    """Serve the test page and receive its ordered, nonce-bound DOM stages."""

    def __init__(self, *, nonce: str, cycle: int, page_path: Path = PAGE_PATH) -> None:
        if NONCE_PATTERN.fullmatch(nonce) is None:
            raise GateError("physical-graphics-stage-nonce")
        if type(cycle) is not int or cycle not in (1, 2, 3):
            raise GateError("physical-graphics-stage-cycle")
        try:
            page = page_path.read_bytes()
        except OSError as error:
            raise GateError("physical-graphics-stage-page") from error
        if not page.startswith(b"<!doctype html>") or len(page) > 128 * 1024:
            raise GateError("physical-graphics-stage-page")
        self._nonce = nonce
        self._cycle = cycle
        self._page = page
        self._condition = threading.Condition()
        self._stage = "waiting"
        self._failure: str | None = None
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> DomStageServer:
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                owner._handle_get(self)

            def log_message(self, _format: str, *_arguments: object) -> None:
                return None

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(
                self, request: object, client_address: tuple[str, int]
            ) -> None:
                del request, client_address
                owner._fail("physical-graphics-stage-server")

        try:
            server = Server(("127.0.0.1", 0), Handler)
        except OSError as error:
            raise GateError("physical-graphics-stage-listen") from error
        thread = threading.Thread(
            target=server.serve_forever,
            name="physical-graphics-stage-server",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        thread.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)
            if thread.is_alive():
                raise GateError("physical-graphics-stage-server-stop")

    @property
    def origin(self) -> str:
        if self._server is None:
            raise GateError("physical-graphics-stage-not-running")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def page_url(self) -> str:
        return f"{self.origin}/index.html?cycle={self._cycle}&nonce_length=16"

    @staticmethod
    def _respond(
        request: http.server.BaseHTTPRequestHandler,
        status: int,
        body: bytes = b"",
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        request.send_response(status)
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Content-Type", content_type)
        request.send_header("Cache-Control", "no-store")
        request.send_header("Connection", "close")
        request.end_headers()
        request.close_connection = True
        if body:
            request.wfile.write(body)

    def _fail(self, reason: str) -> None:
        with self._condition:
            if self._failure is None:
                self._failure = reason
            self._condition.notify_all()

    def _handle_get(self, request: http.server.BaseHTTPRequestHandler) -> None:
        if request.client_address[0] != "127.0.0.1":
            self._respond(request, 403)
            return
        target = urlsplit(request.path)
        if target.path == "/favicon.ico" and not target.query:
            self._respond(request, 204)
            return
        expected_page_query = f"cycle={self._cycle}&nonce_length=16"
        if target.path == "/index.html" and target.query == expected_page_query:
            self._respond(request, 200, self._page, "text/html; charset=utf-8")
            return
        if target.path != "/stage":
            self._respond(request, 404)
            return
        try:
            query = parse_qs(target.query, strict_parsing=True)
        except ValueError:
            self._respond(request, 400)
            return
        if set(query) != {"cycle", "stage", "nonce"} or any(
            len(values) != 1 for values in query.values()
        ):
            self._respond(request, 400)
            return
        stage = query["stage"][0]
        if (
            query["cycle"] != [str(self._cycle)]
            or query["nonce"] != [self._nonce]
            or stage not in DOM_STAGES[1:]
        ):
            self._respond(request, 400)
            return
        with self._condition:
            current = DOM_STAGES.index(self._stage)
            received = DOM_STAGES.index(stage)
            if received not in (current, current + 1):
                if self._failure is None:
                    self._failure = "physical-graphics-stage-regression"
                self._condition.notify_all()
                status = 409
            else:
                self._stage = stage
                self._condition.notify_all()
                status = 204
        self._respond(request, status)

    def read_title(self, timeout: float) -> str:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise GateError("physical-graphics-stage-timeout")
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._stage == "waiting" and self._failure is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            if self._failure is not None:
                raise GateError(self._failure)
            stage = self._stage
        return dom_stage_title(nonce=self._nonce, cycle=self._cycle, stage=stage)


def _script_value(response: object) -> object:
    if isinstance(response, dict) and set(response) == {"value"}:
        return response["value"]
    return response


def validate_snapshot(
    snapshot: object, *, expected_nonce: str, cycle: int
) -> dict[str, object]:
    """Require one exact completed DOM interaction state."""

    if not isinstance(snapshot, dict) or set(snapshot) != SNAPSHOT_FIELDS:
        raise GateError("physical-graphics-snapshot-fields")
    if snapshot["cycle"] != cycle or type(snapshot["cycle"]) is not int:
        raise GateError("physical-graphics-snapshot-cycle")
    if snapshot["nonce"] != expected_nonce:
        raise GateError("physical-graphics-snapshot-nonce")
    for name in ("trustedKey", "trustedInput", "trustedPointer", "trustedClick"):
        if snapshot[name] is not True:
            raise GateError(f"physical-graphics-snapshot-{name}")
    if type(snapshot["clickCount"]) is not int or snapshot["clickCount"] != 1:
        raise GateError("physical-graphics-snapshot-click-count")
    if snapshot["color"] != "cyan":
        raise GateError("physical-graphics-snapshot-color")
    return snapshot


def snapshot_complete(snapshot: object, *, cycle: int) -> bool:
    """Validate one in-progress page state and report terminal cyan state."""

    if not isinstance(snapshot, dict) or set(snapshot) != SNAPSHOT_FIELDS:
        raise GateError("physical-graphics-snapshot-fields")
    if type(snapshot["cycle"]) is not int or snapshot["cycle"] != cycle:
        raise GateError("physical-graphics-snapshot-cycle")
    nonce = snapshot["nonce"]
    if (
        not isinstance(nonce, str)
        or len(nonce) > 16
        or (nonce and re.fullmatch(r"[0-9a-f]+", nonce) is None)
    ):
        raise GateError("physical-graphics-snapshot-nonce-shape")
    for name in ("trustedKey", "trustedInput", "trustedPointer", "trustedClick"):
        if type(snapshot[name]) is not bool:
            raise GateError(f"physical-graphics-snapshot-{name}-type")
    click_count = snapshot["clickCount"]
    color = snapshot["color"]
    if type(click_count) is not int or click_count not in (0, 1):
        raise GateError("physical-graphics-snapshot-click-count")
    if color not in ("amber", "cyan"):
        raise GateError("physical-graphics-snapshot-color")
    if (click_count == 0) != (color == "amber"):
        raise GateError("physical-graphics-snapshot-state-disagreement")
    if snapshot["trustedClick"] is not (click_count == 1):
        raise GateError("physical-graphics-snapshot-click-disagreement")
    return (
        click_count == 1
        and color == "cyan"
        and all(
            snapshot[name] is True
            for name in (
                "trustedKey",
                "trustedInput",
                "trustedPointer",
                "trustedClick",
            )
        )
    )


class GuardedMarionette:
    """Forbid every browser-side input-synthesis command after operator READY."""

    def __init__(self, client: MarionetteClient) -> None:
        self._client = client
        self._ready = False

    def mark_ready(self) -> None:
        self._ready = True

    def command(self, name: str, parameters: object | None = None) -> object:
        if self._ready:
            allowed = (
                name == "WebDriver:ExecuteScript" and parameters == SNAPSHOT_PARAMETERS
            ) or (name == "WebDriver:TakeScreenshot" and parameters == {"full": False})
            if not allowed:
                raise GateError("marionette-input-synthesis-after-ready")
        return self._client.command(name, parameters)

    def snapshot(self) -> object:
        return _script_value(
            self.command("WebDriver:ExecuteScript", dict(SNAPSHOT_PARAMETERS))
        )

    def screenshot(self) -> object:
        return _script_value(self.command("WebDriver:TakeScreenshot", {"full": False}))


@dataclass(frozen=True)
class CycleEvidence:
    cycle: int
    nonce_sha256: str
    key_downs: int
    relative_events: int
    absolute_events: int
    evdev_sha256: str
    screenshot_sha256: str


class RealEvdevSource:
    """Read complete input-event records from every current evdev node."""

    def __init__(self, directory: Path) -> None:
        try:
            nodes = sorted(directory.glob("event*"))
        except OSError as error:
            raise GateError("evdev-directory-unreadable") from error
        if not nodes:
            raise GateError("evdev-nodes-missing")
        self._selector = selectors.DefaultSelector()
        self._buffers: dict[int, bytearray] = {}
        try:
            for node in nodes:
                metadata = node.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise GateError("evdev-node-symlink")
                flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(node, flags)
                descriptor_metadata = os.fstat(descriptor)
                if not stat.S_ISCHR(descriptor_metadata.st_mode):
                    os.close(descriptor)
                    raise GateError("evdev-node-not-character-device")
                self._selector.register(descriptor, selectors.EVENT_READ)
                self._buffers[descriptor] = bytearray()
        except BaseException:
            self.close()
            raise

    def poll(self, timeout: float) -> list[bytes]:
        records: list[bytes] = []
        for key, _mask in self._selector.select(timeout):
            descriptor = int(key.fd)
            try:
                payload = os.read(descriptor, INPUT_EVENT_STRUCT.size * 64)
            except BlockingIOError:
                continue
            if not payload:
                raise GateError("evdev-device-disappeared")
            buffer = self._buffers[descriptor]
            buffer.extend(payload)
            if len(buffer) > INPUT_EVENT_STRUCT.size * 65:
                raise GateError("evdev-buffer-overflow")
            while len(buffer) >= INPUT_EVENT_STRUCT.size:
                records.append(bytes(buffer[: INPUT_EVENT_STRUCT.size]))
                del buffer[: INPUT_EVENT_STRUCT.size]
        return records

    def drain(self) -> None:
        drained = 0
        while True:
            records = self.poll(0.0)
            if not records:
                return
            drained += len(records)
            if drained > MAX_EVENT_COUNT:
                raise GateError("evdev-drain-overflow")

    def close(self) -> None:
        selector = getattr(self, "_selector", None)
        if selector is None:
            return
        for key in list(selector.get_map().values()):
            try:
                selector.unregister(key.fd)
            except (KeyError, ValueError):
                pass
            os.close(key.fd)
        selector.close()
        self._selector = None  # type: ignore[assignment]


@dataclass(frozen=True)
class InputEvent:
    """One Linux riscv64 `struct input_event` record."""

    seconds: int
    microseconds: int
    event_type: int
    code: int
    value: int

    def encode(self) -> bytes:
        return INPUT_EVENT_STRUCT.pack(
            self.seconds,
            self.microseconds,
            self.event_type,
            self.code,
            self.value,
        )


@dataclass
class EvdevCycle:
    """Bounded raw-input evidence collected after one operator READY marker."""

    key_downs: int = 0
    relative_events: int = 0
    absolute_events: int = 0
    left_down: int = 0
    left_up: int = 0
    _digest: Any = field(default_factory=hashlib.sha256, repr=False)

    @staticmethod
    def _increment(value: int) -> int:
        if value >= MAX_EVENT_COUNT:
            raise GateError("evdev-counter-overflow")
        return value + 1

    @property
    def left_click_complete(self) -> bool:
        return self.left_down == 1 and self.left_up == 1

    @property
    def digest(self) -> str:
        return self._digest.hexdigest()

    def feed_record(self, payload: bytes) -> None:
        if len(payload) != INPUT_EVENT_STRUCT.size:
            raise GateError("truncated-evdev-record")
        self.feed(InputEvent(*INPUT_EVENT_STRUCT.unpack(payload)))

    def feed(self, event: InputEvent) -> None:
        encoded = event.encode()
        if event.event_type == EV_KEY and event.code < BTN_MISC and event.value == 1:
            self.key_downs = self._increment(self.key_downs)
        elif (
            event.event_type == EV_REL
            and event.code in (REL_X, REL_Y)
            and event.value != 0
        ):
            self.relative_events = self._increment(self.relative_events)
        elif event.event_type == EV_ABS and event.code in (ABS_X, ABS_Y):
            self.absolute_events = self._increment(self.absolute_events)
        elif event.event_type == EV_KEY and event.code == BTN_LEFT:
            if event.value == 1:
                if self.left_down != 0 or self.left_up != 0:
                    raise GateError("evdev-duplicate-button-down")
                self.left_down = 1
            elif event.value == 0:
                if self.left_down != 1:
                    raise GateError("evdev-button-up-before-down")
                if self.left_up != 0:
                    raise GateError("evdev-duplicate-button-up")
                self.left_up = 1
        self._digest.update(encoded)


def validate_firefox_namespace(firefox_pid: int) -> None:
    """Require the witness to share the selected online Firefox network stack."""

    if firefox_pid <= 1:
        raise GateError("Firefox PID is outside the valid contract")
    try:
        gate_namespace = os.readlink("/proc/self/ns/net")
        firefox_namespace = os.readlink(f"/proc/{firefox_pid}/ns/net")
        interfaces = [name for _, name in socket.if_nameindex()]
    except OSError as error:
        raise GateError("cannot inspect Firefox network namespace") from error
    if gate_namespace != firefox_namespace:
        raise GateError("physical gate did not join the Firefox network namespace")
    if (
        "lo" not in interfaces
        or len([name for name in interfaces if name != "lo"]) != 1
    ):
        raise GateError("physical gate does not have exactly one non-loopback NIC")


def validate_png_screenshot(
    payload: bytes,
    *,
    max_bytes: int = MAX_SCREENSHOT_BYTES,
    expected_dimensions: tuple[int, int] | None = (
        EXPECTED_SCREENSHOT_WIDTH,
        EXPECTED_SCREENSHOT_HEIGHT,
    ),
) -> None:
    """Require one complete, bounded, non-interlaced PNG."""

    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise GateError("screenshot-not-png")
    if (
        type(max_bytes) is not int
        or not 8 < max_bytes <= 64 * 1024 * 1024
        or not 8 < len(payload) <= max_bytes
    ):
        raise GateError("screenshot-size-invalid")
    offset = 8
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(payload):
        if offset + 12 > len(payload):
            raise GateError("screenshot-chunk-truncated")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        end = offset + 12 + length
        if length > MAX_DECODED_SCREENSHOT_BYTES or end > len(payload):
            raise GateError("screenshot-chunk-invalid")
        contents = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length : end])[0]
        if zlib.crc32(kind + contents) & 0xFFFFFFFF != expected_crc:
            raise GateError("screenshot-chunk-crc-invalid")
        chunks.append((kind, contents))
        offset = end
        if kind == b"IEND":
            break
    if (
        offset != len(payload)
        or not chunks
        or chunks[0][0] != b"IHDR"
        or chunks[-1] != (b"IEND", b"")
        or sum(kind == b"IHDR" for kind, _ in chunks) != 1
        or not any(kind == b"IDAT" for kind, _ in chunks)
    ):
        raise GateError("screenshot-structure-invalid")
    ihdr = chunks[0][1]
    if len(ihdr) != 13:
        raise GateError("screenshot-ihdr-invalid")
    width, height, depth, color_type, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    if (
        not width
        or not height
        or width > 16384
        or height > 16384
        or (expected_dimensions is not None and (width, height) != expected_dimensions)
    ):
        raise GateError("screenshot-dimensions-invalid")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None or depth != 8 or compression or filtering or interlace:
        raise GateError("screenshot-format-unsupported")
    expected_size = height * (1 + width * channels)
    if expected_size > MAX_DECODED_SCREENSHOT_BYTES:
        raise GateError("screenshot-decoded-size-excessive")
    compressed = b"".join(contents for kind, contents in chunks if kind == b"IDAT")
    try:
        decompressor = zlib.decompressobj()
        pixels = decompressor.decompress(compressed, expected_size + 1)
    except zlib.error as error:
        raise GateError("screenshot-pixels-invalid") from error
    if (
        len(pixels) != expected_size
        or decompressor.unconsumed_tail
        or decompressor.unused_data
        or not decompressor.eof
    ):
        raise GateError("screenshot-pixel-size-invalid")
    row_size = 1 + width * channels
    if any(pixels[row * row_size] > 4 for row in range(height)):
        raise GateError("screenshot-filter-invalid")


def _emit_screenshot_frame(
    emit: Callable[[str], None], *, cycle: int, payload: bytes, sha256: str
) -> None:
    emit(
        f"__ASTERINAS_PHYSICAL_SCREENSHOT_BEGIN__ cycle={cycle} "
        f"size={len(payload)} sha256={sha256}"
    )
    emit(base64.b64encode(payload).decode("ascii"))
    emit(f"__ASTERINAS_PHYSICAL_SCREENSHOT_END__ cycle={cycle}")


def run_cycle(
    client: MarionetteClient,
    events: EventSource,
    stages: DomStageSource,
    *,
    nonce: str,
    cycle: int,
    timeout: float,
    expected_width: int = EXPECTED_SCREENSHOT_WIDTH,
    expected_height: int = EXPECTED_SCREENSHOT_HEIGHT,
    page_url: str | None = None,
    emit: Callable[[str], None],
) -> CycleEvidence:
    """Observe one correlated physical interaction cycle without input synthesis."""

    if NONCE_PATTERN.fullmatch(nonce) is None:
        raise GateError("physical-graphics-nonce-invalid")
    if type(cycle) is not int or cycle not in (1, 2, 3):
        raise GateError("physical-graphics-cycle-invalid")
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise GateError("physical-graphics-timeout-invalid")
    if (
        type(expected_width) is not int
        or type(expected_height) is not int
        or not 0 < expected_width <= 16384
        or not 0 < expected_height <= 16384
    ):
        raise GateError("physical-graphics-expected-dimensions-invalid")

    guarded = GuardedMarionette(client)
    if page_url is None:
        page_url = f"{PAGE_URL}?cycle={cycle}&nonce_length=16"
    elif (
        not isinstance(page_url, str)
        or len(page_url) > 512
        or not page_url.startswith("http://127.0.0.1:")
    ):
        raise GateError("physical-graphics-page-url")

    def setup_command(name: str, parameters: object | None = None) -> object:
        emit(f"ASTERINAS_PHYSICAL_SETUP cycle={cycle} phase={name} state=start")
        result = guarded.command(name, parameters)
        emit(f"ASTERINAS_PHYSICAL_SETUP cycle={cycle} phase={name} state=done")
        return result

    try:
        session = setup_command(
            "WebDriver:NewSession",
            {
                "pageLoadStrategy": "none",
                "strictFileInteractability": True,
            },
        )
        if not isinstance(session, dict) or not isinstance(
            session.get("sessionId"), str
        ):
            raise GateError("physical-graphics-marionette-session")
        handles = _script_value(setup_command("WebDriver:GetWindowHandles"))
        if (
            not isinstance(handles, list)
            or not handles
            or len(handles) > 16
            or not all(isinstance(handle, str) and handle for handle in handles)
            or len(set(handles)) != len(handles)
        ):
            raise GateError("physical-graphics-browser-window")
        if (
            _script_value(setup_command("WebDriver:Navigate", {"url": page_url}))
            is not None
        ):
            raise GateError("physical-graphics-navigation-result")
        # Navigation is nonblocking. Firefox may still expose the old document,
        # or return null when its content actor is replaced during navigation.
        # Keep the client's original setup deadline; only READY starts a new
        # interaction budget. The local ceiling also bounds responsive clients.
        focus_deadline = time.monotonic() + MAX_SETUP_TIMEOUT
        focus_parameters = dict(FOCUS_PARAMETERS, args=[page_url])
        while True:
            focus = _script_value(
                setup_command("WebDriver:ExecuteScript", focus_parameters)
            )
            if focus == "focused":
                break
            if focus is not None and focus != "loading":
                raise GateError("physical-graphics-input-focus")
            remaining = focus_deadline - time.monotonic()
            if remaining <= 0:
                raise GateError("physical-graphics-document-timeout")
            time.sleep(min(DOCUMENT_POLL_SECONDS, remaining))
        # Matchbox normally places Firefox at the exact screen geometry before
        # this gate starts.  Avoid FullscreenWindow in that common case: under
        # RISC-V TCG the redundant native WM round trip can take over four
        # minutes.  Keep it as a bounded fallback for an actually wrong window.
        window = _script_value(setup_command("WebDriver:GetWindowRect"))
        if not isinstance(window, dict):
            raise GateError("physical-graphics-window-rect")
        outer_matches = (window.get("width"), window.get("height")) == (
            expected_width,
            expected_height,
        )
        viewport: object = None
        if outer_matches:
            viewport = _script_value(
                setup_command("WebDriver:ExecuteScript", VIEWPORT_PARAMETERS)
            )
        viewport_matches = isinstance(viewport, dict) and (
            viewport.get("width"),
            viewport.get("height"),
        ) == (expected_width, expected_height)
        if not (outer_matches and viewport_matches):
            window = _script_value(setup_command("WebDriver:FullscreenWindow"))
        if (
            not isinstance(window, dict)
            or window.get("width") != expected_width
            or window.get("height") != expected_height
        ):
            raise GateError("physical-graphics-fullscreen-dimensions")

        events.drain()
        nonce_sha256 = hashlib.sha256(nonce.encode()).hexdigest()
        client.set_timeout(timeout)
        emit(
            f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle={cycle} "
            f"nonce_sha256={nonce_sha256}"
        )
        guarded.mark_ready()
        deadline = time.monotonic() + timeout
        input_evidence = EvdevCycle()
        key_ready = False
        pointer_ready = False
        key_titles = {
            dom_stage_title(nonce=nonce, cycle=cycle, stage=stage)
            for stage in ("key", "pointer", "complete")
        }
        pointer_titles = {
            dom_stage_title(nonce=nonce, cycle=cycle, stage=stage)
            for stage in ("pointer", "complete")
        }
        complete_title = dom_stage_title(nonce=nonce, cycle=cycle, stage="complete")
        last_title = dom_stage_title(nonce=nonce, cycle=cycle, stage="waiting")

        def cycle_timeout() -> GateError:
            stage = next(
                (
                    candidate
                    for candidate in DOM_STAGES
                    if last_title
                    == dom_stage_title(nonce=nonce, cycle=cycle, stage=candidate)
                ),
                "unknown",
            )
            return GateError(
                "physical-graphics-cycle-timeout:"
                f"key_ready={int(key_ready)}:pointer_ready={int(pointer_ready)}:"
                f"key_downs={input_evidence.key_downs}:"
                f"relative_events={input_evidence.relative_events}:"
                f"absolute_events={input_evidence.absolute_events}:"
                f"left_down={input_evidence.left_down}:"
                f"left_up={input_evidence.left_up}:stage={stage}"
            )

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise cycle_timeout()
            for record in events.poll(min(0.05, remaining)):
                input_evidence.feed_record(record)
            title: str | None = None
            if input_evidence.key_downs >= len(nonce):
                title = stages.read_title(min(5.0, remaining))
                last_title = title
            if (
                not key_ready
                and input_evidence.key_downs >= len(nonce)
                and title in key_titles
            ):
                key_ready = True
                emit(
                    f"ASTERINAS_PHYSICAL_GRAPHICS_KEY_READY cycle={cycle} "
                    f"nonce_sha256={nonce_sha256}"
                )
            if (
                key_ready
                and not pointer_ready
                and (
                    input_evidence.relative_events >= 1
                    or input_evidence.absolute_events >= 1
                )
                and title in pointer_titles
            ):
                pointer_ready = True
                emit(f"ASTERINAS_PHYSICAL_GRAPHICS_POINTER_READY cycle={cycle}")
            if not (
                pointer_ready
                and input_evidence.left_click_complete
                and title == complete_title
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise cycle_timeout()
                time.sleep(min(DOCUMENT_POLL_SECONDS, remaining))
                continue

            snapshot = guarded.snapshot()
            dom_complete = snapshot_complete(snapshot, cycle=cycle)
            input_complete = (
                input_evidence.key_downs >= len(nonce)
                and (
                    input_evidence.relative_events >= 1
                    or input_evidence.absolute_events >= 1
                )
                and input_evidence.left_click_complete
            )
            if not (dom_complete and input_complete):
                raise GateError("physical-graphics-title-snapshot-disagreement")
            validate_snapshot(snapshot, expected_nonce=nonce, cycle=cycle)
            encoded = guarded.screenshot()
            if not isinstance(encoded, str):
                raise GateError("physical-graphics-screenshot-response")
            try:
                screenshot_payload = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as error:
                raise GateError("physical-graphics-screenshot-base64") from error
            validate_png_screenshot(
                screenshot_payload,
                expected_dimensions=(expected_width, expected_height),
            )
            screenshot_sha256 = hashlib.sha256(screenshot_payload).hexdigest()
            emit(
                f"ASTERINAS_PHYSICAL_GRAPHICS_INPUT cycle={cycle} "
                f"key_downs={input_evidence.key_downs} "
                f"relative_events={input_evidence.relative_events} "
                f"absolute_events={input_evidence.absolute_events} "
                f"left_down={input_evidence.left_down} "
                f"left_up={input_evidence.left_up} "
                f"digest={input_evidence.digest}"
            )
            emit(
                f"ASTERINAS_PHYSICAL_GRAPHICS_DOM cycle={cycle} "
                f"nonce_sha256={nonce_sha256} trusted_key=1 trusted_input=1 "
                "trusted_pointer=1 trusted_click=1 click_count=1 color=cyan"
            )
            emit(
                f"ASTERINAS_PHYSICAL_GRAPHICS_SCREENSHOT cycle={cycle} "
                f"sha256={screenshot_sha256}"
            )
            _emit_screenshot_frame(
                emit,
                cycle=cycle,
                payload=screenshot_payload,
                sha256=screenshot_sha256,
            )
            emit(f"ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle={cycle}")
            return CycleEvidence(
                cycle=cycle,
                nonce_sha256=nonce_sha256,
                key_downs=input_evidence.key_downs,
                relative_events=input_evidence.relative_events,
                absolute_events=input_evidence.absolute_events,
                evdev_sha256=input_evidence.digest,
                screenshot_sha256=screenshot_sha256,
            )
    finally:
        events.close()


def verify_final_state(client: MarionetteClient, *, nonce: str, cycle: int) -> None:
    """Recheck the terminal DOM without navigating or synthesizing input."""

    if cycle != 3 or NONCE_PATTERN.fullmatch(nonce) is None:
        raise GateError("physical-graphics-final-identity")
    guarded = GuardedMarionette(client)
    session = guarded.command(
        "WebDriver:NewSession", {"strictFileInteractability": True}
    )
    if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str):
        raise GateError("physical-graphics-final-marionette-session")
    guarded.mark_ready()
    validate_snapshot(guarded.snapshot(), expected_nonce=nonce, cycle=cycle)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="physical-graphics-gate")
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--cycle", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--firefox-pid", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--setup-timeout", type=float, default=DEFAULT_SETUP_TIMEOUT)
    parser.add_argument("--expected-width", type=int, default=EXPECTED_SCREENSHOT_WIDTH)
    parser.add_argument(
        "--expected-height", type=int, default=EXPECTED_SCREENSHOT_HEIGHT
    )
    parser.add_argument("--port", type=int, default=2828)
    parser.add_argument("--input-directory", type=Path, default=Path("/dev/input"))
    parser.add_argument("--verify-final", action="store_true")
    values = parser.parse_args(arguments)
    if (
        NONCE_PATTERN.fullmatch(values.nonce) is None
        or values.firefox_pid <= 1
        or not 1 <= values.port <= 65535
        or not math.isfinite(values.timeout)
        or not 0 < values.timeout <= 300
        or not math.isfinite(values.setup_timeout)
        or not 0 < values.setup_timeout <= MAX_SETUP_TIMEOUT
        or not 0 < values.expected_width <= 16384
        or not 0 < values.expected_height <= 16384
    ):
        parser.error("physical graphics arguments are outside the bounded contract")

    client: MarionetteClient | None = None
    try:
        validate_firefox_namespace(values.firefox_pid)
        setup_deadline = time.monotonic() + values.setup_timeout
        client = _connect("127.0.0.1", values.port, setup_deadline)
        if values.verify_final:
            verify_final_state(client, nonce=values.nonce, cycle=values.cycle)
            print(
                f"__ASTERINAS_PHYSICAL_FINAL__ cycle={values.cycle} "
                f"nonce_sha256={hashlib.sha256(values.nonce.encode()).hexdigest()}",
                flush=True,
            )
        else:
            with DomStageServer(nonce=values.nonce, cycle=values.cycle) as stages:
                run_cycle(
                    client,
                    RealEvdevSource(values.input_directory),
                    stages,
                    nonce=values.nonce,
                    cycle=values.cycle,
                    timeout=values.timeout,
                    expected_width=values.expected_width,
                    expected_height=values.expected_height,
                    page_url=stages.page_url,
                    emit=lambda marker: print(marker, flush=True),
                )
    except (GateError, MarionetteGateError, OSError, TimeoutError) as error:
        print(
            f"ASTERINAS_PHYSICAL_GRAPHICS_FAIL cycle={values.cycle} "
            f"reason={str(error)}",
            flush=True,
        )
        return 1
    finally:
        if client is not None:
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
