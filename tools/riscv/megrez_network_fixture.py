#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Serve one deterministic payload for bounded Megrez network gates."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import ipaddress
import json
import signal
import struct
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit


FIXTURE_PATH = "/asterinas-network-probe.bin"
PAYLOAD_SIZE = 64 * 1024
PAYLOAD = bytes(range(256)) * (PAYLOAD_SIZE // 256)
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
MAX_REQUEST_RECORDS = 64
BROWSER_INDEX_PATH = "/browser-quality/index.html"
BROWSER_SECOND_PATH = "/browser-quality/second.html"
BROWSER_IMAGE_PATH = "/browser-quality/pattern.png"
BROWSER_DOWNLOAD_PATH = "/browser-quality/download.bin"
BROWSER_API_PATH = "/browser-quality/capabilities.json"
BROWSER_AUDIO_PATH = "/browser-quality/tone.wav"
BROWSER_CAPTURE_PATH = "/browser-quality/capture.xwd.gz"
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
BROWSER_DOWNLOAD = bytes(range(256)) * 1024
BROWSER_DOWNLOAD_SHA256 = hashlib.sha256(BROWSER_DOWNLOAD).hexdigest()
BROWSER_API = b'{"schema_version":1,"token":"asterinas-browser-quality"}\n'


def _pcm_wav() -> bytes:
    """Return 250 ms of deterministic mono PCM without an external asset."""

    samples = bytes((128 + ((index % 32) - 16) * 4) & 0xFF for index in range(2000))
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(samples))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8)
        + b"data"
        + struct.pack("<I", len(samples))
        + samples
    )


BROWSER_AUDIO = _pcm_wav()
BROWSER_IMAGE = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000020000000200806000000737a7af4"
    "000000414944415478da6310cf58f31f19a3035acb338c3a60c01d406f0bd1e547"
    "1d30f00e18cd05a30e18cd05a30e18cd05a30e18cd05a30e18cd05a30e18cd0523"
    "de0100a3694cb594617d3a0000000049454e44ae426082"
)
BROWSER_INDEX = b"""<!doctype html>
<meta charset=utf-8><title>Asterinas Browser Quality</title>
<link rel=icon href=/browser-quality/pattern.png>
<style>body{font-family:sans-serif}.scroll{height:1600px}
.second{position:absolute;left:40px;top:220px}
.download{position:absolute;left:40px;top:260px}</style>
<h1>Asterinas browser quality / \xe6\xb5\x8f\xe8\xa7\x88\xe5\x99\xa8\xe8\xb4\xa8\xe9\x87\x8f</h1>
<form method=get><input name=q><button>Search</button></form>
<img src=/browser-quality/pattern.png alt=pattern>
<p class=second><a href=/browser-quality/second.html>Second page</a></p>
<p class=download><a id=quality-download download=asterinas-browser-quality.bin
 href=/browser-quality/download.bin>Download</a></p>
<canvas id=quality-canvas width=1 height=1 hidden></canvas>
<audio id=quality-audio preload=auto hidden></audio>
<output id=quality-capabilities hidden></output>
<div class=scroll></div>
<script>
(() => {
  'use strict';
  const checks = Object.create(null);
  let wasmFailure = null;
  const search = new URLSearchParams(location.search).get('q') === 'asterinas';
  const phase = search ? 'search' : 'home';
  window.__asterinasCapabilities = {version: 1, phase, state: 'running', checks, error: null};
  const publish = () => {
    document.querySelector('#quality-capabilities').textContent =
      JSON.stringify(window.__asterinasCapabilities);
  };
  const mark = name => {
    publish();
    // Firefox mirrors console output to the guest stderr log used by the
    // QEMU gate.  These markers remain useful when an API blocks the
    // Marionette probe before it can return a JSON snapshot.
    console.log('A_WEB_CAPABILITY_STEP step=' + name);
  };
  publish();
  // Capability checks must not monopolize the page probe on a guest whose
  // storage or media stack is unavailable.  Five seconds is ample for these
  // tiny local resources; a timeout is recorded as a failed capability and
  // the overall gate remains fail-closed.
  const failAfter = (promise, name) => Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(name + '-timeout')), 5000))
  ]);
  const openDatabase = () => new Promise((resolve, reject) => {
    const request = indexedDB.open('asterinas-browser-quality-v1', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('quality');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('indexeddb-open'));
  });
  const transactionDone = transaction => new Promise((resolve, reject) => {
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error || new Error('indexeddb-transaction'));
    transaction.onabort = () => reject(transaction.error || new Error('indexeddb-abort'));
  });
  const readDatabase = (database, key) => new Promise((resolve, reject) => {
    const request = database.transaction('quality').objectStore('quality').get(key);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('indexeddb-read'));
  });
  const run = async () => {
    mark('storage');
    const localKey = 'asterinas-quality-local';
    const sessionKey = 'asterinas-quality-session';
    if (search) {
      checks.localStorage = localStorage.getItem(localKey) === 'home';
      checks.sessionStorage = sessionStorage.getItem(sessionKey) === 'home';
      checks.cookie = document.cookie.split('; ').includes('asterinas_quality=home');
    } else {
      localStorage.setItem(localKey, 'home');
      sessionStorage.setItem(sessionKey, 'home');
      document.cookie = 'asterinas_quality=home; SameSite=Strict; Path=/browser-quality/';
      checks.localStorage = localStorage.getItem(localKey) === 'home';
      checks.sessionStorage = sessionStorage.getItem(sessionKey) === 'home';
      checks.cookie = document.cookie.split('; ').includes('asterinas_quality=home');
    }

    // Do not synchronously create/read a canvas in the readiness document:
    // headless QEMU guests without a compositor can block that call and starve
    // Marionette itself.  The API surface is checked here; pixel rendering is
    // covered by the separate screenshot/render gate.
    checks.canvas = typeof HTMLCanvasElement === 'function' &&
      typeof HTMLCanvasElement.prototype.getContext === 'function';

    mark('wasm');
    try {
      checks.wasm = typeof WebAssembly !== 'undefined' &&
        (await WebAssembly.instantiate(new Uint8Array([
          0,97,115,109,1,0,0,0,1,5,1,96,0,1,127,3,2,1,0,
          7,10,1,6,97,110,115,119,101,114,0,0,10,6,1,4,0,65,42,11
        ]))).instance.exports.answer() === 42;
    } catch (error) {
      checks.wasm = false;
      wasmFailure = String(error && error.message || error).slice(0, 96);
    }

    mark('worker');
    try {
      checks.worker = await failAfter(new Promise((resolve, reject) => {
        const source = 'onmessage=e=>postMessage(e.data*2)';
        const url = URL.createObjectURL(new Blob([source], {type: 'text/javascript'}));
        const worker = new Worker(url);
        worker.onmessage = event => { worker.terminate(); URL.revokeObjectURL(url); resolve(event.data === 42); };
        worker.onerror = event => { worker.terminate(); URL.revokeObjectURL(url); reject(new Error(event.message)); };
        worker.postMessage(21);
      }), 'worker');
    } catch (_) {
      checks.worker = false;
    }

    mark('indexeddb');
    try {
      const database = await failAfter(openDatabase(), 'indexeddb');
      if (search) {
        checks.indexedDb = (await failAfter(readDatabase(database, 'marker'), 'indexeddb-read')) === 'home';
      } else {
        const transaction = database.transaction('quality', 'readwrite');
        transaction.objectStore('quality').put('home', 'marker');
        await failAfter(transactionDone(transaction), 'indexeddb-write');
        checks.indexedDb = (await failAfter(readDatabase(database, 'marker'), 'indexeddb-read')) === 'home';
      }
      database.close();
    } catch (_) {
      checks.indexedDb = false;
    }

    mark('audio');
    try {
      const audio = document.querySelector('#quality-audio');
      checks.audio = await failAfter(new Promise((resolve, reject) => {
        audio.onloadedmetadata = () => resolve(audio.duration > 0.2 && audio.duration < 0.3);
        audio.onerror = () => reject(new Error('audio-decode'));
        audio.src = '/browser-quality/tone.wav';
        audio.load();
      }), 'audio');
    } catch (_) {
      checks.audio = false;
    }

    // Keep network I/O last.  On the guest, a stalled fetch can prevent the
    // promise continuation (and therefore the readiness probe) from running;
    // all local API checks above remain observable in the published report.
    mark('fetch');
    try {
      const api = await failAfter(fetch('/browser-quality/capabilities.json', {cache: 'no-store'}), 'fetch');
      const payload = await failAfter(api.json(), 'fetch-json');
      checks.fetch = api.ok && payload.schema_version === 1 && payload.token === 'asterinas-browser-quality';
    } catch (_) {
      checks.fetch = false;
    }

    mark('complete');
    if (!Object.values(checks).every(value => value === true)) {
      throw new Error(wasmFailure && checks.wasm === false ? 'wasm:' + wasmFailure : 'false-capability');
    }
    window.__asterinasCapabilities.state = 'complete';
    publish();
  };
  // Let the navigation/readiness probe observe a responsive DOM before
  // starting optional capability work.  Some guest implementations service
  // storage/media on the main thread; launching it during HTML parsing can
  // otherwise starve the first Marionette ExecuteScript command.
  setTimeout(() => run().catch(error => {
    window.__asterinasCapabilities.state = 'error';
    const failed = Object.entries(window.__asterinasCapabilities.checks)
      .filter(([, value]) => value !== true).map(([name]) => name).join(',');
    window.__asterinasCapabilities.error = (
      String(error && error.message || error) + (failed ? ':' + failed : '')
    ).slice(0, 160);
    publish();
  }), 500);
})();
</script>"""
BROWSER_SECOND = b"""<!doctype html>
<meta charset=utf-8><title>Second - Asterinas Browser Quality</title>
<h1>Second page / \xe7\xac\xac\xe4\xba\x8c\xe9\xa1\xb5</h1>
<a href=/browser-quality/index.html>First page</a>"""
BROWSER_SEARCH = BROWSER_INDEX.replace(
    b"<title>Asterinas Browser Quality</title>",
    b"<title>asterinas - Asterinas Browser Quality</title>",
)


def browser_resource(path: str) -> tuple[str, bytes] | None:
    """Return one deterministic browser resource for an exact path."""

    return {
        BROWSER_INDEX_PATH: ("text/html; charset=utf-8", BROWSER_INDEX),
        BROWSER_SECOND_PATH: ("text/html; charset=utf-8", BROWSER_SECOND),
        BROWSER_IMAGE_PATH: ("image/png", BROWSER_IMAGE),
        BROWSER_DOWNLOAD_PATH: ("application/octet-stream", BROWSER_DOWNLOAD),
        BROWSER_API_PATH: ("application/json", BROWSER_API),
        BROWSER_AUDIO_PATH: ("audio/wav", BROWSER_AUDIO),
    }.get(path)


def is_successful_summary(
    summary: Mapping[str, object], *, expected_requests: int
) -> bool:
    """Require an exact set of successful fixed-payload requests."""

    if (
        isinstance(expected_requests, bool)
        or not isinstance(expected_requests, int)
        or not 0 < expected_requests <= MAX_REQUEST_RECORDS
    ):
        return False
    requests = summary.get("requests")
    if not isinstance(requests, list) or len(requests) != expected_requests:
        return False
    if (
        summary.get("schema_version") != 1
        or summary.get("payload_path") != FIXTURE_PATH
        or summary.get("payload_sha256") != PAYLOAD_SHA256
        or summary.get("payload_size") != PAYLOAD_SIZE
        or summary.get("request_count") != expected_requests
        or summary.get("records_truncated") is not False
    ):
        return False
    return all(
        isinstance(record, dict)
        and record.get("body_bytes") == PAYLOAD_SIZE
        and record.get("path") == FIXTURE_PATH
        and record.get("status") == 200
        for record in requests
    )


@dataclass(frozen=True)
class FixtureConfig:
    """The exact local listener and optional peer restriction."""

    bind_address: str = "127.0.0.1"
    port: int = 17894
    allowed_peer: str | None = None

    def __post_init__(self) -> None:
        _validate_ipv4(self.bind_address, "bind address")
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ValueError("port must be an integer between 0 and 65535")
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        if self.allowed_peer is not None:
            _validate_ipv4(self.allowed_peer, "allowed peer")


def _validate_ipv4(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an IPv4 address")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an IPv4 address") from error
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical IPv4 address")


class FixtureServer:
    """One explicitly owned ThreadingHTTPServer with bounded evidence."""

    def __init__(self, config: FixtureConfig) -> None:
        self.config = config
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._request_count = 0
        self._records: list[dict[str, object]] = []
        self._last_timestamp = 0
        self._capture: bytes | None = None
        self._capture_evidence: dict[str, object] | None = None

    def __enter__(self) -> FixtureServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        del exc_info
        self.close()

    @property
    def address(self) -> str:
        server = self._require_server()
        return str(server.server_address[0])

    @property
    def port(self) -> int:
        server = self._require_server()
        return int(server.server_address[1])

    @property
    def endpoint(self) -> str:
        return f"http://{self.address}:{self.port}{FIXTURE_PATH}"

    @property
    def running(self) -> bool:
        return self._server is not None and self._thread is not None

    @property
    def thread(self) -> threading.Thread:
        if self._thread is None:
            raise RuntimeError("fixture server is not running")
        return self._thread

    def _require_server(self) -> http.server.ThreadingHTTPServer:
        if self._server is None:
            raise RuntimeError("fixture server is not running")
        return self._server

    def start(self) -> FixtureServer:
        if self._server is not None:
            return self
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                owner._handle_get(self)

            def do_POST(self) -> None:
                owner._handle_post(self)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = http.server.ThreadingHTTPServer(
            (self.config.bind_address, self.config.port), Handler
        )
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="megrez-network-fixture",
        )
        self._server = server
        self._thread = thread
        thread.start()
        return self

    def close(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        self._server = None
        self._thread = None
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)
            if thread.is_alive():
                raise RuntimeError("fixture server thread did not stop")

    def _handle_get(self, request: http.server.BaseHTTPRequestHandler) -> None:
        peer = request.client_address[0]
        target = urlsplit(request.path)
        is_browser_request = target.path.startswith("/browser-quality/")
        if self.config.allowed_peer is not None and peer != self.config.allowed_peer:
            status = 403
            body = b""
            content_type = "application/octet-stream"
        elif is_browser_request:
            status, content_type, body = self._browser_response(
                target.path, target.query
            )
        elif request.path != FIXTURE_PATH:
            status = 404
            body = b""
            content_type = "application/octet-stream"
        else:
            status = 200
            body = PAYLOAD
            content_type = "application/octet-stream"

        # Publish legacy request evidence before the fixed-length response is
        # visible to the client.  Otherwise a client can finish reading the
        # body and ask for the summary during the tiny interval before the
        # handler's finally block records it.
        if not is_browser_request:
            self._record(peer, request.path, status, len(body))
        self._send_response(request, status, body, content_type)
        request.wfile.write(body)

    def _browser_response(self, path: str, query: str) -> tuple[int, str, bytes]:
        if path == BROWSER_INDEX_PATH:
            if not query:
                return 200, "text/html; charset=utf-8", BROWSER_INDEX
            if query == "q=asterinas":
                return 200, "text/html; charset=utf-8", BROWSER_SEARCH
            return 400, "text/plain; charset=utf-8", b""
        if query:
            return 400, "text/plain; charset=utf-8", b""
        resource = browser_resource(path)
        if resource is None:
            return 404, "text/plain; charset=utf-8", b""
        content_type, body = resource
        return 200, content_type, body

    def _handle_post(self, request: http.server.BaseHTTPRequestHandler) -> None:
        peer = request.client_address[0]
        target = urlsplit(request.path)
        if self.config.allowed_peer is not None and peer != self.config.allowed_peer:
            self._send_response(request, 403)
            return
        if target.path != BROWSER_CAPTURE_PATH or target.query:
            self._send_response(request, 404)
            return
        if request.headers.get("Transfer-Encoding") is not None:
            self._send_response(request, 400)
            return
        lengths = request.headers.get_all("Content-Length", failobj=[])
        if not lengths:
            self._send_response(request, 411)
            return
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            self._send_response(request, 400)
            return
        size = int(lengths[0])
        if size == 0:
            self._send_response(request, 400)
            return
        if size > MAX_CAPTURE_BYTES:
            self._send_response(request, 413)
            return
        request.connection.settimeout(1.0)
        try:
            payload = request.rfile.read(size)
        except OSError:
            self._send_response(request, 400)
            return
        if len(payload) != size:
            self._send_response(request, 400)
            return
        with self._lock:
            if self._capture is not None:
                status = 409
            else:
                self._capture = bytes(payload)
                self._capture_evidence = {
                    "bytes": size,
                    "path": BROWSER_CAPTURE_PATH,
                    "peer": peer,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                status = 201
        self._send_response(request, status)

    def _send_response(
        self,
        request: http.server.BaseHTTPRequestHandler,
        status: int,
        body: bytes = b"",
        content_type: str = "application/octet-stream",
    ) -> None:
        request.send_response(status)
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Content-Type", content_type)
        request.send_header("Cache-Control", "no-store")
        request.send_header("Connection", "close")
        request.end_headers()
        request.close_connection = True

    def capture_payload(self) -> bytes | None:
        """Return the immutable accepted capture, if one exists."""

        with self._lock:
            return self._capture

    def capture_summary(self) -> dict[str, object] | None:
        """Return detached evidence for the accepted capture."""

        with self._lock:
            if self._capture_evidence is None:
                return None
            return dict(self._capture_evidence)

    def _record(self, peer: str, path: str, status: int, body_bytes: int) -> None:
        with self._lock:
            self._request_count += 1
            now = max(time.monotonic_ns(), self._last_timestamp + 1)
            self._last_timestamp = now
            if len(self._records) < MAX_REQUEST_RECORDS:
                self._records.append(
                    {
                        "body_bytes": body_bytes,
                        "monotonic_ns": now,
                        "path": path,
                        "peer": peer,
                        "status": status,
                    }
                )

    def summary(self) -> dict[str, object]:
        """Return a detached canonical-schema snapshot of bounded evidence."""

        with self._lock:
            records = [dict(record) for record in self._records]
            request_count = self._request_count
        return {
            "payload_path": FIXTURE_PATH,
            "payload_sha256": PAYLOAD_SHA256,
            "payload_size": PAYLOAD_SIZE,
            "records_truncated": request_count > len(records),
            "request_count": request_count,
            "requests": records,
            "schema_version": 1,
        }

    def summary_json(self) -> bytes:
        return (
            json.dumps(self.summary(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()


def _ipv4_argument(value: str) -> str:
    try:
        _validate_ipv4(value, "address")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return value


def _port_argument(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535") from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def _parse_args(arguments: Sequence[str] | None = None) -> FixtureConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind-address", type=_ipv4_argument, default="127.0.0.1")
    parser.add_argument("--port", type=_port_argument, default=17894)
    parser.add_argument("--allow-peer", type=_ipv4_argument)
    values = parser.parse_args(arguments)
    return FixtureConfig(values.bind_address, values.port, values.allow_peer)


def main(arguments: Sequence[str] | None = None) -> int:
    config = _parse_args(arguments)
    stop = threading.Event()
    previous: dict[int, signal.Handlers] = {}

    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        stop.set()

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    try:
        with FixtureServer(config) as server:
            print(server.endpoint, flush=True)
            stop.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
