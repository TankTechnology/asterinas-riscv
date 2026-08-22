#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Serve the BROWSER-M12 TLS certificate matrix over HTTPS on the host loopback.

Binds one HTTPS endpoint per test certificate on 127.0.0.1 (the slirp ``host``
address the guest reaches as 10.0.2.2):

    :8443  valid.crt      (test-CA signed, IP:10.0.2.2, in-date)
    :8444  expired.crt    (test-CA signed, in the past)
    :8445  wronghost.crt  (test-CA signed, DNS:wrong.example.com)
    :8446  selfsigned.crt (untrusted self-signed leaf)

Each endpoint serves a small HTML page whose title names the certificate, so a
client that *does* fetch (e.g. curl -k) can be confirmed to have hit the right
endpoint. Every connection is logged with its TLS handshake outcome — ``OK`` with
the negotiated version/cipher when the client accepted the certificate, or
``FAIL`` with the TLS alert reason when the client aborted verification. That
transcript is the kernel-TLS-link evidence: the handshake always reaches the
certificate exchange (so the guest's virtio-net/TCP stack carried the full
handshake); only the final userspace X.509 check differs.

Usage:
    python3 tls_cert_server.py <certdir> [--log /tmp/tls-server.log] [--port-base 8443]
"""

from __future__ import annotations

import argparse
import http.server
import ssl
import sys
import threading
from pathlib import Path

CASES = [
    ("valid", "valid.crt", "valid.key"),
    ("expired", "expired.crt", "expired.key"),
    ("wronghost", "wronghost.crt", "wronghost.key"),
    ("selfsigned", "selfsigned.crt", "selfsigned.key"),
]

PAGE = """<!DOCTYPE html><html><head><title>TLS {name} endpoint</title></head>
<body><h1>TLS certificate: {name}</h1>
<p>This page is served over HTTPS with the <code>{name}</code> certificate.</p>
</body></html>"""


def log(msg: str) -> None:
    print(msg, flush=True)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "BROWSER-M12/1.0"

    def do_GET(self) -> None:
        name = getattr(self.server, "case_name", "?")
        body = PAGE.format(name=name).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silence per-request stderr line
        pass


class TLSServer(http.server.ThreadingHTTPServer):
    """Plain-TCP ThreadingHTTPServer; TLS is negotiated per-connection in
    get_request() so the handshake outcome can be logged (and a client's fatal
    verification alert is dropped without killing the accept loop)."""

    allow_reuse_address = True
    daemon_threads = True
    sslctx: ssl.SSLContext | None = None
    case_name: str = "?"

    def get_request(self):
        sock, addr = super().get_request()
        try:
            ssock = self.sslctx.wrap_socket(sock, server_side=True)
            log(f"[tls] {self.case_name} OK {addr} {ssock.version()} {ssock.cipher()[0]}")
            return ssock, addr
        except ssl.SSLError as e:
            log(f"[tls] {self.case_name} FAIL {addr} {getattr(e, 'reason', e)}")
            try:
                sock.close()
            except OSError:
                pass
            raise
        except OSError as e:
            log(f"[tls] {self.case_name} ERR {addr} {e}")
            raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("certdir", type=Path)
    ap.add_argument("--port-base", type=int, default=8443)
    args = ap.parse_args()

    servers: list[TLSServer] = []
    for i, (name, crt, key) in enumerate(CASES):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(args.certdir / crt), str(args.certdir / key))
        srv = TLSServer(("127.0.0.1", args.port_base + i), Handler)
        srv.sslctx = ctx
        srv.case_name = name
        servers.append(srv)
        log(f"[serve] {name} on 127.0.0.1:{args.port_base + i} (cert {crt})")

    log(f"[serve] BROWSER-M12 TLS server up ({len(servers)} endpoints); Ctrl-C to stop")
    threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2},
                                daemon=True) for s in servers]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        log("[serve] interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
