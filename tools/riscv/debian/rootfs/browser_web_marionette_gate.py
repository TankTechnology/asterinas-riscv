#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fail-closed Marionette gate for real Baidu and Bilibili HTTPS content."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
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
FIXTURE_INDEX_PATH = "/browser-quality/index.html"
FIXTURE_IMAGE_PATH = "/browser-quality/pattern.png"
FIXTURE_SOURCE_PATH = "/asterinas-network-probe.bin"
FIXTURE_DOWNLOAD_PATH = "/browser-quality/download.bin"
FIXTURE_DOWNLOAD_FILE = Path(
    "/home/asterinas/Downloads/asterinas-browser-quality.bin"
)
FIXTURE_DOWNLOAD_BYTES = 256 * 1024
FIXTURE_DOWNLOAD_SHA256 = (
    "2312394bd99545d9de131c24efb781e765ac1aec243f2ed9347597a793a415e9"
)
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
CHALLENGE_HOSTS = frozenset({"wappass.baidu.com"})
BV_RE = re.compile(r"^https://www\.bilibili\.com/video/(BV[0-9A-Za-z]+)/?(?:[?#].*)?$")
MAX_RESOURCES = 256
DETAIL_DIAGNOSTIC_MARKER = Path("/run/asterinas-browser-web-detail-phase")


def _timeline(marker: str, firefox_pid: int, page: str | None = None) -> None:
    try:
        guest_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        guest_ns = time.monotonic_ns()
    line = (
        f"A_WEB_TIMELINE marker={marker} guest_monotonic_ns={guest_ns} "
        f"firefox_pid={firefox_pid}"
    )
    if page is not None:
        line += f" page={page}"
    print(line, file=sys.stderr, flush=True)

_PROBE_SCRIPT = r"""const host = location.hostname;
// The repository-owned fixture is the first readiness boundary.  Keep this
// script deliberately tiny: on slow RISC-V guests even harmless selector
// enumeration over a document with pending image loads can starve Marionette.
// Public pages still use the richer branch below after fixture readiness.
if (location.pathname.startsWith('/browser-quality/')) {
  const output = document.querySelector('#quality-capabilities');
  let browserCapabilities = null;
  if (output !== null && output.textContent !== '') {
    try { browserCapabilities = JSON.parse(output.textContent); } catch (_) {
      browserCapabilities = {state: 'malformed'};
    }
  }
  const image = document.querySelector('img[alt="pattern"]');
  return JSON.stringify({
    url: location.href,
    title: document.title,
    readyState: document.readyState,
    bodyText: (document.body === null ? '' : (document.body.textContent || '')).slice(0, 2048),
    jsComplete: (() => { window.__asterinasWebGate = 6 * 7; return window.__asterinasWebGate === 42; })(),
    browserCapabilities,
    dom: {
      baiduLogo: false,
      baiduKeyword: false,
      baiduSubmit: false,
      baiduResults: 0,
      bilibiliHome: false,
      bilibiliDetail: false,
      fixtureQuery: document.querySelector('form input[name="q"]') !== null,
      fixtureImage: image !== null && image.complete && image.naturalWidth === 32,
      fixtureSecond: document.querySelector('a[href="/browser-quality/second.html"]') !== null
    }
  });
}
return JSON.stringify({
  // Readiness probes run against script-heavy public pages.  Keep this pass
  // deliberately small: the full NavigationTiming/resource snapshot is
  // collected only after readiness succeeds.
  url: location.href,
  title: document.title,
  readyState: document.readyState,
  // textContent avoids synchronous layout work and a per-node JS loop.  The
  // probe runs repeatedly on large, script-heavy public pages, so keep it
  // allocation-bounded while
  // retaining enough text to reject empty/challenge documents.
  bodyText: document.body === null ? '' : (document.body.textContent || '').slice(0, 2048),
  jsComplete: (() => { window.__asterinasWebGate = 6 * 7; return window.__asterinasWebGate === 42; })(),
  browserCapabilities: (() => {
    const output = document.querySelector('#quality-capabilities');
    if (output === null || output.textContent === '') return null;
    try { return JSON.parse(output.textContent); } catch (_) { return {state: 'malformed'}; }
  })(),
  dom: {
    baiduLogo: (() => {
      const candidates = Array.from(document.querySelectorAll(
        '#lg img, img[src*="baidu" i], img[alt*="Baidu" i], img[alt*="百度"], img[aria-label*="Baidu" i], img[aria-label*="百度"], img[title*="Baidu" i], img[title*="百度"]'
      )).slice(0, 16);
      return candidates.some((image) => image.complete && image.naturalWidth > 0);
    })(),
    baiduKeyword: document.querySelector('#kw') !== null,
    baiduSubmit: document.querySelector('#su') !== null,
    // A presence check avoids enumerating hundreds of dynamic result nodes;
    // the detailed snapshot performs the bounded count after the page is
    // already known to be responsive.
    baiduResults: host === 'www.baidu.com' && location.pathname === '/s' && document.querySelector('#content_left') !== null ? 1 : 0,
    bilibiliHome: document.querySelector('a[href*="/video/BV"], main, #app') !== null,
    bilibiliDetail: document.querySelector('video, .bpx-player-container, #bilibili-player') !== null,
    fixtureQuery: document.querySelector('form input[name="q"]') !== null,
    fixtureImage: (() => { const image = document.querySelector('img[alt="pattern"]'); return image !== null && image.complete && image.naturalWidth === 32; })(),
    fixtureSecond: document.querySelector('a[href="/browser-quality/second.html"]') !== null
  }
});"""

_PUBLIC_LIGHT_PROBE_SCRIPT = r"""const host = location.hostname;
return JSON.stringify({
  url: location.href,
  title: document.title,
  readyState: document.readyState,
  // Avoid enumerating a large third-party DOM.  URL/title are sufficient for
  // challenge detection after the host has been checked by the validator.
  bodyText: ((document.title || '') + '\n' + location.href).slice(0, 512),
  jsComplete: (() => { window.__asterinasWebGate = 6 * 7; return window.__asterinasWebGate === 42; })(),
  browserCapabilities: null,
  dom: {
    baiduLogo: false,
    baiduKeyword: false,
    baiduSubmit: false,
    baiduResults: 0,
    bilibiliHome: host === 'www.bilibili.com' && document.querySelector('a[href*="/video/BV"], main, #app') !== null,
    bilibiliDetail: host === 'www.bilibili.com' && document.querySelector('video, .bpx-player-container, #bilibili-player') !== null,
    fixtureQuery: false,
    fixtureImage: false,
    fixtureSecond: false
  }
});"""

_SNAPSHOT_SCRIPT = r"""const detailPage = location.hostname === 'www.bilibili.com' && location.pathname.startsWith('/video/');
return JSON.stringify({
  url: location.href,
  title: document.title,
  readyState: document.readyState,
  // Keep evidence collection layout-free as well.  The Bilibili home page can
  // contain a very large hydrated tree; its lightweight snapshot only needs
  // enough text for challenge/empty-document checks.  The full branch remains
  // available for Baidu and the deterministic fixture evidence.
  bodyText: (arguments[0] && arguments[0].lightweight) ?
    ((document.title || '') + '\\n' + location.href).slice(0, 512) :
    (document.body === null ? '' : (document.body.textContent || '').slice(0, 8192)),
  jsComplete: (() => { window.__asterinasWebGate = 6 * 7; return window.__asterinasWebGate === 42; })(),
  browserCapabilities: (() => {
    const output = document.querySelector('#quality-capabilities');
    if (output === null || output.textContent === '') return null;
    try { return JSON.parse(output.textContent); } catch (_) { return {state: 'malformed'}; }
  })(),
  dom: {
    baiduLogo: (() => {
      const candidates = Array.from(document.querySelectorAll(
        '#lg img, img[src*="baidu" i], img[alt*="Baidu" i], img[alt*="百度"], img[aria-label*="Baidu" i], img[aria-label*="百度"], img[title*="Baidu" i], img[title*="百度"]'
      )).slice(0, 16);
      return candidates.some((image) => image.complete && image.naturalWidth > 0);
    })(),
    baiduKeyword: document.querySelector('#kw') !== null,
    baiduSubmit: document.querySelector('#su') !== null,
    baiduResults: document.querySelectorAll('#content_left .result, #content_left .c-container').length,
    bilibiliHome: document.querySelector('a[href*="/video/BV"], main, #app') !== null,
    bilibiliDetail: document.querySelector('video, .bpx-player-container, #bilibili-player') !== null,
    fixtureQuery: document.querySelector('form input[name="q"]') !== null,
    fixtureImage: (() => { const image = document.querySelector('img[alt="pattern"]'); return image !== null && image.complete && image.naturalWidth === 32; })(),
    fixtureSecond: document.querySelector('a[href="/browser-quality/second.html"]') !== null
  },
  // Detail pages contain a very large recommendation graph.  It is not used
  // for validation after the BV identity has been selected, so avoid a full
  // DOM enumeration that can starve Marionette on slow RISC-V guests.
  links: detailPage ? [] : Array.from(document.querySelectorAll('a[href*="/video/BV"]')).slice(0, 128).map(a => a.href),
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
  resources: (arguments[0] && arguments[0].lightweight) ? [] :
    performance.getEntriesByType('resource').slice(0, detailPage ? 32 : 256).map(r => ({
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

_FIXTURE_SUBMIT_SCRIPT = r"""const query = document.querySelector('form input[name="q"]');
const submit = document.querySelector('form button');
if (query === null || submit === null || query.form === null ||
    submit.form !== query.form) {
  return 'missing-controls';
}
query.focus();
query.value = 'asterinas';
query.dispatchEvent(new Event('input', {bubbles: true}));
query.dispatchEvent(new Event('change', {bubbles: true}));
if (query.value !== 'asterinas') {
  return 'query-rejected';
}
setTimeout(() => {
  if (typeof query.form.requestSubmit === 'function') query.form.requestSubmit(submit);
  else submit.click();
}, 0);
return 'fixture-search-scheduled';"""

_FIXTURE_DOWNLOAD_SCRIPT = r"""const link = document.querySelector('#quality-download');
if (link === null || link.getAttribute('href') !== '/browser-quality/download.bin' ||
    link.getAttribute('download') !== 'asterinas-browser-quality.bin') {
  return 'missing-download';
}
setTimeout(() => link.click(), 0);
return 'fixture-download-scheduled';"""

_DOM_FIELDS = {
    "baiduLogo",
    "baiduKeyword",
    "baiduSubmit",
    "baiduResults",
    "bilibiliHome",
    "bilibiliDetail",
    "fixtureQuery",
    "fixtureImage",
    "fixtureSecond",
}


def _mapping(snapshot: object) -> dict[str, object]:
    expected = {
        "url", "title", "readyState", "bodyText", "jsComplete",
        "browserCapabilities", "dom",
        "links", "navigation", "resources",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != expected:
        raise GateError("web snapshot has unexpected fields")
    return snapshot


def _probe_mapping(probe: object) -> dict[str, object]:
    expected = {
        "url", "title", "readyState", "bodyText", "jsComplete",
        "browserCapabilities", "dom",
    }
    probe_keys = set(probe) if isinstance(probe, dict) else set()
    if probe_keys != expected and probe_keys != expected | {"apiTypes"}:
        raise GateError("web readiness probe has unexpected fields")
    if "apiTypes" in probe:
        api_types = probe["apiTypes"]
        if (
            not isinstance(api_types, dict)
            or set(api_types) != {"wasm", "worker", "indexedDb", "audio", "fetch"}
            or not all(isinstance(value, str) for value in api_types.values())
        ):
            raise GateError("web readiness capability types are malformed")
    dom = probe["dom"]
    if not isinstance(dom, dict) or set(dom) != _DOM_FIELDS:
        raise GateError("web readiness DOM evidence is malformed")
    return probe


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


def _validate_probe_common(probe: object, *, host: str) -> dict[str, object]:
    result = _probe_mapping(probe)
    url = result["url"]
    if not isinstance(url, str):
        raise GateError("web readiness URL is malformed")
    parsed = urlparse(url)
    if parsed.hostname in CHALLENGE_HOSTS:
        raise GateError(f"challenge host observed: url={url!r}")
    if parsed.scheme != "https" or parsed.hostname != host:
        raise GateError(
            "web readiness did not retain the required HTTPS origin: "
            f"url={url!r} required_host={host!r}"
        )
    if result["readyState"] != "complete" or result["jsComplete"] is not True:
        raise GateError(
            "web readiness or JavaScript is incomplete: "
            f"readyState={result['readyState']!r} "
            f"jsComplete={result['jsComplete']!r}"
        )
    if result["browserCapabilities"] is not None:
        raise GateError("public web page unexpectedly supplied fixture capabilities")
    _reject_challenge(result)
    return result


def probe_baidu_home(probe: object) -> None:
    result = _validate_probe_common(probe, host="www.baidu.com")
    dom = result["dom"]
    assert isinstance(dom, dict)
    if (
        dom["baiduKeyword"] is not True
        or dom["baiduSubmit"] is not True
        or dom["baiduLogo"] is not True
    ):
        raise GateError("Baidu home search controls or logo are not ready")


def probe_baidu_search(probe: object) -> None:
    result = _validate_probe_common(probe, host="www.baidu.com")
    parsed = urlparse(str(result["url"]))
    if parse_qs(parsed.query).get("wd") != ["Asterinas"]:
        raise GateError("Baidu search readiness URL does not contain the exact query")
    dom = result["dom"]
    assert isinstance(dom, dict)
    count = dom["baiduResults"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise GateError("Baidu search result DOM is not ready")


def probe_bilibili_home(probe: object) -> None:
    result = _validate_probe_common(probe, host="www.bilibili.com")
    dom = result["dom"]
    assert isinstance(dom, dict)
    if dom["bilibiliHome"] is not True:
        raise GateError("Bilibili home DOM is not ready")


def probe_bilibili_detail(probe: object, expected_url: str) -> None:
    result = _validate_probe_common(probe, host="www.bilibili.com")
    expected = BV_RE.fullmatch(expected_url)
    actual = BV_RE.fullmatch(str(result["url"]))
    if expected is None or actual is None or actual.group(1) != expected.group(1):
        raise GateError("Bilibili detail readiness lost the selected live BV identity")
    dom = result["dom"]
    assert isinstance(dom, dict)
    if dom["bilibiliDetail"] is not True:
        raise GateError("Bilibili player/detail DOM is not ready")


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
    if parsed.hostname in CHALLENGE_HOSTS:
        raise GateError(f"challenge host observed: url={url!r}")
    if parsed.scheme != "https" or parsed.hostname != host:
        raise GateError(
            "web navigation did not retain the required HTTPS origin: "
            f"url={url!r} required_host={host!r}"
        )
    if result["readyState"] != "complete" or result["jsComplete"] is not True:
        raise GateError(
            "web document or JavaScript is incomplete: "
            f"readyState={result['readyState']!r} "
            f"jsComplete={result['jsComplete']!r}"
        )
    if result["browserCapabilities"] is not None:
        raise GateError("public web page unexpectedly supplied fixture capabilities")
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
    if not isinstance(dom, dict) or set(dom) != _DOM_FIELDS:
        raise GateError("web DOM evidence is malformed")
    return result


def validate_baidu_home(
    snapshot: object, *, require_tls_handshake: bool = True
) -> None:
    result = _validate_common(
        snapshot,
        host="www.baidu.com",
        require_tls_handshake=require_tls_handshake,
    )
    dom = result["dom"]
    assert isinstance(dom, dict)
    if (
        dom["baiduKeyword"] is not True
        or dom["baiduSubmit"] is not True
        or dom["baiduLogo"] is not True
    ):
        raise GateError("Baidu home search controls or logo are absent")


def validate_baidu_search(snapshot: object) -> None:
    result = _validate_common(snapshot, host="www.baidu.com", require_tls_handshake=False)
    parsed = urlparse(str(result["url"]))
    if parsed.path != "/s" or parse_qs(parsed.query).get("wd") != ["Asterinas"]:
        raise GateError("Baidu search URL does not contain the exact query")
    title = result["title"]
    body = result["bodyText"]
    assert isinstance(title, str) and isinstance(body, str)
    if "asterinas" not in f"{title}\n{body}".casefold():
        raise GateError("Baidu search query content is absent")
    dom = result["dom"]
    assert isinstance(dom, dict)
    count = dom["baiduResults"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise GateError("Baidu returned no live search result DOM")


def fixture_index_url_from_environment() -> str:
    raw = os.environ.get("ASTERINAS_DESKTOP_FIXTURE_URL", "")
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError as error:
        raise GateError("browser fixture URL is malformed") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "10.0.2.2"
        or port != 17894
        or parsed.path != FIXTURE_SOURCE_PATH
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise GateError("browser fixture URL is outside the frozen slirp contract")
    return f"http://10.0.2.2:{port}{FIXTURE_INDEX_PATH}"


def _validate_fixture_document(document: object, expected_url: str) -> dict[str, object]:
    probe_fields = {
        "url", "title", "readyState", "bodyText", "jsComplete",
        "browserCapabilities", "dom",
    }
    result = (
        _probe_mapping(document)
        if isinstance(document, dict) and set(document) == probe_fields
        else _mapping(document)
    )
    if result["url"] != expected_url:
        raise GateError("fixture search did not retain the exact query URL")
    parsed = urlparse(expected_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "10.0.2.2"
        or parsed.port != 17894
        or parsed.path != FIXTURE_INDEX_PATH
        or parse_qs(parsed.query) != {"q": ["asterinas"]}
    ):
        raise GateError("fixture search URL is outside the frozen contract")
    if (
        result["title"] != "asterinas - Asterinas Browser Quality"
        or result["readyState"] != "complete"
        or result["jsComplete"] is not True
    ):
        raise GateError("fixture search document or JavaScript is incomplete")
    body = result["bodyText"]
    if not isinstance(body, str) or not all(
        token in body for token in ("Asterinas browser quality", "浏览器质量")
    ):
        raise GateError("fixture search lost its exact Latin/CJK content")
    dom = result["dom"]
    assert isinstance(dom, dict)
    if not all(dom[name] is True for name in (
        "fixtureQuery", "fixtureImage", "fixtureSecond"
    )):
        raise GateError("fixture form, PNG, or navigation link is not ready")
    _validate_fixture_capability_shape(result["browserCapabilities"], "search")
    return result


def _validate_fixture_capability_shape(capabilities: object, phase: str) -> None:
    expected_checks = (
        {"canvas", "cookie", "fetch", "localStorage", "sessionStorage"}
        if phase == "basic" else
        {"audio", "canvas", "cookie", "fetch", "indexedDb",
         "localStorage", "sessionStorage", "wasm", "worker"}
    )
    if not isinstance(capabilities, dict) or set(capabilities) != {
        "version", "phase", "state", "checks", "error"
    }:
        raise GateError("fixture browser capability evidence is malformed")
    if capabilities["version"] != 1 or capabilities["phase"] != phase:
        raise GateError(
            "fixture browser capabilities are incomplete: "
            f"phase={capabilities.get('phase')!r} "
            f"state={capabilities.get('state')!r} "
            f"error={capabilities.get('error')!r}"
        )
    if capabilities["state"] not in {"running", "complete", "error"}:
        raise GateError(
            "fixture browser capability state is malformed: "
            f"state={capabilities.get('state')!r}"
        )
    checks = capabilities["checks"]
    if (
        not isinstance(checks, dict)
        or set(checks) != expected_checks
        or not all(isinstance(value, bool) for value in checks.values())
    ):
        raise GateError("fixture browser capability checks are malformed")


def _validate_fixture_capabilities(capabilities: object, phase: str) -> None:
    _validate_fixture_capability_shape(capabilities, phase)
    assert isinstance(capabilities, dict)
    if capabilities["state"] != "complete" or capabilities["error"] is not None:
        raise GateError(
            "fixture browser capabilities are incomplete: "
            f"phase={capabilities.get('phase')!r} "
            f"state={capabilities.get('state')!r} "
            f"error={capabilities.get('error')!r}"
        )
    checks = capabilities["checks"]
    assert isinstance(checks, dict)
    if not all(value is True for value in checks.values()):
        raise GateError("fixture browser capability checks are incomplete")


def probe_fixture_search(probe: object, expected_url: str) -> None:
    _validate_fixture_document(probe, expected_url)


def probe_fixture_home(probe: object, expected_url: str) -> None:
    result = _probe_mapping(probe)
    if (
        result["url"] != expected_url
        or result["title"] != "Asterinas Browser Quality"
        or result["readyState"] != "complete"
        or result["jsComplete"] is not True
    ):
        raise GateError(
            "fixture home document or JavaScript is incomplete: "
            f"url={result['url']!r} title={result['title']!r} "
            f"readyState={result['readyState']!r} jsComplete={result['jsComplete']!r}"
        )
    body = result["bodyText"]
    if not isinstance(body, str) or not all(
        token in body for token in ("Asterinas browser quality", "浏览器质量")
    ):
        raise GateError("fixture home lost its exact Latin/CJK content")
    dom = result["dom"]
    assert isinstance(dom, dict)
    if not all(dom[name] is True for name in (
        "fixtureQuery", "fixtureImage", "fixtureSecond"
    )):
        raise GateError("fixture home form, PNG, or navigation link is not ready")
    _validate_fixture_capability_shape(result["browserCapabilities"], "home")


def probe_fixture_capabilities(probe: object, expected_url: str) -> None:
    """Validate optional APIs on a dedicated page, isolated from DOM probes."""

    result = _probe_mapping(probe)
    if (
        result["url"] != expected_url
        or result["title"] != "Asterinas Browser Quality"
        or result["readyState"] != "complete"
        or result["jsComplete"] is not True
    ):
        raise GateError("fixture capability document or JavaScript is incomplete")
    phase = "basic" if "capabilities=basic" in expected_url else "home"
    _validate_fixture_capabilities(result["browserCapabilities"], phase)


def validate_fixture_search(snapshot: object, expected_url: str) -> None:
    result = _validate_fixture_document(snapshot, expected_url)
    navigation = result["navigation"]
    if (
        not isinstance(navigation, dict)
        or navigation.get("entryType") != "navigation"
        or navigation.get("name") != expected_url
    ):
        raise GateError("fixture NavigationTiming evidence is missing")
    resources = result["resources"]
    if not isinstance(resources, list) or not 0 < len(resources) <= MAX_RESOURCES:
        raise GateError("fixture ResourceTiming evidence is missing or oversized")
    expected_image = f"http://10.0.2.2:17894{FIXTURE_IMAGE_PATH}"
    image_seen = False
    for resource in resources:
        if not isinstance(resource, dict) or set(resource) != {
            "name", "initiatorType", "duration", "transferSize"
        }:
            raise GateError("fixture ResourceTiming evidence is malformed")
        name = resource["name"]
        if not isinstance(name, str):
            raise GateError("fixture resource URL is malformed")
        parsed = urlparse(name)
        if parsed.scheme == "data":
            continue
        if parsed.scheme != "http" or parsed.hostname != "10.0.2.2" or parsed.port != 17894:
            raise GateError("fixture loaded a resource outside its frozen origin")
        image_seen = image_seen or name == expected_image
    if not image_seen:
        raise GateError("fixture PNG ResourceTiming evidence is missing")


def validate_baidu_challenge(snapshot: object) -> None:
    result = _mapping(snapshot)
    url = result["url"]
    if not isinstance(url, str):
        raise GateError("Baidu challenge URL is malformed")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    back_urls = query.get("backurl", [])
    if (
        parsed.scheme != "https"
        or parsed.hostname != "wappass.baidu.com"
        or not parsed.path.startswith("/static/captcha/")
        or len(back_urls) != 1
    ):
        raise GateError("Baidu challenge does not match the exact external contract")
    back = urlparse(back_urls[0])
    if (
        back.scheme != "https"
        or back.hostname != "www.baidu.com"
        or back.path != "/s"
        or parse_qs(back.query).get("wd") != ["Asterinas"]
    ):
        raise GateError("Baidu challenge back URL lost the submitted query")
    if result["readyState"] != "complete" or result["jsComplete"] is not True:
        raise GateError("Baidu challenge document or JavaScript is incomplete")
    navigation = result["navigation"]
    if (
        not isinstance(navigation, dict)
        or navigation.get("entryType") != "navigation"
        or navigation.get("name") != url
    ):
        raise GateError("Baidu challenge NavigationTiming evidence is missing")
    resources = result["resources"]
    if not isinstance(resources, list) or len(resources) > MAX_RESOURCES:
        raise GateError("Baidu challenge ResourceTiming evidence is malformed")
    for resource in resources:
        if not isinstance(resource, dict) or set(resource) != {
            "name", "initiatorType", "duration", "transferSize"
        }:
            raise GateError("Baidu challenge ResourceTiming record is malformed")
        name = resource["name"]
        if not isinstance(name, str) or urlparse(name).scheme not in {"https", "data"}:
            raise GateError("Baidu challenge loaded a non-HTTPS external resource")


def validate_baidu_search_outcome(snapshot: object) -> str:
    try:
        validate_baidu_search(snapshot)
    except GateError:
        validate_baidu_challenge(snapshot)
        return "external-captcha"
    return "pass"


def select_bilibili_video(snapshot: object) -> str:
    result = _validate_common(snapshot, host="www.bilibili.com", require_tls_handshake=True)
    dom = result["dom"]
    assert isinstance(dom, dict)
    if dom["bilibiliHome"] is not True:
        raise GateError("Bilibili home DOM is absent")
    links = result["links"]
    if not isinstance(links, list):
        raise GateError("Bilibili links are malformed")
    # Keep the page's DOM order.  Lexicographically sorting live links can
    # select a preloaded recommendation whose detail page is a much heavier
    # anti-bot/media variant than the first rendered card.
    canonical: list[str] = []
    seen: set[str] = set()
    for link in links:
        if isinstance(link, str) and BV_RE.fullmatch(link) and link not in seen:
            seen.add(link)
            canonical.append(link)
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


def firefox_process_uid(firefox_pid: int) -> int:
    """Return the browser's real UID from the same proc status used by evidence."""

    try:
        status = Path(f"/proc/{firefox_pid}/status").read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as error:
        raise GateError("cannot inspect Firefox process owner") from error
    match = re.search(r"(?m)^Uid:\s+([0-9]+)(?:\s+[0-9]+){3}\s*$", status)
    if match is None or int(match.group(1)) != 1000:
        raise GateError("Firefox process owner is outside the user contract")
    return int(match.group(1))


def validate_gecko_profiler_environment(firefox_pid: int) -> None:
    expected = {
        "MOZ_PROFILER_STARTUP": "1",
        "MOZ_PROFILER_STARTUP_ENTRIES": "262144",
        "MOZ_PROFILER_STARTUP_INTERVAL": "10",
        "MOZ_PROFILER_STARTUP_FEATURES": "js,leaf,ipcmessages,processcpu",
        "MOZ_PROFILER_STARTUP_FILTERS": (
            "GeckoMain,DOM Worker,Compositor,Renderer,Socket Thread,"
            "SwComposite,MediaDecoderStateMachine"
        ),
    }
    try:
        entries = Path(f"/proc/{firefox_pid}/environ").read_bytes().split(b"\0")
    except OSError as error:
        raise GateError("cannot inspect Firefox profiler environment") from error
    environment: dict[str, str] = {}
    for entry in entries:
        key, separator, value = entry.partition(b"=")
        if not separator:
            continue
        environment[key.decode("utf-8", "replace")] = value.decode(
            "utf-8", "replace"
        )
    for name, value in expected.items():
        if environment.get(name) != value:
            raise GateError(f"Firefox profiler environment mismatch: {name}")


def _snapshot(client: Marionette, *, lightweight: bool = False) -> dict[str, object]:
    response = client.command("WebDriver:ExecuteScript", {
        "script": _SNAPSHOT_SCRIPT,
        "args": [{"lightweight": lightweight}],
        "newSandbox": True,
        "sandbox": "default",
        "line": 1,
        "filename": "asterinas-browser-web-gate",
    })
    value = _script_value(response)
    if not isinstance(value, str):
        detail = repr(response)
        if len(detail) > 256:
            detail = detail[:253] + "..."
        raise GateError(
            "web snapshot script returned no JSON: "
            f"response_type={type(response).__name__} response={detail}"
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise GateError("web snapshot script returned malformed JSON") from error
    return _mapping(parsed)


def _probe(client: Marionette, *, lightweight: bool = False) -> dict[str, object]:
    response = client.command("WebDriver:ExecuteScript", {
        "script": _PUBLIC_LIGHT_PROBE_SCRIPT if lightweight else _PROBE_SCRIPT,
        "args": [],
        "line": 1,
        "filename": "asterinas-browser-web-readiness",
    })
    value = _script_value(response)
    if not isinstance(value, str):
        detail = repr(response)
        if len(detail) > 256:
            detail = detail[:253] + "..."
        raise GateError(
            "web readiness script returned no JSON: "
            f"response_type={type(response).__name__} response={detail}"
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise GateError("web readiness script returned malformed JSON") from error
    return _probe_mapping(parsed)


def _script_value(response: object) -> object:
    """Normalize Marionette ExecuteScript responses across protocol variants.

    Firefox normally wraps the script result as ``{"value": ...}``, while
    older/alternate Marionette endpoints return the value directly.  Both are
    semantically equivalent; accepting the raw form avoids rejecting a valid
    page snapshot before content validation runs.
    """
    if isinstance(response, dict) and "value" in response:
        return response["value"]
    return response


def _navigate(client: Marionette, url: str) -> None:
    response = client.command("WebDriver:Navigate", {"url": url})
    if _script_value(response) is not None:
        raise GateError("Marionette returned an invalid Navigate result")


def probe_about_blank(probe: object) -> None:
    """Confirm that the previous document has been unloaded.

    The web gate uses ``pageLoadStrategy=none`` so a script-heavy public page
    can continue work while the next navigation is requested.  A short
    about:blank hop gives Gecko an explicit unload boundary before entering
    the deterministic fixture; this prevents third-party timers and fetches
    from starving the fixture's readiness probe on the slow RISC-V guest.
    """
    result = _probe_mapping(probe)
    if result["url"] != "about:blank":
        raise GateError("browser did not reach the about:blank unload boundary")
    if result["readyState"] not in {"interactive", "complete"}:
        raise GateError("about:blank document is not ready")
    if result["jsComplete"] is not True:
        raise GateError("about:blank JavaScript probe is incomplete")


def _clear_document(client: Marionette, deadline: float) -> None:
    # WebDriver:Navigate to about:blank can itself wait behind a busy
    # third-party script even with pageLoadStrategy=none.  Stop network work
    # and remove the live DOM in-place; this is a bounded unload barrier that
    # does not ask Marionette to synchronously commit a second navigation.
    # The next controlled navigation then installs a fresh document.
    response = client.command("WebDriver:ExecuteScript", {
        "script": "window.stop(); const root = document.documentElement; "
        "if (root !== null) root.replaceChildren(); return 'document-stopped';",
        "args": [],
        "newSandbox": True,
        "sandbox": "default",
        "line": 1,
        "filename": "asterinas-public-unload",
    })
    if _script_value(response) != "document-stopped":
        raise GateError("public document could not be stopped")


def _submit_baidu_search(client: Marionette) -> None:
    response = client.command("WebDriver:ExecuteScript", {
        "script": _BAIDU_SUBMIT_SCRIPT,
        "args": [],
        "newSandbox": True,
        "sandbox": "default",
        "line": 1,
        "filename": "asterinas-baidu-search-submit",
    })
    value = _script_value(response)
    if value != "search-click-scheduled":
        raise GateError("Baidu homepage search form could not be submitted")


def _submit_fixture_search(client: Marionette) -> None:
    response = client.command("WebDriver:ExecuteScript", {
        "script": _FIXTURE_SUBMIT_SCRIPT,
        "args": [],
        "newSandbox": True,
        "sandbox": "default",
        "line": 1,
        "filename": "asterinas-fixture-search-submit",
    })
    value = _script_value(response)
    if value != "fixture-search-scheduled":
        raise GateError("controlled fixture search form could not be submitted")


def _trigger_fixture_download(client: Marionette) -> None:
    if FIXTURE_DOWNLOAD_FILE.exists() or FIXTURE_DOWNLOAD_FILE.is_symlink():
        raise GateError("controlled fixture download has stale state")
    response = client.command("WebDriver:ExecuteScript", {
        "script": _FIXTURE_DOWNLOAD_SCRIPT,
        "args": [],
        "newSandbox": True,
        "sandbox": "default",
        "line": 1,
        "filename": "asterinas-fixture-download",
    })
    if _script_value(response) != "fixture-download-scheduled":
        raise GateError("controlled fixture download link could not be activated")


def _start_fixture_capabilities(client: Marionette) -> None:
    response = client.command("WebDriver:ExecuteScript", {
        "script": "window.setTimeout(() => document.dispatchEvent(new Event('asterinas-basic-capabilities-start')), 0); "
        "return 'basic-capabilities-started';",
        "args": [],
        # Dispatching a DOM event reaches the page from a fresh sandbox and
        # avoids relying on a function binding that may belong to another
        # Marionette sandbox instance.
        "newSandbox": True,
        "sandbox": "default",
        "line": 1,
        "filename": "asterinas-browser-basic-capabilities",
    })
    value = _script_value(response)
    # Some Firefox ESR builds normalize an ExecuteScript result from a
    # sandboxed asynchronous dispatch to JSON null even though the event was
    # queued successfully. The subsequent capability probe is authoritative.
    if value not in {"basic-capabilities-started", None}:
        print(
            f"A_WEB_CAPABILITY_START_RESULT value={json.dumps(value, ensure_ascii=True)}",
            file=sys.stderr,
            flush=True,
        )
        raise GateError("controlled fixture capability runner could not be started")


def _wait_for_fixture_download(
    path: Path, deadline: float, evidence_dir: Path, expected_owner_uid: int
) -> dict[str, object]:
    while time.monotonic() < deadline:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_owner_uid
        ):
            raise GateError("controlled fixture download is not a safe regular file")
        if metadata.st_size != FIXTURE_DOWNLOAD_BYTES:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != FIXTURE_DOWNLOAD_SHA256:
            raise GateError("controlled fixture download hash does not match")
        result: dict[str, object] = {
            "bytes": FIXTURE_DOWNLOAD_BYTES,
            "filename": path.name,
            "sha256": digest,
        }
        evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (evidence_dir / "fixture-download.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return result
    raise GateError("controlled fixture download did not complete")


def _wait_for_probe(
    client: Marionette,
    validator: Callable[[object], object],
    deadline: float,
    recover: Callable[[GateError], None] | None = None,
    lightweight: bool = False,
) -> tuple[dict[str, object], object]:
    last_error: GateError | None = None
    capability_reported = False
    command_reported = False
    recovered = False
    reported_errors: set[str] = set()
    while time.monotonic() < deadline:
        probe: object | None = None
        try:
            # A navigation with pageLoadStrategy=none can transiently yield a
            # null/empty ExecuteScript result while Firefox replaces the
            # document's browsing context.  The readiness probe is deliberately
            # small and does not force layout, enumerate resource timing, or
            # capture the full evidence body.  Only one full snapshot is taken
            # after this loop reports the expected live DOM.
            if not command_reported:
                command_reported = True
                print("A_WEB_PROBE_COMMAND state=start", file=sys.stderr, flush=True)
            probe = _probe(client, lightweight=lightweight)
            print("A_WEB_PROBE_COMMAND state=done", file=sys.stderr, flush=True)
            return probe, validator(probe)
        except GateError as error:
            # Preserve the browser-side capability progress in serial evidence
            # instead of reducing a long-running readiness timeout to a vague
            # protocol error.  This remains diagnostic-only: validators stay
            # fail-closed and the marker is emitted at most once per wait.
            capabilities = probe.get("browserCapabilities") if isinstance(probe, dict) else None
            if (
                not capability_reported
                and isinstance(capabilities, dict)
                and capabilities.get("state") in {"running", "error"}
            ):
                capability_reported = True
                checks = capabilities.get("checks")
                checks_text = json.dumps(checks, sort_keys=True, separators=(",", ":"))
                print(
                    "A_WEB_PROBE_CAPABILITIES "
                    f"state={capabilities.get('state')} "
                    f"error={json.dumps(str(capabilities.get('error')), ensure_ascii=True)} "
                    f"checks={checks_text}",
                    file=sys.stderr,
                    flush=True,
                )
            if "challenge" in str(error) or "403" in str(error) or "access denial" in str(error):
                raise
            if recover is not None and not recovered and "about:blank" in str(error):
                recovered = True
                recover(error)
            error_text = str(error)
            if error_text not in reported_errors:
                reported_errors.add(error_text)
                print(
                    "A_WEB_PROBE_RETRY error="
                    + json.dumps(error_text, ensure_ascii=True),
                    file=sys.stderr,
                    flush=True,
                )
            last_error = error
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    raise GateError(f"web readiness probe did not become ready: {last_error}")


def _wait_across_windows(
    client: Marionette,
    probe_validator: Callable[[object], object],
    snapshot_validator: Callable[[object], object],
    deadline: float,
) -> tuple[dict[str, object], object]:
    """Wait for a matching live document in any bounded browser window.

    Public search pages may honor a click by opening a new tab.  The click is
    still the required user interaction, but subsequent WebDriver commands
    remain attached to the old browsing context until it is switched
    explicitly.  Enumerating every current handle avoids mistaking that normal
    browser behavior for a seven-minute page load.
    """

    last_error: GateError | None = None
    while time.monotonic() < deadline:
        handles = _script_value(client.command("WebDriver:GetWindowHandles"))
        if (
            not isinstance(handles, list)
            or not 0 < len(handles) <= 16
            or not all(isinstance(handle, str) and handle for handle in handles)
            or len(set(handles)) != len(handles)
        ):
            raise GateError("Marionette returned invalid browser window handles")
        for handle in handles:
            switched = client.command(
                "WebDriver:SwitchToWindow", {"handle": handle, "focus": False}
            )
            if _script_value(switched) is not None:
                raise GateError("Marionette returned an invalid window switch result")
            try:
                probe = _probe(client)
                probe_validator(probe)
                snapshot = _snapshot(client)
                return snapshot, snapshot_validator(snapshot)
            except GateError as error:
                if (
                    "challenge" in str(error)
                    or "403" in str(error)
                    or "access denial" in str(error)
                ):
                    raise
                last_error = error
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    raise GateError(f"web DOM did not become ready in any window: {last_error}")


def _wait_baidu_search_outcome(
    client: Marionette, deadline: float
) -> tuple[dict[str, object], str]:
    last_error: GateError | None = None
    while time.monotonic() < deadline:
        handles = _script_value(client.command("WebDriver:GetWindowHandles"))
        if (
            not isinstance(handles, list)
            or not 0 < len(handles) <= 16
            or not all(isinstance(handle, str) and handle for handle in handles)
            or len(set(handles)) != len(handles)
        ):
            raise GateError("Marionette returned invalid browser window handles")
        for handle in handles:
            switched = client.command(
                "WebDriver:SwitchToWindow", {"handle": handle, "focus": False}
            )
            if _script_value(switched) is not None:
                raise GateError("Marionette returned an invalid window switch result")
            try:
                probe = _probe(client)
                url = probe.get("url")
                if isinstance(url, str) and urlparse(url).hostname in CHALLENGE_HOSTS:
                    if probe["readyState"] != "complete" or probe["jsComplete"] is not True:
                        raise GateError("Baidu challenge readiness is incomplete")
                    # A challenge page only needs URL/navigation/DOM proof;
                    # enumerating its full ResourceTiming list can itself
                    # become the slowest operation on RISC-V.
                    snapshot = _snapshot(client, lightweight=True)
                    validate_baidu_challenge(snapshot)
                    return snapshot, "external-captcha"
                probe_baidu_search(probe)
                snapshot = _snapshot(client)
                validate_baidu_search(snapshot)
                return snapshot, "pass"
            except GateError as error:
                last_error = error
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    raise GateError(f"Baidu search outcome did not become ready: {last_error}")


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
    encoded = _script_value(response)
    if not isinstance(encoded, str):
        raise GateError("Marionette screenshot response is malformed")
    try:
        screenshot = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise GateError("Marionette screenshot is not canonical base64") from error
    if not screenshot.startswith(b"\x89PNG\r\n\x1a\n"):
        raise GateError("Marionette screenshot is not PNG")
    (directory / f"{name}.png").write_bytes(screenshot)


def run_gate(
    host: str,
    port: int,
    timeout: float,
    evidence_dir: Path,
    firefox_pid: int,
    *,
    require_tls_handshake: bool = True,
    basic_only: bool = False,
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    def phase(name: str, state: str, error: BaseException | None = None) -> None:
        line = f"A_WEB_PHASE phase={name} state={state} firefox_pid={firefox_pid}"
        if error is not None:
            line += (
                f" exception_type={type(error).__name__}"
                f" exception={json.dumps(str(error), ensure_ascii=True)}"
            )
        print(line, file=sys.stderr, flush=True)

    client = _connect(host, port, deadline, phase=phase)
    def run_phase(name: str, operation: Callable[[], object]) -> object:
        phase(name, "start")
        try:
            result = operation()
        except BaseException as error:
            phase(name, "exception", error)
            raise
        phase(name, "done")
        return result

    def command(stage: str, name: str, parameters: object | None = None) -> object:
        return run_phase(
            stage, lambda: _script_value(client.command(name, parameters))
        )
    firefox_uid = firefox_process_uid(firefox_pid)
    _timeline("BOOT_MARIONETTE_CONNECTED", firefox_pid)
    try:
        session = command("new-session", "WebDriver:NewSession", {
            "acceptInsecureCerts": False,
            "pageLoadStrategy": "none",
            "strictFileInteractability": True,
        })
        _timeline("BOOT_NEW_SESSION_DONE", firefox_pid)
        if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str):
            raise GateError("Marionette did not create a web session")
        capabilities = session.get("capabilities", {})
        if not isinstance(capabilities, dict) or capabilities.get("acceptInsecureCerts") is not False:
            raise GateError("Firefox did not preserve certificate verification")
        handles = command("window-handles", "WebDriver:GetWindowHandles")
        if not isinstance(handles, list) or not handles or not all(
            isinstance(handle, str) for handle in handles
        ):
            raise GateError("Firefox created no first browser window")
        _timeline("BOOT_FIRST_WINDOW_READY", firefox_pid)

        # Run the deterministic fixture before visiting script-heavy public
        # pages.  This keeps the capability/download contract independent of
        # third-party timers that can otherwise starve Marionette commands.
        fixture_index = fixture_index_url_from_environment()
        fixture_search_url = f"{fixture_index}?q=asterinas"
        run_phase("navigate-fixture-home", lambda: _navigate(client, fixture_index))
        run_phase(
            "probe-fixture-home",
            lambda: _wait_for_probe(
                client, lambda probe: probe_fixture_home(probe, fixture_index), deadline
            ),
        )
        run_phase("submit-fixture-search", lambda: _submit_fixture_search(client))
        run_phase(
            "probe-fixture-search",
            lambda: _wait_for_probe(
                client,
                lambda probe: probe_fixture_search(probe, fixture_search_url),
                deadline,
            ),
        )
        fixture_search = run_phase(
            "snapshot-fixture-search", lambda: _snapshot(client)
        )
        assert isinstance(fixture_search, dict)
        validate_fixture_search(fixture_search, fixture_search_url)
        _timeline("BOOT_DOM_READY", firefox_pid, "fixture-search")
        run_phase(
            "evidence-fixture-search",
            lambda: _write_evidence(
                client, evidence_dir, "fixture-search", fixture_search
            ),
        )
        run_phase(
            "trigger-fixture-download", lambda: _trigger_fixture_download(client)
        )
        run_phase(
            "verify-fixture-download",
            lambda: _wait_for_fixture_download(
                FIXTURE_DOWNLOAD_FILE, deadline, evidence_dir, firefox_uid
            ),
        )

        if basic_only:
            capability_url = f"{fixture_index}?capabilities=basic"
            run_phase(
                "navigate-fixture-capabilities",
                lambda: _navigate(client, capability_url),
            )
            run_phase(
                "start-fixture-capabilities",
                lambda: _start_fixture_capabilities(client),
            )
            capability_probe = run_phase(
                "probe-fixture-capabilities",
                lambda: _wait_for_probe(
                    client,
                    lambda probe: probe_fixture_capabilities(probe, capability_url),
                    deadline,
                ),
            )
            capability_snapshot = run_phase(
                "snapshot-fixture-capabilities", lambda: _snapshot(client)
            )
            assert isinstance(capability_snapshot, dict)
            probe_fixture_capabilities(capability_probe[0], capability_url)
            _validate_fixture_capabilities(
                capability_snapshot.get("browserCapabilities"), "basic"
            )
            run_phase(
                "evidence-fixture-capabilities",
                lambda: _write_evidence(
                    client,
                    evidence_dir,
                    "fixture-capabilities",
                    capability_snapshot,
                ),
            )
            print(
                "DEBIAN_BROWSER_WEB_PLATFORM_READY_BASIC "
                "fixture_search=pass capabilities=pass download=pass",
                file=sys.stderr,
                flush=True,
            )
            # Deleting a Marionette session synchronously can block behind a
            # CPU-bound RISC-V Firefox process (the direct QEMU path observed
            # this after all fixture work had already passed).  Session
            # deletion is cleanup, not acceptance evidence; close the bounded
            # loopback socket instead and let systemd own Firefox lifecycle.
            # The caller still performs the process/PID/security checks below.
            client.close()
            return "", "basic"

        command("navigate-baidu-home", "WebDriver:Navigate", {"url": BAIDU_HOME})
        # Native WebDriver metadata is a cheap control probe.  If this phase
        # completes while ExecuteScript below does not, the stall is in
        # Firefox's JS evaluation/sandbox path rather than navigation or the
        # kernel's socket transport.
        run_phase(
            "title-baidu-home",
            lambda: client.command("WebDriver:GetTitle"),
        )
        run_phase(
            "probe-baidu-home",
            lambda: _wait_for_probe(
                client,
                probe_baidu_home,
                deadline,
                recover=lambda _error: _navigate(client, BAIDU_HOME),
            ),
        )
        baidu_home = run_phase("snapshot-baidu-home", lambda: _snapshot(client))
        assert isinstance(baidu_home, dict)
        validate_baidu_home(
            baidu_home, require_tls_handshake=require_tls_handshake
        )
        api_types = baidu_home.get("apiTypes")
        if isinstance(api_types, dict) and all(
            isinstance(api_types.get(name), str)
            for name in ("wasm", "worker", "indexedDb", "audio", "fetch")
        ):
            print(
                "A_WEB_CAPABILITY_TYPES "
                + " ".join(f"{name}={api_types[name]}" for name in (
                    "wasm", "worker", "indexedDb", "audio", "fetch"
                )),
                file=sys.stderr,
                flush=True,
            )
        _timeline("BOOT_DOM_READY", firefox_pid, "baidu-home")
        run_phase(
            "evidence-baidu-home",
            lambda: _write_evidence(client, evidence_dir, "baidu-home", baidu_home),
        )

        run_phase("navigate-bilibili-home", lambda: _navigate(client, BILIBILI_HOME))
        run_phase(
            "probe-bilibili-home",
            lambda: _wait_for_probe(
                client, probe_bilibili_home, deadline, lightweight=True
            ),
        )
        bilibili_home = run_phase(
            "snapshot-bilibili-home", lambda: _snapshot(client, lightweight=True)
        )
        assert isinstance(bilibili_home, dict)
        selected = select_bilibili_video(bilibili_home)
        assert isinstance(selected, str)
        _timeline("BOOT_DOM_READY", firefox_pid, "bilibili-home")
        run_phase(
            "evidence-bilibili-home",
            lambda: _write_evidence(
                client, evidence_dir, "bilibili-home", bilibili_home
            ),
        )

        print(f"A_WEB_SELECTED_BV url={selected}", file=sys.stderr, flush=True)
        if os.environ.get("ASTERINAS_BROWSER_WEB_PROC_DIAGNOSTIC") == "1":
            DETAIL_DIAGNOSTIC_MARKER.write_text(selected + "\n", encoding="utf-8")
        if os.environ.get("ASTERINAS_FIREFOX_GECKO_PROFILE") == "1":
            run_phase(
                "gecko-profiler-verify",
                lambda: validate_gecko_profiler_environment(firefox_pid),
            )
        run_phase("navigate-bilibili-detail", lambda: _navigate(client, selected))
        run_phase(
            "probe-bilibili-detail",
            lambda: _wait_for_probe(
                client,
                lambda probe: probe_bilibili_detail(probe, selected),
                deadline,
                lightweight=True,
            ),
        )
        bilibili_detail = run_phase(
            "snapshot-bilibili-detail", lambda: _snapshot(client)
        )
        assert isinstance(bilibili_detail, dict)
        validate_bilibili_detail(bilibili_detail, selected)
        _timeline("BOOT_DOM_READY", firefox_pid, "bilibili-detail")
        run_phase(
            "evidence-bilibili-detail",
            lambda: _write_evidence(
                client, evidence_dir, "bilibili-detail", bilibili_detail
            ),
        )
        selected_bv = BV_RE.fullmatch(selected).group(1)  # type: ignore[union-attr]
        print(
            "DEBIAN_BROWSER_WEB_PLATFORM_READY "
            f"baidu_home=pass bilibili_home=pass bilibili_detail=pass bv={selected_bv} "
            "tls=verified",
            file=sys.stderr,
            flush=True,
        )

        # Keep the public anti-automation probe strict, but run it after the
        # deterministic platform evidence so a third-party CAPTCHA cannot hide
        # whether Firefox rendered and inspected both public sites correctly.
        run_phase("navigate-baidu-search-home", lambda: _navigate(client, BAIDU_HOME))
        run_phase(
            "probe-baidu-search-home",
            lambda: _wait_for_probe(client, probe_baidu_home, deadline),
        )
        run_phase("submit-baidu-search", lambda: _submit_baidu_search(client))
        baidu_search_result = run_phase(
            "snapshot-baidu-search",
            lambda: _wait_baidu_search_outcome(client, deadline),
        )
        assert isinstance(baidu_search_result, tuple)
        baidu_search, baidu_search_outcome = baidu_search_result
        _timeline("BOOT_DOM_READY", firefox_pid, "baidu-search")
        run_phase(
            "evidence-baidu-search",
            lambda: _write_evidence(client, evidence_dir, "baidu-search", baidu_search),
        )
        deleted = _script_value(client.command("WebDriver:DeleteSession"))
        if deleted is not None:
            raise GateError("Marionette returned an invalid session deletion result")
        return selected_bv, baidu_search_outcome
    finally:
        client.close()


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="browser_web_marionette_gate")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2828)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--firefox-pid", type=int, required=True)
    parser.add_argument("--basic-only", action="store_true")
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
        bv, baidu_outcome = run_gate(
            values.host,
            values.port,
            values.timeout,
            values.evidence_dir,
            values.firefox_pid,
            require_tls_handshake=(
                os.environ.get("ASTERINAS_WEB_NETWORK_MODE", "direct")
                == "direct"
            ),
            basic_only=values.basic_only,
        )
    except (GateError, OSError, TimeoutError) as error:
        parser.error(str(error))
    if values.basic_only:
        print(
            f"{PASS_PREFIX} fixture_search=pass capabilities=pass download=pass "
            "public_sites=not-run",
        )
        return 0
    print(
        f"{PASS_PREFIX} fixture_search=pass baidu_home=pass "
        f"baidu_search=observed bilibili_home=pass bilibili_detail=pass "
        f"bv={bv} tls=verified baidu_outcome={baidu_outcome} "
        "capabilities=pass download=pass"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
