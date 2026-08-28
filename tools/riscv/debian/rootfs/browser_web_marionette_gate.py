#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fail-closed Marionette gate for real Baidu and Bilibili HTTPS content."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from collections.abc import Callable, Sequence
from urllib.parse import parse_qs, quote_plus, urlparse

if Path("/usr/lib/asterinas/browser_m5_marionette_gate.py").is_file():
    sys.path.insert(0, "/usr/lib/asterinas")
    from browser_m5_marionette_gate import GateError, Marionette, _connect
else:
    from tools.riscv.debian.rootfs.browser_m5_marionette_gate import (
        GateError,
        Marionette,
        _connect,
    )


BAIDU_HOME = "https://www.baidu.com/"
BAIDU_SEARCH = "https://www.baidu.com/s?wd=" + quote_plus("Asterinas")
BILIBILI_HOME = "https://www.bilibili.com/"
PASS_PREFIX = "DEBIAN_BROWSER_WEB_CONTENT"
CHALLENGE_TOKENS = (
    "403 forbidden",
    "access denied",
    "captcha",
    "challenge",
    "验证码",
    "安全验证",
    "访问受限",
    "请求被拦截",
)
BV_RE = re.compile(r"^https://www\.bilibili\.com/video/(BV[0-9A-Za-z]+)/?(?:[?#].*)?$")
MAX_RESOURCES = 256

_SNAPSHOT_SCRIPT = r"""return JSON.stringify({
  url: location.href,
  title: document.title,
  readyState: document.readyState,
  bodyText: (document.body === null ? '' : document.body.innerText).slice(0, 8192),
  jsComplete: (() => { window.__asterinasWebGate = 6 * 7; return window.__asterinasWebGate === 42; })(),
  dom: {
    baiduKeyword: document.querySelector('#kw') !== null,
    baiduSubmit: document.querySelector('#su') !== null,
    baiduResults: document.querySelectorAll('#content_left .result, #content_left .c-container').length,
    bilibiliHome: (() => { const n = document.querySelector('a[href*="/video/BV"], main, #app'); return n !== null && n.getBoundingClientRect().width > 0; })(),
    bilibiliDetail: (() => { const n = document.querySelector('video, .bpx-player-container, #bilibili-player'); return n !== null && n.getBoundingClientRect().width > 0; })()
  },
  links: Array.from(document.querySelectorAll('a[href*="/video/BV"]')).map(a => a.href).slice(0, 128),
  navigation: (() => {
    const n = performance.getEntriesByType('navigation')[0];
    return n === undefined ? null : {
      name: n.name,
      entryType: n.entryType,
      startTime: n.startTime,
      duration: n.duration,
      domainLookupStart: n.domainLookupStart,
      domainLookupEnd: n.domainLookupEnd,
      connectStart: n.connectStart,
      secureConnectionStart: n.secureConnectionStart,
      connectEnd: n.connectEnd,
      requestStart: n.requestStart,
      responseStart: n.responseStart,
      responseEnd: n.responseEnd,
      domContentLoadedEventEnd: n.domContentLoadedEventEnd,
      loadEventEnd: n.loadEventEnd,
      nextHopProtocol: n.nextHopProtocol
    };
  })(),
  resources: performance.getEntriesByType('resource').slice(0, 256).map(r => ({
    name: r.name,
    initiatorType: r.initiatorType,
    duration: r.duration,
    transferSize: r.transferSize
  }))
});"""

_BAIDU_SUBMIT_SCRIPT = r"""const keyword = document.querySelector('#kw');
const submit = document.querySelector('#su');
if (keyword === null || submit === null || keyword.form === null ||
    submit.form !== keyword.form) {
  return 'missing-controls';
}
keyword.focus();
keyword.value = 'Asterinas';
keyword.dispatchEvent(new Event('input', {bubbles: true}));
keyword.dispatchEvent(new Event('change', {bubbles: true}));
if (keyword.value !== 'Asterinas') {
  return 'keyword-rejected';
}
setTimeout(() => submit.click(), 0);
return 'search-click-scheduled';"""


def _mapping(snapshot: object) -> dict[str, object]:
    expected = {
        "url", "title", "readyState", "bodyText", "jsComplete", "dom",
        "links", "navigation", "resources",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != expected:
        raise GateError("web snapshot has unexpected fields")
    return snapshot


def _reject_challenge(snapshot: dict[str, object]) -> None:
    title = snapshot["title"]
    body = snapshot["bodyText"]
    if not isinstance(title, str) or not isinstance(body, str):
        raise GateError("web title or body evidence is malformed")
    combined = f"{title}\n{body}".casefold()
    if any(token.casefold() in combined for token in CHALLENGE_TOKENS):
        raise GateError("challenge, access denial, or HTTP 403 content observed")
    if len(body.strip()) < 20:
        raise GateError("web document body is empty")


def _validate_common(
    snapshot: object,
    *,
    host: str,
    require_tls_handshake: bool,
) -> dict[str, object]:
    result = _mapping(snapshot)
    url = result["url"]
    if not isinstance(url, str):
        raise GateError("web URL is malformed")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != host:
        raise GateError("web navigation did not retain the required HTTPS origin")
    if result["readyState"] != "complete" or result["jsComplete"] is not True:
        raise GateError("web document or JavaScript is incomplete")
    _reject_challenge(result)
    navigation = result["navigation"]
    if not isinstance(navigation, dict):
        raise GateError("NavigationTiming evidence is missing")
    required_timing = {
        "name", "entryType", "startTime", "duration", "domainLookupStart",
        "domainLookupEnd", "connectStart", "secureConnectionStart", "connectEnd",
        "requestStart", "responseStart", "responseEnd", "domContentLoadedEventEnd",
        "loadEventEnd", "nextHopProtocol",
    }
    if set(navigation) != required_timing or navigation["entryType"] != "navigation":
        raise GateError("NavigationTiming evidence is malformed")
    secure_start = navigation["secureConnectionStart"]
    if require_tls_handshake and (
        isinstance(secure_start, bool)
        or not isinstance(secure_start, (int, float))
        or secure_start <= 0
    ):
        raise GateError("verified TLS handshake timing is absent")
    resources = result["resources"]
    if not isinstance(resources, list) or not 0 < len(resources) <= MAX_RESOURCES:
        raise GateError("ResourceTiming evidence is missing or oversized")
    for resource in resources:
        if not isinstance(resource, dict) or set(resource) != {
            "name", "initiatorType", "duration", "transferSize"
        }:
            raise GateError("ResourceTiming evidence is malformed")
        name = resource["name"]
        if not isinstance(name, str) or urlparse(name).scheme not in {"https", "data"}:
            raise GateError("non-HTTPS external resource observed")
    dom = result["dom"]
    if not isinstance(dom, dict) or set(dom) != {
        "baiduKeyword", "baiduSubmit", "baiduResults", "bilibiliHome", "bilibiliDetail"
    }:
        raise GateError("web DOM evidence is malformed")
    return result


def validate_baidu_home(snapshot: object) -> None:
    result = _validate_common(snapshot, host="www.baidu.com", require_tls_handshake=True)
    dom = result["dom"]
    assert isinstance(dom, dict)
    if dom["baiduKeyword"] is not True or dom["baiduSubmit"] is not True:
        raise GateError("Baidu home search controls are absent")


def validate_baidu_search(snapshot: object) -> None:
    result = _validate_common(snapshot, host="www.baidu.com", require_tls_handshake=False)
    parsed = urlparse(str(result["url"]))
    if parse_qs(parsed.query).get("wd") != ["Asterinas"]:
        raise GateError("Baidu search URL does not contain the exact query")
    dom = result["dom"]
    assert isinstance(dom, dict)
    count = dom["baiduResults"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise GateError("Baidu returned no live search result DOM")


def select_bilibili_video(snapshot: object) -> str:
    result = _validate_common(snapshot, host="www.bilibili.com", require_tls_handshake=True)
    dom = result["dom"]
    assert isinstance(dom, dict)
    if dom["bilibiliHome"] is not True:
        raise GateError("Bilibili home DOM is absent")
    links = result["links"]
    if not isinstance(links, list):
        raise GateError("Bilibili links are malformed")
    canonical = sorted({link for link in links if isinstance(link, str) and BV_RE.fullmatch(link)})
    if not canonical:
        raise GateError("Bilibili home has no live public BV link")
    return canonical[0]


def validate_bilibili_detail(snapshot: object, expected_url: str) -> None:
    result = _validate_common(snapshot, host="www.bilibili.com", require_tls_handshake=False)
    expected = BV_RE.fullmatch(expected_url)
    actual = BV_RE.fullmatch(str(result["url"]))
    if expected is None or actual is None or actual.group(1) != expected.group(1):
        raise GateError("Bilibili detail did not retain the selected live BV identity")
    dom = result["dom"]
    assert isinstance(dom, dict)
    if dom["bilibiliDetail"] is not True:
        raise GateError("Bilibili player/detail DOM is absent")


def validate_network_namespace(firefox_pid: int) -> None:
    if firefox_pid <= 1:
        raise GateError("Firefox PID is outside the valid contract")
    try:
        if os.readlink("/proc/self/ns/net") != os.readlink(f"/proc/{firefox_pid}/ns/net"):
            raise GateError("web gate and Firefox do not share the host network namespace")
        interfaces = [name for _, name in socket.if_nameindex()]
    except OSError as error:
        raise GateError("cannot inspect Firefox network namespace") from error
    if "lo" not in interfaces or len([name for name in interfaces if name != "lo"]) != 1:
        raise GateError("web workload does not have exactly one non-loopback NIC")


def _snapshot(client: Marionette) -> dict[str, object]:
    response = client.command("WebDriver:ExecuteScript", {
        "script": _SNAPSHOT_SCRIPT,
        "args": [],
        "newSandbox": True,
        "sandbox": "default",
        "line": 1,
        "filename": "asterinas-browser-web-gate",
    })
    value = response.get("value") if isinstance(response, dict) else None
    if not isinstance(value, str):
        raise GateError("web snapshot script returned no JSON")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise GateError("web snapshot script returned malformed JSON") from error
    return _mapping(parsed)


def _navigate(client: Marionette, url: str) -> None:
    if client.command("WebDriver:Navigate", {"url": url}) is not None:
        raise GateError("Marionette returned an invalid Navigate result")


def _submit_baidu_search(client: Marionette) -> None:
    response = client.command("WebDriver:ExecuteScript", {
        "script": _BAIDU_SUBMIT_SCRIPT,
        "args": [],
        "newSandbox": True,
        "sandbox": "default",
        "line": 1,
        "filename": "asterinas-baidu-search-submit",
    })
    value = response.get("value") if isinstance(response, dict) else None
    if value != "search-click-scheduled":
        raise GateError("Baidu homepage search form could not be submitted")


def _wait(
    client: Marionette,
    validator: Callable[[object], object],
    deadline: float,
) -> tuple[dict[str, object], object]:
    last_error: GateError | None = None
    while time.monotonic() < deadline:
        snapshot = _snapshot(client)
        try:
            return snapshot, validator(snapshot)
        except GateError as error:
            if "challenge" in str(error) or "403" in str(error) or "access denial" in str(error):
                raise
            last_error = error
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    raise GateError(f"web DOM did not become ready: {last_error}")


def _write_evidence(
    client: Marionette,
    directory: Path,
    name: str,
    snapshot: dict[str, object],
) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    response = client.command("WebDriver:TakeScreenshot", {"full": False})
    encoded = response.get("value") if isinstance(response, dict) else None
    if not isinstance(encoded, str):
        raise GateError("Marionette screenshot response is malformed")
    try:
        screenshot = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise GateError("Marionette screenshot is not canonical base64") from error
    if not screenshot.startswith(b"\x89PNG\r\n\x1a\n"):
        raise GateError("Marionette screenshot is not PNG")
    (directory / f"{name}.png").write_bytes(screenshot)


def run_gate(host: str, port: int, timeout: float, evidence_dir: Path) -> str:
    deadline = time.monotonic() + timeout
    client = _connect(host, port, deadline)
    try:
        session = client.command("WebDriver:NewSession", {
            "acceptInsecureCerts": False,
            "strictFileInteractability": True,
        })
        if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str):
            raise GateError("Marionette did not create a web session")
        capabilities = session.get("capabilities", {})
        if not isinstance(capabilities, dict) or capabilities.get("acceptInsecureCerts") is not False:
            raise GateError("Firefox did not preserve certificate verification")

        _navigate(client, BAIDU_HOME)
        baidu_home, _ = _wait(client, validate_baidu_home, deadline)
        _write_evidence(client, evidence_dir, "baidu-home", baidu_home)

        _submit_baidu_search(client)
        baidu_search, _ = _wait(client, validate_baidu_search, deadline)
        _write_evidence(client, evidence_dir, "baidu-search", baidu_search)

        _navigate(client, BILIBILI_HOME)
        bilibili_home, selected = _wait(client, select_bilibili_video, deadline)
        assert isinstance(selected, str)
        _write_evidence(client, evidence_dir, "bilibili-home", bilibili_home)

        _navigate(client, selected)
        bilibili_detail, _ = _wait(
            client, lambda snapshot: validate_bilibili_detail(snapshot, selected), deadline
        )
        _write_evidence(client, evidence_dir, "bilibili-detail", bilibili_detail)
        client.command("WebDriver:DeleteSession")
        return BV_RE.fullmatch(selected).group(1)  # type: ignore[union-attr]
    finally:
        client.close()


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="browser_web_marionette_gate")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2828)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--firefox-pid", type=int, required=True)
    parser.add_argument(
        "--evidence-dir", type=Path,
        default=Path("/home/asterinas/browser-web-evidence"),
    )
    values = parser.parse_args(arguments)
    if values.host not in {"127.0.0.1", "::1"} or not 1 <= values.port <= 65535:
        parser.error("Marionette endpoint is outside the loopback contract")
    if not 0 < values.timeout <= 1200 or not values.evidence_dir.is_absolute():
        parser.error("timeout or evidence directory is outside the bounded contract")
    try:
        validate_network_namespace(values.firefox_pid)
        bv = run_gate(values.host, values.port, values.timeout, values.evidence_dir)
    except (GateError, OSError, TimeoutError) as error:
        parser.error(str(error))
    print(
        f"{PASS_PREFIX} baidu_home=pass baidu_search=pass "
        f"bilibili_home=pass bilibili_detail=pass bv={bv} tls=verified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
