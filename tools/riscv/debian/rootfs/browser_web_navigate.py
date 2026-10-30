#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Point the running Firefox at the browser-web target URL.

The browser service starts Firefox with `--marionette` on `about:blank` and
leaves the page to a driver.  Nothing else in this image drives the browser, so
Firefox sat on a blank page at every boot even when the network and the clock
were fine.

Marionette speaks length-prefixed JSON over a local TCP socket.  The protocol
needs a WebDriver session before it accepts a navigation, so this connects,
creates one, navigates, and leaves the page loaded.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time

DEFAULT_URL = "https://www.baidu.com/"
PORT = 2828


class MarionetteError(RuntimeError):
    """The browser did not accept the request."""


def _receive(sock: socket.socket) -> list:
    buffer = b""
    while b":" not in buffer:
        chunk = sock.recv(1)
        if not chunk:
            raise MarionetteError("marionette closed the connection")
        buffer += chunk
    length, rest = buffer.split(b":", 1)
    payload = rest
    expected = int(length)
    while len(payload) < expected:
        chunk = sock.recv(expected - len(payload))
        if not chunk:
            raise MarionetteError("marionette closed mid-message")
        payload += chunk
    return json.loads(payload.decode())


def _send(sock: socket.socket, message: list) -> None:
    body = json.dumps(message).encode()
    sock.sendall(str(len(body)).encode() + b":" + body)


def _result(reply: list) -> object:
    if len(reply) > 3 and reply[2] is not None:
        error = reply[2]
        raise MarionetteError(str(error.get("message", error)))
    return reply[3] if len(reply) > 3 else None


def navigate(url: str, *, port: int = PORT, connect_timeout: float = 30.0) -> None:
    """Create a session and navigate; every socket operation is bounded."""

    deadline = time.monotonic() + connect_timeout
    while True:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=10)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise MarionetteError(f"marionette port {port} never opened") from None
            time.sleep(1)
    with sock:
        sock.settimeout(120)
        greeting = _receive(sock)
        if greeting.get("applicationType") != "gecko":
            raise MarionetteError(f"unexpected marionette greeting: {greeting!r}")
        _send(sock, [0, 1, "WebDriver:NewSession", {"capabilities": {}}])
        _result(_receive(sock))
        _send(sock, [0, 2, "WebDriver:Navigate", {"url": url}])
        _result(_receive(sock))


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="browser-web-navigate")
    parser.add_argument(
        "--url",
        default=os.environ.get("ASTERINAS_FIREFOX_WEB_TARGET_URL") or DEFAULT_URL,
    )
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--timeout", type=float, default=120.0)
    values = parser.parse_args(arguments)
    try:
        navigate(values.url, port=values.port, connect_timeout=values.timeout)
    except (MarionetteError, OSError, ValueError) as error:
        print(f"ASTERINAS_BROWSER_NAVIGATE_FAIL url={values.url} error={error}", file=sys.stderr)
        return 1
    print(f"ASTERINAS_BROWSER_NAVIGATE_OK url={values.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
