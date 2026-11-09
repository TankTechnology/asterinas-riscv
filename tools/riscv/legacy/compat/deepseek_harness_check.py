#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Make one real DeepSeek chat-completion call from wherever this runs.

This is the end-to-end harness proof: DNS (or a pinned address), TLS, HTTP,
authentication, and a JSON response, using only the standard library so it
works in a minimal rootfs.

The key is read from `DEEPSEEK_API_KEY` (or `ANTHROPIC_API_KEY`, the variable
the harness itself uses, since the board's harness reaches DeepSeek through
`https://api.deepseek.com/anthropic`). It is never printed and never written
to a file.

    DEEPSEEK_API_KEY=... python3 deepseek_harness_check.py
    DEEPSEEK_API_IP=119.188.220.215 python3 deepseek_harness_check.py   # no DNS

Output is one line: `DEEPSEEK_API_OK ...` on success, `DEEPSEEK_API_FAIL ...`
on failure, `DEEPSEEK_API_SKIP ...` without a key or a pinned address.

Exit status: 0 call succeeded, 1 the call failed, 2 skipped.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import http.client
import urllib.error
import urllib.request

HOST = "api.deepseek.com"
PATH = "/v1/chat/completions"


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to `HOST` while the TCP connection goes to a pinned address."""

    def __init__(self, address: str, timeout: float):
        super().__init__(HOST, 443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):  # type: ignore[override]
        sock = socket.create_connection((self._address, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=HOST)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, address: str, timeout: float):
        super().__init__()
        self._address = address
        self._timeout = timeout

    def https_open(self, req):  # type: ignore[override]
        return self.do_open(
            lambda host, **kw: PinnedHTTPSConnection(self._address, self._timeout), req
        )


def main() -> int:
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("DEEPSEEK_API_SKIP no DEEPSEEK_API_KEY/ANTHROPIC_API_KEY in the environment")
        return 2

    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    pinned_ip = os.environ.get("DEEPSEEK_API_IP")
    timeout = float(os.environ.get("DEEPSEEK_TIMEOUT", "30"))

    url = os.environ.get("DEEPSEEK_URL", f"https://{HOST}{PATH}")
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
        }
    ).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )

    opener = (
        urllib.request.build_opener(PinnedHTTPSHandler(pinned_ip, timeout))
        if pinned_ip
        else urllib.request.build_opener()
    )

    try:
        with opener.open(req, timeout=timeout) as resp:
            data = json.load(resp)
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            print(
                "DEEPSEEK_API_OK"
                f" status={resp.status}"
                f" model={data.get('model')}"
                f" finish={choice.get('finish_reason')}"
                f" content_chars={len(content)}"
                f" usage={data.get('usage', {}).get('total_tokens', '?')}"
            )
            return 0
    except urllib.error.HTTPError as err:
        detail = err.read()[:160].decode(errors="replace")
        print(f"DEEPSEEK_API_FAIL http={err.code} body={detail!r}")
        return 1
    except Exception as err:  # noqa: BLE001 - a diagnostic tool reports everything
        print(f"DEEPSEEK_API_FAIL {type(err).__name__}: {str(err)[:160]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
