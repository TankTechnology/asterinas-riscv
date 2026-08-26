#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded Marionette content gate for the offline Firefox workload."""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import socket
import time
from collections.abc import Sequence


PROBE_URL = "file:///usr/share/asterinas/browser-m5/index.html"
VIDEO_URL = "file:///usr/share/asterinas/browser-m5/browser-m5.webm"
EXPECTED_MARKERS = (
    ("js-result", "ASTERINAS_BROWSER_M5_JS_PASS"),
    ("video-canplay-result", "ASTERINAS_BROWSER_M5_VIDEO_CANPLAY"),
    ("video-ended-result", "ASTERINAS_BROWSER_M5_VIDEO_ENDED"),
)
PENDING_MARKERS = (
    ("js-result", "JavaScript pending"),
    ("video-canplay-result", "Local silent video pending"),
    ("video-ended-result", "Local silent video completion pending"),
)
PASS_LINE = "DEBIAN_BROWSER_M5_CONTENT js=pass media=vp8-webm canplay=pass ended=pass network_mode=private-loopback source=file direct_nonloopback_ip=unavailable"
MAX_MESSAGE_BYTES = 1024 * 1024

_EXPRESSION = r"""return JSON.stringify({
  url: location.href,
  markers: Array.from(document.querySelectorAll(
    '#js-result, #video-canplay-result, #video-ended-result'
  )).map(node => [node.id, node.textContent]),
  media: (() => {
    const video = document.querySelector('video');
    return video === null ? null : {
      currentSrc: video.currentSrc,
      ended: video.ended,
      readyState: video.readyState,
      error: video.error === null ? null : video.error.code,
      duration: Number.isFinite(video.duration) ? video.duration : null,
      currentTime: Number.isFinite(video.currentTime) ? video.currentTime : null
    };
  })(),
  resources: performance.getEntriesByType('resource').map(entry => entry.name)
});"""


class GateError(RuntimeError):
    """The browser did not provide exact, trustworthy content evidence."""


class Marionette:
    def __init__(self, host: str, port: int, timeout: float) -> None:
        if host not in {"127.0.0.1", "::1"}:
            raise GateError("Marionette endpoint must be loopback")
        self._deadline = time.monotonic() + timeout
        self._socket = socket.create_connection((host, port), timeout=timeout)
        self._next_id = 1
        try:
            hello = self._receive()
            if hello != {"applicationType": "gecko", "marionetteProtocol": 3}:
                raise GateError("unexpected Marionette protocol greeting")
        except BaseException:
            self.close()
            raise

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Marionette gate deadline expired")
        return remaining

    def _read_exact(self, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            self._socket.settimeout(self._remaining())
            chunk = self._socket.recv(length - len(data))
            if not chunk:
                raise GateError("truncated Marionette message")
            data.extend(chunk)
        return bytes(data)

    def _receive(self) -> object:
        digits = bytearray()
        while True:
            byte = self._read_exact(1)
            if byte == b":":
                break
            if not byte.isdigit() or len(digits) >= 10:
                raise GateError("invalid Marionette frame length")
            digits.extend(byte)
        if not digits:
            raise GateError("missing Marionette frame length")
        length = int(digits)
        if length > MAX_MESSAGE_BYTES:
            raise GateError("oversized Marionette message")
        try:
            return json.loads(self._read_exact(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GateError("invalid Marionette JSON") from error

    def command(self, name: str, parameters: object | None = None) -> object:
        identifier = self._next_id
        self._next_id += 1
        payload = json.dumps([0, identifier, name, parameters or {}], separators=(",", ":")).encode()
        self._socket.settimeout(self._remaining())
        self._socket.sendall(str(len(payload)).encode("ascii") + b":" + payload)
        response = self._receive()
        if not isinstance(response, list) or len(response) != 4:
            raise GateError("malformed Marionette response")
        kind, response_id, error, result = response
        if kind != 1 or response_id != identifier:
            raise GateError("unexpected Marionette response identity")
        if error is not None:
            raise GateError(f"Marionette command failed: {name}")
        return result

    def close(self) -> None:
        self._socket.close()


def snapshot_complete(snapshot: object) -> bool:
    if not isinstance(snapshot, dict) or set(snapshot) != {"url", "markers", "media", "resources"}:
        raise GateError("browser snapshot has unexpected fields")
    if snapshot["url"] != PROBE_URL:
        raise GateError("browser snapshot is not the repository-owned probe")
    markers = snapshot["markers"]
    if not isinstance(markers, list) or len(markers) != len(EXPECTED_MARKERS):
        raise GateError("browser content markers are missing or duplicated")
    passed = 0
    pending_seen = False
    for index, marker in enumerate(markers):
        if marker == list(EXPECTED_MARKERS[index]):
            if pending_seen:
                raise GateError("browser content markers are reordered")
            passed += 1
        elif marker == list(PENDING_MARKERS[index]):
            pending_seen = True
        else:
            raise GateError("browser content marker is forged")
    resources = snapshot["resources"]
    if not isinstance(resources, list) or not all(isinstance(item, str) for item in resources):
        raise GateError("browser resource evidence is malformed")
    if any(resource != VIDEO_URL for resource in resources) or len(resources) > 1:
        raise GateError("browser workload used an unexpected or external resource")
    complete = passed == len(EXPECTED_MARKERS)
    media = snapshot["media"]
    expected_media_keys = {"currentSrc", "ended", "readyState", "error", "duration", "currentTime"}
    if not isinstance(media, dict) or set(media) != expected_media_keys:
        raise GateError("browser media evidence is malformed")
    current_src = media["currentSrc"]
    ended = media["ended"]
    ready_state = media["readyState"]
    error = media["error"]
    duration = media["duration"]
    current_time = media["currentTime"]
    if not isinstance(current_src, str) or current_src not in {"", VIDEO_URL}:
        raise GateError("browser media used an unexpected or external source")
    if not isinstance(ended, bool):
        raise GateError("browser media ended state is malformed")
    if isinstance(ready_state, bool) or not isinstance(ready_state, int) or not 0 <= ready_state <= 4:
        raise GateError("browser media ready state is malformed")
    if error is not None:
        raise GateError("browser media reported a decode error")
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise GateError("browser media duration is malformed")
    if current_time is not None and (
        isinstance(current_time, bool)
        or not isinstance(current_time, (int, float))
        or not math.isfinite(current_time)
        or current_time < 0
    ):
        raise GateError("browser media current time is malformed")
    if complete and (
        current_src != VIDEO_URL
        or not ended
        or ready_state < 2
        or duration is None
        or current_time is None
        or current_time <= 0
        or abs(current_time - duration) > max(0.05, duration * 0.05)
    ):
        raise GateError("completed markers disagree with live media state")
    return complete


def validate_snapshot(snapshot: object) -> None:
    if not snapshot_complete(snapshot):
        raise GateError("browser content markers are still pending")


def validate_network_namespace(firefox_pid: int) -> None:
    if firefox_pid <= 1:
        raise GateError("Firefox PID is outside the valid contract")
    try:
        gate_namespace = os.readlink("/proc/self/ns/net")
        firefox_namespace = os.readlink(f"/proc/{firefox_pid}/ns/net")
    except OSError as error:
        raise GateError("cannot inspect Firefox network namespace") from error
    if gate_namespace != firefox_namespace:
        raise GateError("content gate did not join the Firefox network namespace")
    try:
        interfaces = [name for _, name in socket.if_nameindex()]
    except OSError as error:
        raise GateError("cannot inspect private network interfaces") from error
    if interfaces != ["lo"]:
        raise GateError("Firefox network namespace is not loopback-only")


def run_gate(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GateError("Marionette endpoint did not become ready before deadline")
        try:
            client = Marionette(host, port, remaining)
            break
        except OSError as error:
            if error.errno != errno.ECONNREFUSED:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GateError("Marionette endpoint did not become ready before deadline") from error
            time.sleep(min(0.1, remaining))
    try:
        session = client.command("WebDriver:NewSession", {"strictFileInteractability": True})
        if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str):
            raise GateError("Marionette did not create a session")
        while True:
            handles = client.command("WebDriver:GetWindowHandles")
            if not isinstance(handles, list) or not handles:
                raise GateError("Marionette returned no browser windows")
            snapshots = []
            for handle in handles:
                if not isinstance(handle, str):
                    raise GateError("Marionette returned an invalid window handle")
                client.command("WebDriver:SwitchToWindow", {"handle": handle, "focus": False})
                result = client.command(
                    "WebDriver:ExecuteScript",
                    {
                        "script": _EXPRESSION,
                        "args": [],
                        "newSandbox": True,
                        "sandbox": "default",
                        "line": 1,
                        "filename": "asterinas-browser-m5-gate",
                    },
                )
                value = result.get("value") if isinstance(result, dict) else None
                if isinstance(value, str):
                    try:
                        snapshot = json.loads(value)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(snapshot, dict) and snapshot.get("url") == PROBE_URL:
                        snapshots.append(snapshot)
            if len(snapshots) != 1:
                raise GateError("expected exactly one offline probe browser window")
            if snapshot_complete(snapshots[0]):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GateError("browser content markers did not complete before deadline")
            time.sleep(min(0.1, remaining))
        client.command("WebDriver:DeleteSession")
    finally:
        client.close()


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="browser_m5_marionette_gate")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2828)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--firefox-pid", type=int, required=True)
    values = parser.parse_args(arguments)
    if not 1 <= values.port <= 65535 or not 0 < values.timeout <= 300:
        parser.error("port or timeout is outside the bounded contract")
    try:
        validate_network_namespace(values.firefox_pid)
        run_gate(values.host, values.port, values.timeout)
    except (GateError, OSError, TimeoutError) as error:
        parser.error(str(error))
    print(PASS_LINE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
