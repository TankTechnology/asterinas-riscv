#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run one bounded local Firefox WebRender scene on the DRM desktop."""

from __future__ import annotations

import base64
import hashlib
from html import unescape
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

if Path("/usr/lib/asterinas/browser_m5_marionette_gate.py").is_file():
    sys.path.insert(0, "/usr/lib/asterinas")
    from browser_m5_marionette_gate import GateError, _connect
else:  # host-side syntax/tests
    # A direct `python3 tools/.../firefox_drm_scene_gate.py` invocation puts
    # only this script's directory on sys.path. Bootstrap the repository root
    # so the host path works from any current working directory as well as
    # through the unit-test package import.
    repository_root = Path(__file__).resolve().parents[4]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from tools.riscv.debian.rootfs.browser_m5_marionette_gate import GateError, _connect


DISPLAY = ":0"
XAUTHORITY = "/home/asterinas/.Xauthority"
PROFILE = Path("/home/asterinas/.mozilla/asterinas-firefox-drm-scene")
SCENE_URL = "file:///usr/share/asterinas/firefox-drm-scene.html"
SUPPORT_URL = "about:support"
# r16 established that the local scene URL lets the cold ESR session reach
# NewSession on this DRM guest. A bounded A/B probe showed that replacing it
# with about:blank stalls before NewSession, so keep the known-good start URL
# and explicitly bind a browsing context before issuing scene commands.
START_URL = os.environ.get("ASTERINAS_FIREFOX_START_URL", SCENE_URL).strip() or SCENE_URL
MARIONETTE_PORT = 2828
# Diagnostic-only crash reporter installed by firefox_scene_crash_overlay.json.
CRASH_PROBE_PATH = "/usr/lib/asterinas/firefox_crash_probe.so"
# Cold ESR startup under RISC-V TCG has measured 249--458 seconds before the
# first NewSession response. Keep that phase bounded, but do not classify a
# known cold-start interval as a renderer failure. The service budget covers
# one scene run plus one bounded recovery launch.
SCENE_TIMEOUT = 900.0
NEW_SESSION_TIMEOUT = 840.0
SCENE_SETTLE_SECONDS = 30.0
SCENE_POLL_INTERVAL = 2.0
# The browser window is fixed at 1280x800 below. Marionette's non-full
# screenshot is the content viewport after Firefox chrome, which the guest
# measured as 1280x696 and 1278x690 across runs (the content area follows the
# toolkit's resize timing). The screenshot must equal the page's own
# innerWidth/innerHeight exactly; these bounds keep that viewport tied to the
# fixed outer window instead of accepting an arbitrary page.
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 800
SCREENSHOT_MIN_WIDTH = 1200
SCREENSHOT_MAX_WIDTH = 1280
SCREENSHOT_MIN_HEIGHT = 600
SCREENSHOT_MAX_HEIGHT = 760
# The gate runs as the unprivileged browser user and the DRM rootfs is
# intentionally read-mostly. Keep this disposable PNG on the guest's writable
# tmpfs instead of depending on ext2 writeback; the machine-readable receipt
# carries the hash and the image itself is not an artifact.
SCREENSHOT = Path("/tmp/asterinas-firefox-drm-scene.png")
# Keep both diagnostic logs on the writable tmpfs as well. The gate only
# publishes a bounded marker, but the renderer decision must not depend on
# ext2 writeback succeeding.
FIREFOX_LOG = Path("/tmp/asterinas-firefox-drm-scene-firefox.log")
FIREFOX_MOZ_LOG = Path("/tmp/asterinas-firefox-drm-scene-firefox.mozlog")
PROBE_RECEIPT = Path("/run/asterinas-firefox-marionette-probe.json")
PROBE_ONLY = os.environ.get("ASTERINAS_FIREFOX_PROBE_ONLY") == "1"
PROBE_HEADLESS = os.environ.get("ASTERINAS_FIREFOX_PROBE_HEADLESS") == "1"


def _profile_path() -> Path:
    """Select the Firefox profile directory for a bounded diagnostic run.

    The normal scene keeps its stable profile path.  Short probes may opt into
    a runtime-backed directory so profile initialization cannot turn the
    NewSession boundary into synchronous ext2 writeback.
    """

    override = os.environ.get("ASTERINAS_FIREFOX_PROFILE_DIR", "").strip()
    return Path(override) if override else PROFILE


def _new_session_capabilities() -> dict[str, object]:
    """Return the explicitly selected NewSession comparison arm.

    The default stays minimal so a probe cannot silently reintroduce the
    richer capability negotiation.  The legacy arm is retained only for the
    bounded A/B experiment against the historical successful request.
    """

    mode = os.environ.get("ASTERINAS_FIREFOX_NEWSESSION_CAPS", "minimal").strip().lower()
    if mode == "minimal":
        # The gate polls the scene itself; waiting for the load event inside
        # Navigate caps a cold TCG load at Marionette's 300 s page-load
        # timeout. Returning when the load starts leaves readiness to
        # _wait_scene's own (larger) budget.
        return {
            "pageLoadStrategy": "none",
            "strictFileInteractability": True,
        }
    if mode == "windowless":
        return {
            "strictFileInteractability": True,
            "moz:windowless": True,
        }
    if mode == "legacy":
        return {
            "acceptInsecureCerts": False,
            "pageLoadStrategy": "none",
            "strictFileInteractability": True,
        }
    if mode == "envelope":
        return {
            "capabilities": {
                "alwaysMatch": {
                    "acceptInsecureCerts": False,
                    "pageLoadStrategy": "none",
                    "strictFileInteractability": True,
                },
                "firstMatch": [],
            }
        }
    raise ValueError(f"unknown NewSession capability arm: {mode}")


def _probe_timeout_seconds() -> float:
    """Read a bounded probe deadline without allowing an unbounded guest wait."""

    raw = os.environ.get("ASTERINAS_FIREFOX_PROBE_TIMEOUT", "300")
    try:
        value = float(raw)
    except ValueError:
        value = 300.0
    return min(600.0, max(10.0, value))


PROBE_TIMEOUT = _probe_timeout_seconds()
DISPLAY_WAIT_TIMEOUT = 240.0
BROWSER_LOG_MAX_BYTES = 4 * 1024 * 1024
BROWSER_DIAGNOSTIC_MAX_LINES = 24
BROWSER_DIAGNOSTIC_LINE_BYTES = 400
PROBE_CHILD_SCAN_LIMIT = 512
PROBE_CHILD_LIMIT = 64
PROBE_DESCENDANT_DEPTH = 3
PROBE_SYSCALL_MAX_BYTES = 64 * 1024
# Keep the short probe's timeline useful without turning diagnostics into a
# second workload.  A baseline plus one terminal sample is enough to classify
# the NewSession boundary; periodic samples fill the gap when startup lasts
# longer than one interval.
PROBE_SAMPLE_LIMIT = 4
PROBE_SAMPLE_THREAD_LIMIT = 32
_GATE_START_MONOTONIC = time.monotonic()

SUPPORT_FIELDS = {
    "compositing": re.compile(r"^Compositing\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "webrender": re.compile(r"^WebRender\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "renderer": re.compile(
        r"^(?:GPU #\d+\s+)?(?:Driver Renderer|GPU Renderer|Renderer)\s*:\s*(.+?)\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
}
_SYSTEM_SUPPORT_SCRIPT = """
const windowMediator = Services.wm;
const browserWindow = windowMediator.getMostRecentWindow('navigator:browser');
const selected = browserWindow && browserWindow.gBrowser
  ? browserWindow.gBrowser.selectedBrowser
  : null;
const contentDocument = selected && selected.contentDocument;
return contentDocument && contentDocument.body ? contentDocument.body.innerText : '';
"""
_SCENE_STATE_SCRIPT = """
const state = window.__asterinasDrmScene;
const root = document.documentElement;
const domSamples = String(root && root.dataset.asterinasSamples || '')
  .split(',')
  .filter(item => item.length)
  .map(item => Number(item))
  .filter(item => Number.isFinite(item) && item >= 0);
return JSON.stringify({
  location: String(window.location.href),
  ready: String(document.readyState),
  scene_type: typeof state,
  done: Boolean(state && state.done),
  samples: state && Array.isArray(state.samples) ? state.samples.length : 0,
  dom_marker: String(root && root.dataset.asterinasScene || ''),
  dom_done: String(root && root.dataset.asterinasDone || '') === 'true',
  dom_samples: domSamples,
  dom_renderer: String(root && root.dataset.asterinasRenderer || ''),
  webgl_context: String(root && root.dataset.asterinasWebglContext || '') === 'true',
  webgl_debug: String(root && root.dataset.asterinasWebglDebug || '') === 'true',
  script_count: document.scripts.length,
  script_bytes: Array.from(document.scripts).reduce((total, item) => total + item.textContent.length, 0),
  timeout_type: typeof window.setTimeout,
  error_count: Array.isArray(window.__asterinasDrmSceneErrors) ? window.__asterinasDrmSceneErrors.length : 0,
  last_error: Array.isArray(window.__asterinasDrmSceneErrors) && window.__asterinasDrmSceneErrors.length
    ? String(window.__asterinasDrmSceneErrors[window.__asterinasDrmSceneErrors.length - 1]).slice(0, 120)
    : '',
});
"""
_WEBGL_CREATION_SCRIPT = """
const canvas = document.createElement('canvas');
let message = '';
canvas.addEventListener('webglcontextcreationerror', event => {
  message = String(event && event.statusMessage || 'unknown');
}, {once: true});
const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
let renderer = '';
let vendor = '';
if (gl) {
  const ext = gl.getExtension('WEBGL_debug_renderer_info');
  if (ext) {
    renderer = String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || '');
    vendor = String(gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) || '');
  }
  const lose = gl.getExtension('WEBGL_lose_context');
  if (lose) { lose.loseContext(); }
}
return JSON.stringify({
  context: Boolean(gl),
  message: message,
  renderer: renderer,
  vendor: vendor,
});
"""
_INSTALL_SCENE_ERROR_HOOK = """
window.__asterinasDrmSceneErrors = [];
window.addEventListener('error', event => {
  window.__asterinasDrmSceneErrors.push(String(event && event.message || 'unknown'));
});
return 1;
"""


def _value(response: object) -> object:
    if isinstance(response, dict) and "value" in response:
        return response["value"]
    return response


def _command(
    client: object,
    name: str,
    parameters: object | None,
    deadline: float,
    *,
    limit: float = 15.0,
) -> object:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GateError("Firefox gate phase deadline expired")
    client.set_timeout(min(remaining, limit))
    return _value(client.command(name, parameters))


def _script(
    client: object,
    source: str,
    *,
    sandbox: str = "default",
    deadline: float | None = None,
) -> object:
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GateError("Firefox gate phase deadline expired")
        client.set_timeout(min(remaining, 15.0))
    response = client.command(
        "WebDriver:ExecuteScript",
        {"script": source, "args": [], "newSandbox": True, "sandbox": sandbox},
    )
    return _value(response)


def _wait_text(client: object, deadline: float) -> str:
    observed = False
    while time.monotonic() < deadline:
        chunks: list[str] = []
        try:
            privileged = _script(
                client,
                _SYSTEM_SUPPORT_SCRIPT,
                sandbox="system",
                deadline=deadline,
            )
        except GateError:
            privileged = ""
        if isinstance(privileged, str) and privileged:
            chunks.append(privileged)
        for selector in ("#graphics-features-tbody", "#graphics-gpu-1-tbody"):
            found = _command(
                client,
                "WebDriver:FindElement",
                {"using": "css selector", "value": selector},
                deadline,
            )
            if not isinstance(found, dict):
                continue
            element = found.get("element-6066-11e4-a52e-4f735466cecf")
            if element is None:
                element = found.get("ELEMENT")
            if not isinstance(element, str):
                continue
            text = _command(
                client,
                "WebDriver:GetElementText",
                {"id": element},
                deadline,
            )
            if isinstance(text, str):
                chunks.append(text)
        value = "\n".join(chunks)
        if isinstance(value, str) and value and not observed:
            observed = True
            print(
                "FIREFOX_DRM_SUPPORT_SOURCE "
                f"bytes={len(value)} compositing={int('Compositing' in value)} "
                f"webrender={int('WebRender' in value)} renderer={int('Renderer' in value)}",
                flush=True,
            )
        if isinstance(value, str):
            plain = unescape(re.sub(r"<[^>]*>", "\n", value))
            if "compositing" in plain.lower():
                return plain
        time.sleep(0.1)
    raise GateError("Firefox document text did not become ready")


def _wait_scene(client: object, deadline: float) -> tuple[list[float], str, bool, bool]:
    # Let the page's timers run without a synchronous Marionette round-trip
    # first. Under TCG a rapid sequence of ExecuteScript requests can keep the
    # content thread from delivering its own setTimeout callbacks.
    time.sleep(min(SCENE_SETTLE_SECONDS, max(0.0, deadline - time.monotonic())))
    observed = False
    while time.monotonic() < deadline:
        value = _script(
            client,
            _SCENE_STATE_SCRIPT,
            deadline=deadline,
        )
        try:
            parsed = json.loads(value) if isinstance(value, str) else None
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            raise GateError("Firefox scene state response is malformed")
        if not observed:
            observed = True
            location = parsed.get("location", "")
            location_kind = "scene" if location == SCENE_URL else "other"
            scene_type = parsed.get("scene_type", "unknown")
            ready = parsed.get("ready", "unknown")
            done = int(parsed.get("done") is True)
            samples = parsed.get("samples", [])
            sample_count = len(samples) if isinstance(samples, list) else 0
            dom_marker = parsed.get("dom_marker", "")
            dom_done = int(parsed.get("dom_done") is True)
            dom_samples = parsed.get("dom_samples", [])
            dom_sample_count = len(dom_samples) if isinstance(dom_samples, list) else 0
            webgl_context = int(parsed.get("webgl_context") is True)
            webgl_debug = int(parsed.get("webgl_debug") is True)
            renderer = parsed.get("renderer", "")
            if not isinstance(renderer, str) or not renderer:
                renderer = parsed.get("dom_renderer", "")
            renderer_marker = "_".join(str(renderer).split())[:120] or "none"
            script_count = parsed.get("script_count", -1)
            script_bytes = parsed.get("script_bytes", -1)
            timeout_type = parsed.get("timeout_type", "unknown")
            error_count = parsed.get("error_count", -1)
            last_error = "_".join(str(parsed.get("last_error", "")).split())[:120]
            print(
                "FIREFOX_DRM_SCENE_STATE "
                f"location={location_kind} ready={ready} scene_type={scene_type} "
                f"done={done} samples={sample_count} dom_marker={dom_marker or 'none'} "
                f"dom_done={dom_done} dom_samples={dom_sample_count} scripts={script_count} "
                f"script_bytes={script_bytes} timeout_type={timeout_type} "
                f"renderer={renderer_marker} "
                f"webgl_context={webgl_context} webgl_debug={webgl_debug} "
                f"errors={error_count} last_error={last_error or 'none'}",
                flush=True,
            )
        samples = parsed.get("dom_samples", [])
        if not isinstance(samples, list) or not samples:
            samples = parsed.get("samples", [])
        dom_marker = parsed.get("dom_marker", "")
        dom_done = parsed.get("dom_done") is True
        if (
            dom_marker == "started"
            and (dom_done or parsed.get("done") is True)
            and isinstance(samples, list)
            and len(samples) >= 16
            and all(isinstance(item, (int, float)) and item >= 0 for item in samples)
        ):
            renderer = parsed.get("renderer", "")
            if not isinstance(renderer, str):
                renderer = ""
            dom_renderer = parsed.get("dom_renderer", "")
            if not renderer and isinstance(dom_renderer, str):
                renderer = dom_renderer
            return [float(item) for item in samples], renderer, bool(parsed.get("webgl_context")), bool(parsed.get("webgl_debug"))
        time.sleep(min(SCENE_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))
    raise GateError("Firefox fixed scene did not finish")


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return ordered[index]


_VIEWPORT_SCRIPT = "return [window.innerWidth, window.innerHeight, window.devicePixelRatio];"


def _viewport(client: object, deadline: float) -> tuple[int, int]:
    """Read the content viewport the screenshot must match.

    SetWindowRect fixes the outer window, but the content area follows the
    chrome and the toolkit's resize timing: two runs of the same gate measured
    1280x696 and 1278x690. The page's own innerWidth/innerHeight is the ground
    truth the PNG has to equal, and the bounds keep it tied to the fixed
    1280x800 window instead of accepting an arbitrary page.
    """

    value = _script(client, _VIEWPORT_SCRIPT, deadline=deadline)
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(isinstance(item, (int, float)) for item in value)
    ):
        raise GateError("Firefox viewport probe returned a malformed value")
    width, height, ratio = int(value[0]), int(value[1]), float(value[2])
    if ratio != 1.0:
        raise GateError(f"Firefox viewport devicePixelRatio is {ratio}, expected 1")
    if not (
        SCREENSHOT_MIN_WIDTH <= width <= SCREENSHOT_MAX_WIDTH
        and SCREENSHOT_MIN_HEIGHT <= height <= SCREENSHOT_MAX_HEIGHT
    ):
        raise GateError(
            f"Firefox content viewport {width}x{height} is outside the fixed window"
        )
    return width, height


def _screenshot(
    client: object, deadline: float, viewport: tuple[int, int]
) -> tuple[int, int, str]:
    response = _command(
        client,
        "WebDriver:TakeScreenshot",
        {"full": False},
        deadline,
    )
    if not isinstance(response, str):
        raise GateError("Firefox screenshot response is malformed")
    try:
        payload = base64.b64decode(response, validate=True)
    except ValueError as error:
        raise GateError("Firefox screenshot is not canonical base64") from error
    if not payload.startswith(b"\x89PNG\r\n\x1a\n") or payload[12:16] != b"IHDR":
        raise GateError("Firefox screenshot is not PNG")
    if len(payload) < 24:
        raise GateError("Firefox screenshot is truncated")
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if (width, height) != viewport:
        raise GateError(
            f"Firefox screenshot geometry is {(width, height)}, "
            f"expected {viewport[0]}x{viewport[1]} content viewport"
        )
    SCREENSHOT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    SCREENSHOT.write_bytes(payload)
    return width, height, hashlib.sha256(payload).hexdigest()


def _clear_profile_lock() -> None:
    """Remove a stale profile lock before the recovery launch.

    The first browser is stopped with SIGTERM and falls back to SIGKILL, so
    its lock symlink can survive even though the process is gone. The second
    launch would then refuse to start with "already running".
    """

    try:
        (_profile_path() / "lock").unlink()
    except OSError:
        pass


def _write_profile() -> None:
    profile = _profile_path()
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    preferences = [
                'user_pref("javascript.enabled", true);',
                'user_pref("gfx.webrender.all", true);',
                'user_pref("gfx.webrender.enabled", true);',
                # The ESR Linux WebGL path is gated before it attempts the
                # EGL provider. Force the gate open only for this bounded,
                # local virgl receipt; the runtime renderer check remains
                # strict and rejects software fallbacks.
                'user_pref("webgl.force-enabled", true);',
                'user_pref("dom.ipc.processCount", 1);',
                'user_pref("dom.ipc.processPrelaunch.enabled", false);',
                'user_pref("fission.autostart", false);',
                'user_pref("media.rdd-process.enabled", true);',
                'user_pref("browser.newtabpage.enabled", false);',
                'user_pref("browser.pagethumbnails.capturing_disabled", true);',
                'user_pref("browser.region.network.url", "");',
                'user_pref("browser.topsites.contile.enabled", false);',
                'user_pref("browser.startup.page", 0);',
                'user_pref("browser.sessionstore.resume_from_crash", false);',
                'user_pref("network.captive-portal-service.enabled", false);',
                'user_pref("network.connectivity-service.enabled", false);',
                'user_pref("network.trr.mode", 5);',
                'user_pref("services.settings.server", "");',
                'user_pref("app.update.auto", false);',
                'user_pref("app.update.enabled", false);',
                'user_pref("browser.safebrowsing.downloads.remote.enabled", false);',
                'user_pref("browser.safebrowsing.malware.enabled", false);',
                'user_pref("browser.safebrowsing.phishing.enabled", false);',
                'user_pref("extensions.systemAddon.update.enabled", false);',
                'user_pref("extensions.update.enabled", false);',
    ]
    if os.environ.get("ASTERINAS_FIREFOX_WEBRENDER_SOFTWARE") == "1":
        preferences.append('user_pref("gfx.webrender.software", true);')
        print(
            "FIREFOX_DRM_PROFILE webrender_software=1 source=probe",
            flush=True,
        )
    if os.environ.get("ASTERINAS_FIREFOX_WEBRENDER_DISABLED") == "1":
        preferences.append('user_pref("gfx.webrender.all", false);')
        preferences.append('user_pref("gfx.webrender.enabled", false);')
        print(
            "FIREFOX_DRM_PROFILE webrender_disabled=1 source=probe",
            flush=True,
        )
    # Test whether the first per-window idle task (Sync UI init) is the startup
    # hop that synchronously waits on localization/IO.
    if os.environ.get("ASTERINAS_FIREFOX_FXA_DISABLED") == "1":
        preferences.append('user_pref("identity.fxaccounts.enabled", false);')
        print("FIREFOX_DRM_PROFILE fxa_disabled=1 source=probe", flush=True)
    # Isolate a synchronous WebGL context request in the initial page load.
    # The scene acceptance path must never run with WebGL disabled.
    if os.environ.get("ASTERINAS_FIREFOX_WEBGL_DISABLED") == "1":
        preferences.append('user_pref("webgl.disabled", true);')
        print("FIREFOX_DRM_PROFILE webgl_disabled=1 source=probe", flush=True)
    # Diagnostic arm: keep the Marionette TCP connection in the parent process
    # instead of the socket process.  If a dying socket process is what closes
    # the connection, this arm keeps the session alive.
    if os.environ.get("ASTERINAS_FIREFOX_SOCKET_PROCESS_DISABLED") == "1":
        preferences.append('user_pref("network.process.enabled", false);')
        print(
            "FIREFOX_DRM_PROFILE socket_process_disabled=1 source=probe",
            flush=True,
        )
    # This is opt-in diagnostic tracing only.  It is enabled by a dedicated
    # short probe and never enters the normal Firefox scene acceptance path.
    if os.environ.get("ASTERINAS_FIREFOX_REMOTE_TRACE") == "1":
        preferences.append('user_pref("remote.log.level", "Trace");')
        # DumpAppender output is compiled in, but the JS dump() helper is
        # disabled in release builds unless this preference is set.
        preferences.append('user_pref("browser.dom.window.dump.enabled", true);')
        preferences.append('user_pref("browser.sessionstore.debug", true);')
        print(
            "FIREFOX_DRM_PROFILE remote_log_level=Trace source=user.js",
            flush=True,
        )
    (profile / "user.js").write_text(
        "\n".join(preferences) + "\n",
        encoding="utf-8",
    )


def _phase(name: str) -> None:
    print(f"FIREFOX_DRM_PHASE name={name}", flush=True)
    elapsed_ms = max(0.0, (time.monotonic() - _GATE_START_MONOTONIC) * 1000.0)
    print(
        f"FIREFOX_DRM_PHASE_METRIC name={name} elapsed_ms={elapsed_ms:.1f}",
        flush=True,
    )


def _render_access_marker() -> None:
    path = Path("/dev/dri/renderD128")
    try:
        stat_result = path.stat()
        mode = stat_result.st_mode & 0o777
        print(
            "FIREFOX_DRM_ACCESS "
            "device=renderD128 "
            f"groups={','.join(str(group) for group in sorted(os.getgroups())) or 'none'} "
            f"owner={stat_result.st_uid} group={stat_result.st_gid} mode={mode:04o} "
            f"read={int(os.access(path, os.R_OK))} write={int(os.access(path, os.W_OK))}",
            flush=True,
        )
    except OSError as error:
        print(
            "FIREFOX_DRM_ACCESS "
            f"groups={','.join(str(group) for group in sorted(os.getgroups())) or 'none'} "
            f"error={type(error).__name__}",
            flush=True,
        )


def _egl_probe_marker() -> None:
    command = (
        "/usr/bin/python3",
        "/usr/lib/asterinas/desktop-drm-egl-pixel-probe",
        "/dev/dri/renderD128",
    )
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20.0,
            env={**os.environ, "PYTHONOPTIMIZE": "1"},
        )
        payload = completed.stdout[:2048]
        text = " ".join(payload.decode("utf-8", "replace").split())
        print(
            "FIREFOX_DRM_EGL_PROBE "
            f"rc={completed.returncode} bytes={len(completed.stdout)} "
            f"text={text[:1800] or 'none'}",
            flush=True,
        )
    except subprocess.TimeoutExpired:
        print("FIREFOX_DRM_EGL_PROBE rc=timeout bytes=unknown text=timeout", flush=True)
    except OSError as error:
        print(
            "FIREFOX_DRM_EGL_PROBE "
            f"rc=error error={type(error).__name__} text=unavailable",
            flush=True,
        )


def _glxtest_marker() -> None:
    runtime_xauthority = (
        "/run/asterinas.Xauthority"
        if Path("/run/asterinas.Xauthority").is_file()
        else XAUTHORITY
    )
    try:
        completed = subprocess.run(
            ("/usr/lib/firefox-esr/glxtest",),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20.0,
            env={**os.environ, "DISPLAY": DISPLAY, "XAUTHORITY": runtime_xauthority},
        )
        payload = completed.stdout[:2048]
        text = " ".join(payload.decode("utf-8", "replace").split())
        print(
            "FIREFOX_DRM_GLXTEST "
            f"rc={completed.returncode} bytes={len(completed.stdout)} "
            f"text={text[:1800] or 'none'}",
            flush=True,
        )
    except subprocess.TimeoutExpired:
        print("FIREFOX_DRM_GLXTEST rc=timeout bytes=unknown text=timeout", flush=True)
    except OSError as error:
        print(
            "FIREFOX_DRM_GLXTEST "
            f"rc=error error={type(error).__name__} text=unavailable",
            flush=True,
        )


def _wait_for_display() -> None:
    """Wait for the Xorg provider before starting Firefox or glxtest."""

    started = time.monotonic()
    deadline = started + DISPLAY_WAIT_TIMEOUT
    runtime_xauthority = (
        "/run/asterinas.Xauthority"
        if Path("/run/asterinas.Xauthority").is_file()
        else XAUTHORITY
    )
    environment = {**os.environ, "DISPLAY": DISPLAY, "XAUTHORITY": runtime_xauthority}
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            result = subprocess.run(
                ("/usr/bin/xdpyinfo", "-display", DISPLAY),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            print(
                "FIREFOX_DRM_DISPLAY_WAIT "
                f"status=pass attempts={attempts} "
                f"elapsed_ms={(time.monotonic() - started) * 1000.0:.1f}",
                flush=True,
            )
            return
        time.sleep(0.5)
    print(
        "FIREFOX_DRM_DISPLAY_WAIT "
        f"status=fail attempts={attempts} "
        f"elapsed_ms={(time.monotonic() - started) * 1000.0:.1f}",
        flush=True,
    )
    raise GateError("Xorg display did not become ready before Firefox startup")


def _spawn(start_url: str = START_URL) -> subprocess.Popen[bytes]:
    log = FIREFOX_LOG.open("wb")
    FIREFOX_MOZ_LOG.write_bytes(b"")
    runtime_xauthority = "/run/asterinas.Xauthority" if Path("/run/asterinas.Xauthority").is_file() else XAUTHORITY
    command = [
        "/usr/bin/firefox-esr",
        "--no-remote",
        "--new-instance",
        "--offline",
        "--marionette",
        "--profile",
        str(_profile_path()),
        start_url,
    ]
    if PROBE_HEADLESS:
        command.insert(1, "--headless")
    # `sync` flushes each record to MOZ_LOG_FILE.  Without it the NSPR buffer
    # is lost when a wedged browser is killed at the probe deadline, which made
    # every NewSession timeout look like an empty log.
    moz_log = "timestamp,sync,Widget:4,Layers:4,WebRender:4"
    if os.environ.get("ASTERINAS_FIREFOX_REMOTE_TRACE") == "1":
        # Keep the protocol trace readable: the default graphics modules emit
        # far more records than Marionette and would bury the await boundary.
        moz_log = "timestamp,sync,RemoteAgent:5,Marionette:5,IPC:5"
    environment = {
        **os.environ,
        "DISPLAY": DISPLAY,
        "XAUTHORITY": runtime_xauthority,
        "MOZ_SANDBOX_LOGGING": "1",
        # Firefox's native Linux WebGL provider otherwise stops at the
        # GLX path even though the guest EGL/virgl contract is green.
        "MOZ_WEBGL_FORCE_EGL": "1",
        # about:support is privileged and is not scriptable through
        # Marionette on current ESR. Keep an independent compositor log
        # so the gate can still prove WebRender without weakening the
        # acceptance rule.
        "MOZ_LOG": moz_log,
        # Transport diagnostics contain only command names, byte counts,
        # and timing state; they never include URLs, scripts, or response
        # payloads. They make a cold-session timeout auditable.
        "ASTERINAS_MARIONETTE_DIAGNOSTICS": "1",
    }
    if os.environ.get("ASTERINAS_FIREFOX_CRASH_PROBE") == "1":
        # Diagnostic arm: a silent signal death leaves no browser log line.
        # The preloaded reporter prints the faulting address and the
        # module-relative frames to the same captured stderr.
        if Path(CRASH_PROBE_PATH).is_file():
            environment["LD_PRELOAD"] = CRASH_PROBE_PATH
            print(
                f"FIREFOX_DRM_CRASH_PROBE enabled=1 path={CRASH_PROBE_PATH}",
                flush=True,
            )
        else:
            print(
                f"FIREFOX_DRM_CRASH_PROBE enabled=0 path={CRASH_PROBE_PATH}",
                flush=True,
            )
    if os.environ.get("ASTERINAS_FIREFOX_WEBRENDER_DISABLED") == "1":
        environment["MOZ_WEBRENDER"] = "0"
    if os.environ.get("ASTERINAS_FIREFOX_WEBRENDER_SOFTWARE") == "1":
        environment["LIBGL_ALWAYS_SOFTWARE"] = "1"
        environment["MOZ_X11_EGL"] = "0"
    if os.environ.get("ASTERINAS_FIREFOX_MOZ_LOG_STDERR") == "1":
        # C++ NSPR records otherwise go to MOZ_LOG_FILE, which is lost when a
        # wedged browser is killed.  Unsetting the file routes them to stdout,
        # captured by the browser log, and `sync` flushes each record.
        environment.pop("MOZ_LOG_FILE", None)
        print("FIREFOX_DRM_MOZ_SINK sink=stdout", flush=True)
    else:
        environment["MOZ_LOG_FILE"] = str(FIREFOX_MOZ_LOG)
        print("FIREFOX_DRM_MOZ_SINK sink=file", flush=True)
    return subprocess.Popen(
        command,
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        # Firefox launches compositor/content children in the same session.
        # Terminating only the parent leaves those children alive and can
        # prevent MOZ_LOG from flushing before the renderer receipt is read.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # The child can already be a zombie whose status this kernel
                # has not reported to wait4 yet (r177 showed /proc State=Z
                # while poll() still returned None). The process is gone and
                # its Marionette port is closed; failing the whole run here
                # would discard an otherwise complete scene receipt.
                print(
                    "FIREFOX_DRM_STOP un-reaped "
                    f"pid={process.pid} poll={process.poll()} "
                    "reason=wait_timeout_after_sigkill",
                    flush=True,
                )


def _browser_watch(process: subprocess.Popen[bytes], stop: threading.Event) -> None:
    """Emit bounded process-state records while Marionette is bootstrapping."""

    while not stop.wait(30.0):
        state = "exited" if process.poll() is not None else "running"
        rss_kib = "unknown"
        try:
            status = Path(f"/proc/{process.pid}/status").read_text(encoding="utf-8")
            match = re.search(r"^VmRSS:\s+([0-9]+) kB$", status, re.MULTILINE)
            if match:
                rss_kib = match.group(1)
        except OSError:
            pass
        try:
            log_bytes = FIREFOX_LOG.stat().st_size
        except OSError:
            log_bytes = 0
        try:
            mozlog_bytes = FIREFOX_MOZ_LOG.stat().st_size
        except OSError:
            mozlog_bytes = 0
        mem_free_kib = "unknown"
        try:
            meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
            match = re.search(r"^MemFree:\s+([0-9]+) kB$", meminfo, re.MULTILINE)
            if match:
                mem_free_kib = match.group(1)
        except OSError:
            pass
        print(
            "FIREFOX_DRM_WATCH "
            f"pid={process.pid} state={state} rss_kib={rss_kib} "
            f"browser_log_bytes={log_bytes} mozlog_bytes={mozlog_bytes} "
            f"mem_free_kib={mem_free_kib}",
            flush=True,
        )


def _browser_exit_watch(process: subprocess.Popen[bytes], stop: threading.Event) -> None:
    """Report the browser exit status as soon as the child is reaped.

    The 30 s state sampler can miss a crash that lands just before the
    Marionette socket closes; a blocking ``wait`` records the status at the
    exact moment it becomes observable.
    """

    while not stop.is_set():
        try:
            returncode = process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            continue
        print(
            "FIREFOX_DRM_BROWSER_EXIT "
            f"pid={process.pid} returncode={returncode} "
            f"monotonic_ns={time.monotonic_ns()}",
            flush=True,
        )
        return
    # A death can also land between the final wait timeout and the stop
    # request; report it here rather than losing the status to the race.
    returncode = process.poll()
    if returncode is not None:
        print(
            "FIREFOX_DRM_BROWSER_EXIT "
            f"pid={process.pid} returncode={returncode} "
            f"monotonic_ns={time.monotonic_ns()}",
            flush=True,
        )


def _browser_postmortem(process: subprocess.Popen[bytes]) -> None:
    """Record whether the browser survived the failed Marionette exchange.

    A clean ``recv`` EOF can come from the peer closing the socket or from
    the peer process dying.  The exit status and the thread/socket snapshot
    taken before the gate stops the process distinguish the two.
    """

    try:
        returncode = process.poll()
    except OSError:
        returncode = None
    if returncode is None:
        # A death can land between the last watcher sample and this failure
        # path. The zombie's status is still pending, and it says which
        # signal (or exit code) ended the session instead of leaving a
        # "running" record that only the /proc state contradicts.
        try:
            observed = process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            observed = None
        if observed is not None:
            returncode = observed
    print(
        "FIREFOX_DRM_POSTMORTEM "
        f"pid={process.pid} state={'exited' if returncode is not None else 'running'} "
        f"returncode={returncode} monotonic_ns={time.monotonic_ns()}",
        flush=True,
    )
    if returncode is not None:
        return
    fields: dict[str, str] = {}
    status = _probe_read(Path(f"/proc/{process.pid}/status"))
    if not status.startswith("<unavailable:"):
        for key in ("State", "Threads", "VmRSS"):
            match = re.search(rf"^{key}:\s+(.*)$", status, re.MULTILINE)
            if match:
                fields[key.lower()] = "_".join(match.group(1).split())
    print(
        "FIREFOX_DRM_POSTMORTEM_STATUS "
        f"pid={process.pid} state={fields.get('state', 'unknown')} "
        f"threads={fields.get('threads', 'unknown')} "
        f"rss={fields.get('vmrss', 'unknown')}",
        flush=True,
    )
    sockets = _probe_socket_snapshot()
    print(
        "FIREFOX_DRM_POSTMORTEM_SOCKETS "
        + json.dumps(sockets, sort_keys=True, separators=(",", ":"))[:400],
        flush=True,
    )
    # A fresh connection proves the Marionette server still accepts clients:
    # the old connection's EOF then came from the session, not from a dead
    # browser.
    try:
        with socket.create_connection(
            ("127.0.0.1", MARIONETTE_PORT), timeout=5.0
        ) as probe:
            probe.settimeout(5.0)
            greeting = probe.recv(64)
        print(
            "FIREFOX_DRM_POSTMORTEM_RECONNECT "
            f"ok=1 greeting_bytes={len(greeting)}",
            flush=True,
        )
    except OSError as error:
        print(
            "FIREFOX_DRM_POSTMORTEM_RECONNECT "
            f"ok=0 error={type(error).__name__}",
            flush=True,
        )
    try:
        tids = sorted(
            int(entry.name)
            for entry in (Path("/proc") / str(process.pid) / "task").iterdir()
        )
    except (OSError, ValueError):
        tids = []
    for tid in tids[:24]:
        try:
            comm = (
                Path("/proc") / str(process.pid) / "task" / str(tid) / "comm"
            ).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            comm = "unknown"
        state = _probe_thread_state(process.pid, tid)
        print(
            "FIREFOX_DRM_POSTMORTEM_THREAD "
            f"tid={tid} comm={'_'.join(comm.split())[:40] or 'unknown'} "
            f"schedstat={str(state['schedstat'])[:80] or 'none'} "
            f"syscall={str(state['syscall'])[:160]}",
            flush=True,
        )
    _browser_postmortem_children(process.pid)


_BROWSER_CHILD_PREFIXES = (
    "firefox",
    "Web Content",
    "Isolated Web",
    "Socket",
    "RDD",
    "Utility",
    "forkserver",
)


def _browser_postmortem_children(parent_pid: int) -> None:
    """List surviving Firefox child processes after the session failed.

    When the Marionette socket lives in Firefox's socket process, a child
    crash closes the connection while the parent keeps running.  The parent
    snapshot alone cannot tell that apart from a healthy shutdown.
    """

    printed = 0
    for entry in sorted(Path("/proc").iterdir()):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == parent_pid:
            continue
        try:
            comm = (entry / "comm").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            continue
        if not any(comm.startswith(prefix) for prefix in _BROWSER_CHILD_PREFIXES):
            continue
        state = "unknown"
        threads = "unknown"
        status = _probe_read(entry / "status")
        if not status.startswith("<unavailable:"):
            state_match = re.search(r"^State:\s+(.*)$", status, re.MULTILINE)
            thread_match = re.search(r"^Threads:\s+(.*)$", status, re.MULTILINE)
            if state_match:
                state = "_".join(state_match.group(1).split())
            if thread_match:
                threads = thread_match.group(1).strip()
        print(
            "FIREFOX_DRM_POSTMORTEM_CHILD "
            f"pid={pid} comm={'_'.join(comm.split())[:40] or 'unknown'} "
            f"state={state} threads={threads}",
            flush=True,
        )
        printed += 1
        if printed >= 12:
            return


_RESIDUE_COMM_PREFIXES = (
    "firefox-esr",
    "Socket Process",
    "Web Content",
    "Isolated Web",
    "RDD Process",
    "Utility Process",
    "forkserver",
)


def _recovery_residue_marker() -> None:
    """List Firefox processes that outlived the first browser.

    A surviving Socket Process keeps port 2828 bound; the recovery
    connection is then accepted by a browser whose parent is gone and is
    reset, which is one of the ways r188 failed recovery.
    """

    printed = 0
    for entry in sorted(Path("/proc").iterdir()):
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            continue
        if not any(comm.startswith(prefix) for prefix in _RESIDUE_COMM_PREFIXES):
            continue
        pid = int(entry.name)
        state = "unknown"
        status = _probe_read(entry / "status")
        if not status.startswith("<unavailable:"):
            match = re.search(r"^State:\s+(.*)$", status, re.MULTILINE)
            if match:
                state = "_".join(match.group(1).split())
        print(
            "FIREFOX_DRM_RECOVERY_RESIDUE "
            f"pid={pid} comm={'_'.join(comm.split())[:40]} state={state}",
            flush=True,
        )
        printed += 1
        if printed >= 16:
            return


def _start_browser_watchers(
    process: subprocess.Popen[bytes],
) -> tuple[threading.Event, list[threading.Thread]]:
    """Start the periodic state sampler and the immediate exit reporter."""

    stop = threading.Event()
    threads = [
        threading.Thread(target=_browser_watch, args=(process, stop), daemon=True),
        threading.Thread(
            target=_browser_exit_watch, args=(process, stop), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()
    return stop, threads


def _stop_browser_watchers(
    process: subprocess.Popen[bytes],
    stop: threading.Event,
    threads: list[threading.Thread],
) -> None:
    """Join the watchers and record the browser state before stopping it."""

    stop.set()
    for thread in threads:
        thread.join(timeout=2.0)
    _browser_postmortem(process)


def _probe_read(path: Path, limit: int = 8192) -> str:
    """Read one proc/log file with a hard byte cap for the short probe."""

    try:
        return path.read_bytes()[:limit].decode("utf-8", "replace")
    except OSError as error:
        return f"<unavailable:{type(error).__name__}>"


def _parse_probe_tcp_table(payload: str, port: int) -> list[dict[str, str]]:
    """Parse bounded ``/proc/net/tcp*`` data for one local port."""

    target = f"{port:04X}"
    entries: list[dict[str, str]] = []
    for line in payload.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            continue
        local = fields[1]
        if ":" not in local or local.rsplit(":", 1)[-1].upper() != target:
            continue
        entries.append(
            {
                "local": local,
                "remote": fields[2],
                "state": fields[3],
                "uid": fields[7],
                "inode": fields[9],
            }
        )
    return entries


def _probe_socket_snapshot(port: int = MARIONETTE_PORT) -> dict[str, object]:
    """Capture the requested Marionette port from both proc TCP families."""

    sockets: dict[str, object] = {"port": port}
    for family in ("tcp", "tcp6"):
        payload = _probe_read(Path("/proc/net") / family, 64 * 1024)
        if payload.startswith("<unavailable:"):
            sockets[family] = {"error": payload}
        else:
            sockets[family] = _parse_probe_tcp_table(payload, port)
    return sockets


def _probe_socket_marker(snapshot: dict[str, object]) -> str:
    """Render socket evidence without conflating missing procfs with empty tables."""

    parts: list[str] = []
    for family in ("tcp", "tcp6"):
        entries = snapshot.get(family, [])
        if isinstance(entries, dict) and "error" in entries:
            error = str(entries["error"])
            error = error.rsplit(":", 1)[-1].strip("<>").replace(" ", "_")
            parts.append(f"{family}=error:{error}")
        elif isinstance(entries, list):
            states = [
                str(entry.get("state", "unknown"))
                for entry in entries
                if isinstance(entry, dict)
            ]
            parts.append(f"{family}={','.join(states) or 'none'}")
        else:
            parts.append(f"{family}=invalid")
    return " ".join(parts)


def _probe_process_status(pid: int) -> dict[str, object] | None:
    """Read one bounded child-process record without following its FDs."""

    root = Path("/proc") / str(pid)
    status = _probe_read(root / "status", 4096)
    if not status or status.startswith("<unavailable:"):
        return None
    fields: dict[str, str] = {}
    for line in status.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key in {"Name", "State", "Pid", "PPid", "Threads", "VmRSS"}:
                fields[key] = value.strip()
    if fields.get("Pid") != str(pid):
        return None
    return {
        "pid": pid,
        "status": fields,
        "cmdline": _probe_read(root / "cmdline", 512).replace("\x00", " ").strip(),
        "wchan": _probe_read(root / "wchan", 256).strip(),
        "schedstat": " ".join(_probe_read(root / "schedstat", 512).split())[:256],
        "asterinas_syscall": _probe_read(
            root / "asterinas_syscall", PROBE_SYSCALL_MAX_BYTES
        ),
    }


def _probe_children(pid: int) -> list[dict[str, object]]:
    """Capture a shallow Firefox process tree with bounded procfs work.

    A parent-level snapshot cannot distinguish a content process waiting for a
    renderer from the browser process waiting for that child. Scan a bounded
    process table, build only three levels of parent links, and retain the
    selected descendants. This keeps the diagnostic read-only and prevents a
    large process table from turning the short probe into another long gate.
    """

    try:
        candidates = sorted(
            int(entry.name)
            for entry in Path("/proc").iterdir()
            if entry.name.isdecimal()
        )[:PROBE_CHILD_SCAN_LIMIT]
    except OSError:
        return []
    parent_by_pid: dict[int, str] = {}
    for candidate in candidates:
        status = _probe_read(Path("/proc") / str(candidate) / "status", 4096)
        if not status or status.startswith("<unavailable:"):
            continue
        fields: dict[str, str] = {}
        for line in status.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key in {"Pid", "PPid"}:
                fields[key] = value.strip()
        if fields.get("Pid") == str(candidate) and "PPid" in fields:
            parent_by_pid[candidate] = fields["PPid"].strip()

    selected: list[tuple[int, int]] = []
    frontier = [(pid, 0)]
    seen = {pid}
    while frontier and len(selected) < PROBE_CHILD_LIMIT:
        parent, depth = frontier.pop(0)
        if depth >= PROBE_DESCENDANT_DEPTH:
            continue
        for candidate, candidate_parent in parent_by_pid.items():
            if candidate in seen or candidate_parent != str(parent):
                continue
            seen.add(candidate)
            selected.append((candidate, depth + 1))
            frontier.append((candidate, depth + 1))
            if len(selected) >= PROBE_CHILD_LIMIT:
                break

    children: list[dict[str, object]] = []
    for candidate, depth in selected:
        record = _probe_process_status(candidate)
        if record is None:
            continue
        record["tree_depth"] = depth
        if depth == 1:
            record["relation"] = "direct"
        else:
            record["relation"] = "descendant"
        children.append(record)
    return children


def _probe_proc_snapshot(pid: int, *, full_syscall: bool = False) -> dict[str, object]:
    """Collect bounded process/thread state before Firefox is terminated."""

    root = Path("/proc") / str(pid)
    status = _probe_read(root / "status")
    fields: dict[str, str] = {}
    for line in status.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key in {
                "Name",
                "State",
                "Pid",
                "PPid",
                "Threads",
                "VmRSS",
                "voluntary_ctxt_switches",
                "nonvoluntary_ctxt_switches",
            }:
                fields[key] = value.strip()
    threads: list[dict[str, str]] = []
    try:
        tids = sorted(
            int(entry.name)
            for entry in (root / "task").iterdir()
            if entry.name.isdecimal()
        )[:128]
    except OSError:
        tids = []
    for tid in tids:
        thread_root = root / "task" / str(tid)
        thread_status = _probe_read(thread_root / "status", 4096)
        thread_fields: dict[str, str] = {"tid": str(tid)}
        for line in thread_status.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                if key in {"Name", "State", "voluntary_ctxt_switches", "nonvoluntary_ctxt_switches"}:
                    thread_fields[key] = value.strip()
        thread_fields["wchan"] = _probe_read(thread_root / "wchan", 256).strip()
        thread_fields["stat"] = " ".join(_probe_read(thread_root / "stat", 2048).split())[:512]
        thread_fields["schedstat"] = " ".join(
            _probe_read(thread_root / "schedstat", 512).split()
        )[:256]
        syscall_cap = 65536 if full_syscall else 4096
        thread_fields["asterinas_syscall"] = _probe_read(
            thread_root / "asterinas_syscall", syscall_cap
        )[:syscall_cap]
        threads.append(thread_fields)
    return {
        "pid": pid,
        "status": fields,
        "socket": _probe_socket_snapshot(),
        "wchan": _probe_read(root / "wchan", 256).strip(),
        "stat": " ".join(_probe_read(root / "stat", 2048).split())[:512],
        "schedstat": " ".join(_probe_read(root / "schedstat", 512).split())[:256],
        "asterinas_syscall": _probe_read(
            root / "asterinas_syscall", PROBE_SYSCALL_MAX_BYTES
        ),
        "threads": threads,
        "children": _probe_children(pid),
    }


def _probe_log_snapshot() -> dict[str, object]:
    logs: dict[str, object] = {}
    for name, path in (("browser", FIREFOX_LOG), ("moz", FIREFOX_MOZ_LOG)):
        try:
            total = path.stat().st_size
            with path.open("rb") as stream:
                if total > 262144:
                    stream.seek(total - 262144)
                payload = stream.read()
        except OSError:
            total = 0
            payload = b""
        text = payload.decode("utf-8", "replace")
        lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
        logs[name] = {
            "path": str(path),
            "bytes": total,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "tail": " ".join(text[-8192:].split())[:4096],
            "tail_end": " ".join(text[-4096:].split())[-2048:],
            "last_lines": [line[:400] for line in lines[-12:]],
            "marionette_lines": [
                line[:400] for line in lines if "Marionette" in line
            ][-40:],
            "ipc_lines": [line[:400] for line in lines if "IPC" in line][-60:],
            "session_lines": [
                line[:400]
                for line in lines
                if "sessionstore" in line.lower()
            ][-60:],
        }
    return logs


def _probe_log_after_stop() -> None:
    """Publish the post-exit log tail, after NSPR has had a chance to flush."""

    for name, log in _probe_log_snapshot().items():
        print(
            "FIREFOX_DRM_PROBE_LOG_AFTER_STOP "
            f"name={name} bytes={log['bytes']} sha256={log['sha256']} "
            f"tail={log['tail'] or 'none'}",
            flush=True,
        )


def _probe_window_snapshot() -> dict[str, object]:
    """Capture the X window tree when Marionette is waiting on browser startup."""

    runtime_xauthority = (
        "/run/asterinas.Xauthority"
        if Path("/run/asterinas.Xauthority").is_file()
        else XAUTHORITY
    )
    try:
        completed = subprocess.run(
            ["/usr/bin/xwininfo", "-root", "-tree"],
            env={**os.environ, "DISPLAY": DISPLAY, "XAUTHORITY": runtime_xauthority},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"error": type(error).__name__}
    payload = completed.stdout[:8192]
    lines = [
        " ".join(line.split())
        for line in payload.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]
    firefox_lines = [
        line
        for line in lines
        if "firefox" in line.lower() or "asterinas" in line.lower()
    ]
    return {
        "returncode": completed.returncode,
        "bytes": len(completed.stdout),
        "firefox_lines": firefox_lines[:8],
        "lines": lines[-8:],
    }


def _probe_maps_region(pid: int, address: int) -> dict[str, object]:
    """Resolve a diagnostic futex address to one bounded /proc maps region."""

    try:
        payload = (Path("/proc") / str(pid) / "maps").read_bytes()[:262144]
    except OSError as error:
        return {"error": type(error).__name__}
    for line in payload.decode("utf-8", "replace").splitlines():
        fields = line.split(None, 5)
        if len(fields) < 5:
            continue
        try:
            start_text, end_text = fields[0].split("-", 1)
            start, end = int(start_text, 16), int(end_text, 16)
        except (ValueError, IndexError):
            continue
        if start <= address < end:
            return {
                "start": f"0x{start:x}",
                "end": f"0x{end:x}",
                "perms": fields[1],
                "offset": fields[2],
                "path": fields[5][:160] if len(fields) > 5 else "anonymous",
            }
    return {"address": f"0x{address:x}", "region": "not-found"}


def _probe_syscall_sp(value: object) -> int | None:
    """Return the syscall-entry user stack pointer from a scalar snapshot."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        document = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(document, dict):
        return None
    current = document.get("current")
    if not isinstance(current, dict):
        return None
    sp = current.get("sp")
    if isinstance(sp, int) and sp > 0:
        return sp
    return None


def _probe_startstack(stat_text: object) -> int | None:
    """Extract startstack (field 28) from one compacted /proc task stat line."""

    if not isinstance(stat_text, str):
        return None
    end = stat_text.rfind(")")
    if end < 0:
        return None
    fields = stat_text[end + 1 :].split()
    index = 28 - 3
    if len(fields) <= index:
        return None
    try:
        return int(fields[index])
    except ValueError:
        return None


def _probe_thread_futexes(
    pid: int, threads: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Report every thread currently sleeping on a futex and its word value."""

    results: list[dict[str, object]] = []
    for thread in threads:
        address = _probe_futex_address(thread.get("asterinas_syscall"))
        if address is None:
            continue
        results.append(
            {
                "tid": thread.get("tid"),
                "name": thread.get("Name", "unknown"),
                "schedstat": " ".join(str(thread.get("schedstat", "")).split()),
                "addr": f"0x{address:x}",
                "word": _probe_futex_value(pid, address),
            }
        )
        if len(results) >= 32:
            break
    return results


def _probe_pipe_fd(pid: int, fd: int) -> dict[str, object]:
    """Report a pending IPC fd target and its readable byte count."""

    path = f"/proc/{pid}/fd/{fd}"
    try:
        target = os.readlink(path)
    except OSError as error:
        return {"fd": fd, "error": type(error).__name__}
    available: object = "unknown"
    try:
        import fcntl
        import struct
        import termios

        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            buffer = fcntl.ioctl(
                descriptor, getattr(termios, "FIONREAD", 0x541B), struct.pack("i", 0)
            )
            available = struct.unpack("i", buffer[:4])[0]
        finally:
            os.close(descriptor)
    except OSError as error:
        available = {"error": type(error).__name__}
    return {"fd": fd, "target": target, "available": available}


def _probe_ipc_fds(
    pid: int, threads: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Inspect the fds of threads blocked in the IPC read path."""

    results: list[dict[str, object]] = []
    for thread in threads:
        name = str(thread.get("Name", ""))
        if "IPC I/O" not in name:
            continue
        value = thread.get("asterinas_syscall")
        if not isinstance(value, str):
            continue
        try:
            document = json.loads(value)
        except (ValueError, TypeError):
            continue
        current = document.get("current") if isinstance(document, dict) else None
        if not isinstance(current, dict) or current.get("number") not in (22, 63):
            continue
        args = current.get("args")
        if not isinstance(args, list) or not args:
            continue
        fd = args[0]
        if not isinstance(fd, int) or not 0 <= fd < 4096:
            continue
        fdinfo_path = f"/proc/{pid}/fdinfo/{fd}"
        try:
            fdinfo = Path(fdinfo_path).read_text(
                encoding="utf-8", errors="replace"
            )[:4096]
        except OSError:
            fdinfo = ""
        watched: list[dict[str, object]] = []
        for line in fdinfo.splitlines():
            match = re.search(r"tfd:\s*(\d+)", line)
            if match is None:
                continue
            target_fd = int(match.group(1))
            try:
                target = os.readlink(f"/proc/{pid}/fd/{target_fd}")
            except OSError:
                target = "unavailable"
            try:
                target_info = Path(
                    f"/proc/{pid}/fdinfo/{target_fd}"
                ).read_text(encoding="utf-8", errors="replace")[:256]
            except OSError:
                target_info = ""
            watched.append(
                {
                    "tfd": target_fd,
                    "target": target,
                    "fdinfo": target_info,
                    "io": _probe_pipe_fd(pid, target_fd),
                }
            )
            if len(watched) >= 8:
                break
        results.append(
            {
                "tid": thread.get("tid"),
                "name": name,
                "syscall": current.get("number"),
                **_probe_pipe_fd(pid, fd),
                "fdinfo": fdinfo,
                "watched": watched,
            }
        )
    return results


# The wake experiment only ever delivers SIGCONT: its default disposition
# continues the process, so an unexpected delivery cannot terminate Firefox.
# A poke forces the target out of a sleeping wait (futex or epoll) and makes it
# re-check its queued work, which distinguishes a lost kernel wake from work
# that was never queued in the first place.
PROBE_POKE_TARGETS = ("io", "compositor", "renderer", "all", "xstim")
PROBE_POKE_SIGNAL = signal.SIGCONT
PROBE_POKE_THREAD_PREFIXES = {
    "io": ("IPC I/O",),
    "compositor": ("Compositor",),
    "renderer": ("Renderer",),
}


def _probe_poke_plan() -> list[tuple[str, float]]:
    """Parse ``ASTERINAS_FIREFOX_PROBE_POKE`` into ordered signal steps.

    The format is ``target:seconds`` comma-separated, with seconds measured
    from the NewSession send.  An empty or absent value disables poking so the
    baseline probe path stays byte-for-byte unchanged.
    """

    raw = os.environ.get("ASTERINAS_FIREFOX_PROBE_POKE", "").strip()
    if not raw:
        return []
    plan: list[tuple[str, float]] = []
    for item in raw.split(","):
        target, _, offset_text = item.strip().partition(":")
        if target not in PROBE_POKE_TARGETS:
            raise ValueError(f"unknown poke target: {item.strip()}")
        try:
            offset = float(offset_text)
        except ValueError:
            raise ValueError(f"invalid poke offset: {item.strip()}") from None
        if offset < 0:
            raise ValueError(f"negative poke offset: {item.strip()}")
        plan.append((target, offset))
    return plan


def _probe_thread_tids(pid: int, target: str) -> list[int]:
    """Return the tids whose ``comm`` matches one poke target."""

    prefixes = PROBE_POKE_THREAD_PREFIXES[target]
    tids: list[int] = []
    try:
        entries = sorted((Path("/proc") / str(pid) / "task").iterdir())
    except OSError:
        return tids
    for entry in entries:
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if any(comm.startswith(prefix) for prefix in prefixes):
            try:
                tids.append(int(entry.name))
            except ValueError:
                continue
    return tids


def _probe_tgkill(tgid: int, tid: int, signum: int) -> dict[str, object]:
    """Deliver a signal to one thread through the riscv64 ``tgkill`` syscall."""

    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.syscall(
            131,
            ctypes.c_int(tgid),
            ctypes.c_int(tid),
            ctypes.c_int(signum),
        )
    except OSError as error:
        return {"tid": tid, "result": "error", "detail": type(error).__name__}
    if result != 0:
        return {"tid": tid, "result": "error", "errno": ctypes.get_errno()}
    return {"tid": tid, "result": "ok"}


def _probe_thread_state(pid: int, tid: int) -> dict[str, object]:
    """Capture one target thread's scheduler and syscall state."""

    root = Path("/proc") / str(pid) / "task" / str(tid)
    try:
        schedstat = (root / "schedstat").read_text(encoding="utf-8").strip()
    except OSError:
        schedstat = ""
    try:
        syscall = (root / "asterinas_syscall").read_text(
            encoding="utf-8", errors="replace"
        )[:PROBE_SYSCALL_MAX_BYTES]
    except OSError:
        syscall = ""
    return {
        "tid": tid,
        "schedstat": schedstat,
        "syscall": _probe_syscall_summary(syscall) if syscall else "none",
    }


def _probe_x_stimulus() -> dict[str, object]:
    """Send one harmless X request to wake the server's event loop.

    If a client request is queued on the server's accepted connection while
    the server sleeps in epoll, an unrelated request makes the loop re-scan
    every watched descriptor.  Running this mid-block separates a request the
    server has not read from one it has not answered.
    """

    runtime_xauthority = (
        "/run/asterinas.Xauthority"
        if Path("/run/asterinas.Xauthority").is_file()
        else XAUTHORITY
    )
    try:
        completed = subprocess.run(
            ["/usr/bin/xwininfo", "-root", "-tree"],
            env={**os.environ, "DISPLAY": DISPLAY, "XAUTHORITY": runtime_xauthority},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"result": type(error).__name__}
    return {
        "result": "ok" if completed.returncode == 0 else "error",
        "rc": completed.returncode,
    }


def _probe_poke(pid: int, target: str) -> dict[str, object]:
    """Wake the threads of one target and report each delivery."""

    if target == "all":
        try:
            os.kill(pid, PROBE_POKE_SIGNAL)
            delivery: object = "ok"
        except OSError as error:
            delivery = {"error": type(error).__name__, "errno": error.errno}
        return {
            "target": target,
            "signal": PROBE_POKE_SIGNAL,
            "scope": "process",
            "delivery": delivery,
            "threads": [],
        }
    tids = _probe_thread_tids(pid, target)
    results = [_probe_tgkill(pid, tid, PROBE_POKE_SIGNAL) for tid in tids]
    return {
        "target": target,
        "signal": PROBE_POKE_SIGNAL,
        "scope": "thread",
        "delivery": "ok" if results else "no-match",
        "threads": results,
    }


def _probe_futex_value(pid: int, address: int) -> dict[str, object]:
    """Read the futex word the leader is sleeping on to detect a lost wakeup."""

    region = _probe_maps_region(pid, address)
    if not isinstance(region, dict) or "start" not in region:
        return {"address": f"0x{address:x}", "region": "not-found"}
    try:
        descriptor = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        try:
            raw = os.pread(descriptor, 4, address)
        finally:
            os.close(descriptor)
    except OSError as error:
        return {"error": type(error).__name__}
    if len(raw) != 4:
        return {"bytes": len(raw)}
    return {
        "u32_le": int.from_bytes(raw, "little"),
        "u32_be": int.from_bytes(raw, "big"),
    }


def _probe_stack_strings(pid: int, address: int) -> dict[str, object]:
    """Extract bounded printable strings around a blocked futex stack slot."""

    before = 4096
    size = 20480
    region = _probe_maps_region(pid, address)
    if not isinstance(region, dict) or "start" not in region:
        return {"address": f"0x{address:x}", "bytes": 0, "strings": []}
    try:
        region_start = int(str(region["start"]), 16)
        region_end = int(str(region["end"]), 16)
    except (KeyError, ValueError):
        return {"address": f"0x{address:x}", "bytes": 0, "strings": []}
    start = max(region_start, address - before)
    end = min(region_end, address + size)
    try:
        descriptor = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        try:
            raw = os.pread(descriptor, end - start, start)
        finally:
            os.close(descriptor)
    except OSError as error:
        return {"error": type(error).__name__}
    strings: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(rb"[\x20-\x7e]{6,}", raw):
        text = match.group().decode("ascii", "replace")[:200]
        if text in seen:
            continue
        seen.add(text)
        strings.append(text)
        if len(strings) >= 40:
            break
    return {"address": f"0x{address:x}", "bytes": len(raw), "strings": strings}


def _probe_stack_words(
    pid: int,
    address: int,
    *,
    before: int = 2048,
    after: int = 8192,
    max_words: int = 40,
) -> dict[str, object]:
    """Report stack words that point into executable file mappings."""

    try:
        maps_text = (Path("/proc") / str(pid) / "maps").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError as error:
        return {"error": type(error).__name__}
    regions: list[tuple[int, int, int, str]] = []
    containing: tuple[int, int, int, str] | None = None
    for line in maps_text.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 5:
            continue
        try:
            start_text, end_text = fields[0].split("-", 1)
            start, end = int(start_text, 16), int(end_text, 16)
            offset = int(fields[2], 16) if len(fields) > 2 else 0
        except (ValueError, IndexError):
            continue
        path = fields[5][:160] if len(fields) > 5 else "anonymous"
        if start <= address < end and "r" in fields[1]:
            containing = (start, end, offset, path)
        if "x" in fields[1]:
            regions.append((start, end, offset, path))
    if containing is None:
        return {"address": f"0x{address:x}", "bytes": 0, "words": [], "unmapped": True}
    # Clamp the window to the readable mapping.  An unclamped read at a stack
    # edge panicked the guest kernel in the r154 probe.
    start_address = max(containing[0], address - before)
    end_address = min(containing[1], address + after)
    try:
        descriptor = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        try:
            raw = os.pread(descriptor, end_address - start_address, start_address)
        finally:
            os.close(descriptor)
    except OSError as error:
        return {"error": type(error).__name__}
    words: list[dict[str, str]] = []
    seen: set[int] = set()
    for index in range(0, len(raw) - 7, 8):
        value = int.from_bytes(raw[index : index + 8], "little")
        if value in seen:
            continue
        for region_start, region_end, region_offset, path in regions:
            if region_start <= value < region_end:
                seen.add(value)
                words.append(
                    {
                        "value": f"0x{value:x}",
                        "map_start": f"0x{region_start:x}",
                        "map_offset": f"0x{region_offset:x}",
                        "path": path,
                    }
                )
                break
        if len(words) >= max_words:
            break
    return {"window": f"0x{start_address:x}", "bytes": len(raw), "words": words}


def _probe_external_processes(names: tuple[str, ...] = ("Xorg", "Xorg.bin")) -> None:
    """Publish the live syscall state of the display server.

    A syscall that is still blocked never appears in the completed-syscall
    profile, so the only way to tell an idle X server from one wedged in a
    driver wait is to read its current record while the browser is stuck.
    """

    for entry in sorted(Path("/proc").iterdir()):
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if comm not in names:
            continue
        pid = int(entry.name)
        try:
            raw = (entry / "asterinas_syscall").read_text(
                encoding="utf-8", errors="replace"
            )[:PROBE_SYSCALL_MAX_BYTES]
        except OSError:
            raw = ""
        print(
            "FIREFOX_DRM_PROBE_EXTERNAL "
            f"pid={pid} name={comm} "
            f"syscall={_probe_syscall_summary(raw) if raw else 'none'}",
            flush=True,
        )
        try:
            tasks = sorted((entry / "task").iterdir())
        except OSError:
            continue
        for task in tasks[:16]:
            try:
                tid = int(task.name)
            except ValueError:
                continue
            try:
                task_comm = (task / "comm").read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
            except OSError:
                task_comm = "unknown"
            try:
                task_raw = (task / "asterinas_syscall").read_text(
                    encoding="utf-8", errors="replace"
                )[:PROBE_SYSCALL_MAX_BYTES]
            except OSError:
                task_raw = ""
            print(
                "FIREFOX_DRM_PROBE_EXTERNAL_THREAD "
                f"pid={pid} tid={tid} name={task_comm} "
                f"syscall={_probe_syscall_summary(task_raw) if task_raw else 'none'}",
                flush=True,
            )


def _probe_pollfds(pid: int, thread: dict[str, object], limit: int = 16) -> None:
    """Publish the files a thread is waiting on in poll/ppoll.

    A one-entry ppoll is how Mesa waits for a GPU fence and how an event loop
    waits for its wakeup descriptor; resolving the fd number separates those
    two cases without reading any user payload.
    """

    value = thread.get("asterinas_syscall")
    if not isinstance(value, str):
        return
    try:
        document = json.loads(value)
    except (ValueError, TypeError):
        return
    current = document.get("current") if isinstance(document, dict) else None
    if not isinstance(current, dict) or current.get("number") not in (7, 73):
        return
    args = current.get("args")
    if not isinstance(args, list) or len(args) < 2:
        return
    address, count = args[0], args[1]
    if not isinstance(address, int) or not isinstance(count, int):
        return
    count = min(count, limit)
    if count <= 0:
        return
    region = _probe_maps_region(pid, address)
    if not isinstance(region, dict) or "start" not in region:
        return
    try:
        region_start = int(str(region["start"]), 16)
        region_end = int(str(region["end"]), 16)
    except (KeyError, ValueError):
        return
    start = max(region_start, address)
    end = min(region_end, address + count * 8)
    try:
        descriptor = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        try:
            raw = os.pread(descriptor, end - start, start)
        finally:
            os.close(descriptor)
    except OSError:
        return
    for index in range(0, len(raw) - 7, 8):
        fd = int.from_bytes(raw[index : index + 4], "little", signed=True)
        events = int.from_bytes(raw[index + 4 : index + 6], "little")
        revents = int.from_bytes(raw[index + 6 : index + 8], "little")
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError as error:
            target = f"error:{type(error).__name__}"
        print(
            "FIREFOX_DRM_PROBE_POLLFD "
            f"tid={thread.get('tid', 'unknown')} "
            f"name={thread.get('Name', 'unknown')} "
            f"index={index // 8} fd={fd} "
            f"events=0x{events:x} revents=0x{revents:x} target={target}",
            flush=True,
        )


def _probe_syscall_fp(value: object) -> int | None:
    """Return the syscall-entry frame pointer from a scalar snapshot."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        document = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(document, dict):
        return None
    current = document.get("current")
    if not isinstance(current, dict):
        return None
    fp = current.get("fp")
    if isinstance(fp, int) and fp > 0:
        return fp
    return None


def _probe_read_u64(pid: int, address: int) -> int | None:
    try:
        descriptor = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        try:
            raw = os.pread(descriptor, 8, address)
        finally:
            os.close(descriptor)
    except OSError:
        return None
    if len(raw) != 8:
        return None
    return int.from_bytes(raw, "little")


def _probe_frame_chain(pid: int, fp: int, limit: int = 24) -> list[dict[str, object]]:
    """Walk the user frame record chain from a blocked thread's FP.

    The riscv64 psABI saves the return address at ``fp - 8`` and the previous
    frame pointer at ``fp - 16``.  Invalid or absent frame records stop the
    walk; the caller keeps the raw stack-word scan as the fallback.
    """

    frames: list[dict[str, object]] = []
    current = fp
    for index in range(limit):
        region = _probe_maps_region(pid, current)
        if not isinstance(region, dict) or "start" not in region:
            break
        if "rw" not in str(region.get("perms", "")):
            break
        try:
            region_start = int(str(region["start"]), 16)
            region_end = int(str(region["end"]), 16)
        except (KeyError, ValueError):
            break
        # The frame record is 16 bytes below the frame pointer.  Keep the whole
        # record inside the readable mapping so the kernel never sees an
        # unmapped /proc/pid/mem read.
        if current - 16 < region_start or current >= region_end:
            break
        ra = _probe_read_u64(pid, current - 8)
        previous = _probe_read_u64(pid, current - 16)
        if ra is None:
            break
        ra_region = _probe_maps_region(pid, ra)
        frames.append(
            {
                "index": index,
                "fp": f"0x{current:x}",
                "ra": f"0x{ra:x}",
                "ra_map": ra_region,
            }
        )
        if not previous or previous <= current:
            break
        current = previous
    return frames


def _probe_thread_stack_marker(
    pid: int, thread: dict[str, object], limit: int = 96
) -> None:
    """Publish one bounded stack scan for a named Firefox thread.

    The kernel reports the syscall-entry user SP for each thread, so a blocked
    partner (compositor, IPC I/O, renderer) can be symbolized from its own
    stack.  One word per line keeps each record below the serial line limit
    that truncated the combined main-stack record.
    """

    name = str(thread.get("Name", "unknown"))
    value = thread.get("asterinas_syscall")
    sp = _probe_syscall_sp(value)
    if sp is None:
        print(
            "FIREFOX_DRM_PROBE_THREAD_STACK "
            f"tid={thread.get('tid', 'unknown')} name={name} sp=absent",
            flush=True,
        )
        return
    result = _probe_stack_words(pid, sp, after=32768, max_words=limit)
    words = result.get("words", []) if isinstance(result, dict) else []
    print(
        "FIREFOX_DRM_PROBE_THREAD_STACK "
        f"tid={thread.get('tid', 'unknown')} name={name} sp=0x{sp:x} "
        f"bytes={result.get('bytes', 'unknown') if isinstance(result, dict) else 'unknown'} "
        f"words={len(words)}",
        flush=True,
    )
    for word in words[:limit]:
        if not isinstance(word, dict):
            continue
        print(
            "FIREFOX_DRM_PROBE_THREAD_STACK_WORD "
            f"tid={thread.get('tid', 'unknown')} name={name} "
            f"value={word.get('value', '?')} "
            f"map_start={word.get('map_start', '?')} "
            f"map_offset={word.get('map_offset', '?')} "
            f"path={word.get('path', '?')}",
            flush=True,
        )
    fp = _probe_syscall_fp(value)
    if fp is None:
        print(
            "FIREFOX_DRM_PROBE_FRAME_CHAIN "
            f"tid={thread.get('tid', 'unknown')} name={name} fp=absent",
            flush=True,
        )
        return
    frames = _probe_frame_chain(pid, fp)
    print(
        "FIREFOX_DRM_PROBE_FRAME_CHAIN "
        f"tid={thread.get('tid', 'unknown')} name={name} "
        f"fp=0x{fp:x} frames={len(frames)}",
        flush=True,
    )
    for frame in frames:
        region = frame.get("ra_map")
        mapping = ""
        if isinstance(region, dict):
            mapping = (
                f"map_start={region.get('start', '?')} "
                f"map_offset={region.get('offset', '?')} "
                f"path={region.get('path', '?')}"
            )
        print(
            "FIREFOX_DRM_PROBE_FRAME "
            f"tid={thread.get('tid', 'unknown')} name={name} "
            f"index={frame['index']} ra={frame['ra']} fp={frame['fp']} "
            f"{mapping}",
            flush=True,
        )


def _probe_main_stack_words(
    pid: int, stat_text: object, user_sp: int | None = None
) -> dict[str, object]:
    """Scan the live user stack for executable pointers.

    When the kernel exposes the syscall-entry user SP, the window starts at
    that SP and is clamped to its readable mapping.  Otherwise fall back to
    the initial stack top captured by ``startstack``.
    """

    try:
        maps_text = (Path("/proc") / str(pid) / "maps").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError as error:
        return {"error": type(error).__name__}
    all_regions: list[tuple[int, int, str]] = []
    exec_regions: list[tuple[int, int, int, str]] = []
    for line in maps_text.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 5:
            continue
        try:
            start_text, end_text = fields[0].split("-", 1)
            start, end = int(start_text, 16), int(end_text, 16)
            offset = int(fields[2], 16)
        except (ValueError, IndexError):
            continue
        perms = fields[1]
        path = fields[5][:160] if len(fields) > 5 else "anonymous"
        all_regions.append((start, end, perms))
        if "x" in perms:
            exec_regions.append((start, end, offset, path))
    mode = "sp"
    window = 65536
    if user_sp is not None:
        region = next(
            (
                item
                for item in all_regions
                if item[0] <= user_sp < item[1]
                and "r" in item[2]
                and "w" in item[2]
            ),
            None,
        )
        if region is None:
            user_sp = None
        else:
            window = min(window, region[1] - user_sp)
            start_address = user_sp
            startstack = user_sp
    if user_sp is None:
        startstack = _probe_startstack(stat_text)
        if startstack is None:
            return {"error": "no-startstack"}
        mode = "startstack"
        window = 262144
        start_address = max(0, startstack - window)
    try:
        descriptor = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        try:
            raw = os.pread(descriptor, window, start_address)
        finally:
            os.close(descriptor)
    except OSError as error:
        return {"error": type(error).__name__}
    words: list[dict[str, object]] = []
    seen: set[int] = set()
    # The window starts at the live SP, so walking upward visits the newest
    # frame's return addresses before the older callers.
    for index in range(0, len(raw) - 7, 8):
        value = int.from_bytes(raw[index : index + 8], "little")
        if value in seen:
            continue
        for region_start, region_end, region_offset, path in exec_regions:
            if region_start <= value < region_end:
                seen.add(value)
                words.append(
                    {
                        "stack_offset": index,
                        "value": f"0x{value:x}",
                        "map_start": f"0x{region_start:x}",
                        "map_offset": f"0x{region_offset:x}",
                        "path": path,
                    }
                )
                break
        if len(words) >= 48:
            break
    return {
        "mode": mode,
        "startstack": f"0x{startstack:x}",
        "window": f"0x{start_address:x}",
        "bytes": len(raw),
        "words": words,
    }


def _probe_process_environ(pid: int) -> dict[str, str]:
    """Report the Firefox logging switches that actually reached the process."""

    try:
        payload = (Path("/proc") / str(pid) / "environ").read_bytes()[:65536]
    except OSError as error:
        return {"error": type(error).__name__}
    selected: dict[str, str] = {}
    wanted = {
        "MOZ_LOG",
        "MOZ_LOG_FILE",
        "MOZ_WEBGL_FORCE_EGL",
        "ASTERINAS_FIREFOX_PROBE_TIMEOUT",
        "ASTERINAS_FIREFOX_REMOTE_TRACE",
        "ASTERINAS_DESKTOP_BROWSER_ENABLED",
    }
    for entry in payload.split(b"\0"):
        if b"=" not in entry:
            continue
        raw_key, raw_value = entry.split(b"=", 1)
        try:
            key = raw_key.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if key in wanted:
            selected[key] = raw_value.decode("utf-8", "replace")[:256]
    return selected


def _probe_compact_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    """Bound a timeline sample independently of the final process snapshot."""

    compact = dict(snapshot)
    compact["threads"] = [
        dict(thread, asterinas_syscall=str(thread.get("asterinas_syscall", ""))[:4096])
        for thread in snapshot.get("threads", [])[:PROBE_SAMPLE_THREAD_LIMIT]
    ]
    compact["children"] = [
        dict(child, asterinas_syscall=str(child.get("asterinas_syscall", ""))[:4096])
        for child in snapshot.get("children", [])[:8]
    ]
    compact["asterinas_syscall"] = str(snapshot.get("asterinas_syscall", ""))[:4096]
    return compact


def _probe_syscall_summary(value: object) -> str:
    """Return non-sensitive syscall boundary fields for serial evidence."""

    if not isinstance(value, str) or not value.strip():
        return "absent"
    try:
        document = json.loads(value)
    except (ValueError, TypeError):
        return "invalid"
    if not isinstance(document, dict):
        return "invalid"
    current = document.get("current")
    if not isinstance(current, dict):
        return "none"
    fields = []
    for key in ("sequence", "number", "entered_jiffies", "finished_jiffies", "outcome", "result"):
        if key in current:
            fields.append(f"{key}={str(current[key]).replace(' ', '_')[:64]}")
    # Futex and poll boundaries need their scalar arguments to correlate a
    # parent wait with the corresponding IPC wake without exposing user data.
    number = current.get("number")
    args = current.get("args")
    if number in (
        22, 29, 56, 63, 64, 73, 79, 80, 98, 135, 172, 178,
        202, 203, 206, 207, 211, 212, 215, 220, 221, 222, 226, 278,
    ) and isinstance(args, list):
        for index, value in enumerate(args[:4]):
            fields.append(f"arg{index}={str(value).replace(' ', '_')[:64]}")
    return ",".join(fields) or "none"


def _probe_futex_address(value: object) -> int | None:
    """Return the current futex uaddr from one process-level snapshot."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        document = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(document, dict):
        return None
    current = document.get("current")
    if not isinstance(current, dict) or current.get("number") != 98:
        return None
    args = current.get("args")
    if isinstance(args, list) and args and isinstance(args[0], int):
        return args[0]
    return None


def _probe_sample(
    process: subprocess.Popen[bytes],
    samples: list[dict[str, object]],
    sample_lock: threading.Lock,
) -> None:
    """Emit one lightweight state sample while NewSession is blocked."""

    snapshot = _probe_proc_snapshot(process.pid)
    logs = _probe_log_snapshot()
    with sample_lock:
        sample_index = len(samples)
        if sample_index < PROBE_SAMPLE_LIMIT:
            samples.append(
                {
                    "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "monotonic_ns": time.monotonic_ns(),
                    "process": _probe_compact_snapshot(snapshot),
                    "logs": logs,
                }
            )
    status = snapshot["status"]
    socket_snapshot = snapshot.get("socket", {})
    print(
        "FIREFOX_DRM_PROBE_SAMPLE "
        f"sample_index={sample_index} "
        f"pid={process.pid} state={status.get('State', 'unknown')} "
        f"threads={status.get('Threads', 'unknown')} rss={status.get('VmRSS', 'unknown')} "
        f"schedstat={'_'.join(str(snapshot.get('schedstat', '')).split()) or 'unknown'} "
        f"syscall={_probe_syscall_summary(snapshot.get('asterinas_syscall'))}",
        flush=True,
    )
    print(
        "FIREFOX_DRM_PROBE_SOCKET "
        f"port={MARIONETTE_PORT} {_probe_socket_marker(socket_snapshot)}",
        flush=True,
    )
    futex_address = _probe_futex_address(snapshot.get("asterinas_syscall"))
    if futex_address is not None:
        print(
            "FIREFOX_DRM_PROBE_SAMPLE_FUTEX "
            f"addr=0x{futex_address:x} "
            + json.dumps(
                _probe_futex_value(process.pid, futex_address),
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
    # Publish a compact child/log snapshot on every interval so a host-side
    # timeout still leaves useful evidence even when the final atomic receipt
    # cannot run its cleanup block.
    for child in snapshot.get("children", [])[:8]:
        child_status = child.get("status", {})
        print(
            "FIREFOX_DRM_PROBE_SAMPLE_CHILD "
            f"pid={child.get('pid', 'unknown')} "
            f"name={child_status.get('Name', 'unknown')} "
            f"state={child_status.get('State', 'unknown')} "
            f"threads={child_status.get('Threads', 'unknown')} "
            f"relation={child.get('relation', 'unknown')} "
            f"depth={child.get('tree_depth', 'unknown')} "
            f"wchan={child.get('wchan', 'unknown') or 'unknown'} "
            f"syscall={_probe_syscall_summary(child.get('asterinas_syscall'))}",
            flush=True,
        )
    for thread in snapshot.get("threads", [])[:32]:
        syscall = thread.get("asterinas_syscall")
        if not syscall or _probe_syscall_summary(syscall) in {"absent", "none", "invalid"}:
            continue
        print(
            "FIREFOX_DRM_PROBE_SAMPLE_THREAD "
            f"pid={process.pid} tid={thread.get('tid', 'unknown')} "
            f"state={thread.get('State', 'unknown')} "
            f"syscall={_probe_syscall_summary(syscall)}",
            flush=True,
        )
    print(
        "FIREFOX_DRM_PROBE_SAMPLE_LOG "
        + " ".join(
            f"{name}_bytes={entry['bytes']} {name}_sha256={entry['sha256']}"
            for name, entry in logs.items()
        ),
        flush=True,
    )


def _probe_sampler(
    process: subprocess.Popen[bytes],
    stop: threading.Event,
    samples: list[dict[str, object]],
    sample_lock: threading.Lock,
) -> None:
    while not stop.wait(10.0):
        try:
            _probe_sample(process, samples, sample_lock)
        except BaseException as error:
            print(
                "FIREFOX_DRM_PROBE_SAMPLE_ERROR "
                f"type={type(error).__name__}",
                flush=True,
            )


def _publish_probe_receipt(
    *,
    started_utc: str,
    status: str,
    phase: str,
    reason: str,
    process: subprocess.Popen[bytes],
    samples: list[dict[str, object]],
) -> None:
    """Atomically publish a short-probe receipt before process cleanup."""

    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    display = _probe_window_snapshot()
    environ = _probe_process_environ(process.pid)
    receipt = {
        "schema": 1,
        "gate": "firefox-marionette-probe",
        "status": status,
        "started_utc": started_utc,
        "finished_utc": finished,
        "phase": phase,
        "reason": reason,
        "pid": process.pid,
        "process": _probe_proc_snapshot(process.pid, full_syscall=True),
        "logs": _probe_log_snapshot(),
        "display": display,
        "environ": environ,
        "sample_count": len(samples),
        "samples": samples[:PROBE_SAMPLE_LIMIT],
    }
    futex_address = _probe_futex_address(receipt["process"].get("asterinas_syscall"))
    futex_map = (
        _probe_maps_region(process.pid, futex_address)
        if futex_address is not None
        else None
    )
    receipt["futex_map"] = futex_map
    futex_value = (
        _probe_futex_value(process.pid, futex_address)
        if futex_address is not None
        else None
    )
    receipt["futex_value"] = futex_value
    futex_strings = (
        _probe_stack_strings(process.pid, futex_address)
        if futex_address is not None
        else None
    )
    receipt["futex_strings"] = futex_strings
    stack_words = (
        _probe_stack_words(process.pid, futex_address)
        if futex_address is not None
        else None
    )
    receipt["stack_words"] = stack_words
    leader_stat = next(
        (
            thread.get("stat")
            for thread in receipt["process"]["threads"]
            if str(thread.get("tid")) == str(process.pid)
        ),
        None,
    )
    user_sp = _probe_syscall_sp(receipt["process"].get("asterinas_syscall"))
    main_stack_words = (
        _probe_main_stack_words(process.pid, leader_stat, user_sp)
        if futex_address is not None
        else None
    )
    receipt["main_stack_words"] = main_stack_words
    futex_owner = None
    if futex_map is not None and "start" in futex_map and "end" in futex_map:
        region_start = int(str(futex_map["start"]), 16)
        region_end = int(str(futex_map["end"]), 16)
        for thread in receipt["process"]["threads"]:
            startstack = _probe_startstack(thread.get("stat"))
            if startstack is not None and region_start < startstack <= region_end:
                futex_owner = {
                    "tid": thread.get("tid"),
                    "name": thread.get("Name", "unknown"),
                    "startstack": f"0x{startstack:x}",
                }
                break
    receipt["futex_owner"] = futex_owner
    payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    PROBE_RECEIPT.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = PROBE_RECEIPT.with_name(f".{PROBE_RECEIPT.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, PROBE_RECEIPT)
    directory_fd = os.open(PROBE_RECEIPT.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    process_state = receipt["process"]["status"]
    print(
        "FIREFOX_DRM_PROBE_RECEIPT "
        f"path={PROBE_RECEIPT} sha256={digest} bytes={len(payload)} "
        f"phase={phase} status={status} sample_count={len(samples[:PROBE_SAMPLE_LIMIT])}",
        flush=True,
    )
    print(
        "FIREFOX_DRM_PROBE_PROC "
        f"pid={process.pid} state={process_state.get('State', 'unknown')} "
        f"threads={process_state.get('Threads', 'unknown')} "
        f"rss={process_state.get('VmRSS', 'unknown')} "
        f"wchan={receipt['process']['wchan'] or 'unknown'}",
        flush=True,
    )
    print(
        "FIREFOX_DRM_PROBE_WINDOWS "
        + json.dumps(display, sort_keys=True, separators=(",", ":")),
        flush=True,
    )
    print(
        "FIREFOX_DRM_PROBE_ENVIRON "
        + json.dumps(environ, sort_keys=True, separators=(",", ":")),
        flush=True,
    )
    if futex_map is not None:
        print(
            "FIREFOX_DRM_PROBE_FUTEX_MAP "
            f"addr=0x{futex_address:x} "
            + json.dumps(futex_map, sort_keys=True, separators=(",", ":")),
            flush=True,
        )
    if futex_value is not None:
        print(
            "FIREFOX_DRM_PROBE_FUTEX_VALUE "
            f"addr=0x{futex_address:x} "
            + json.dumps(futex_value, sort_keys=True, separators=(",", ":")),
            flush=True,
        )
    if futex_owner is not None:
        print(
            "FIREFOX_DRM_PROBE_FUTEX_STACK "
            + json.dumps(futex_owner, sort_keys=True, separators=(",", ":")),
            flush=True,
        )
    if futex_strings is not None:
        print(
            "FIREFOX_DRM_PROBE_STACK_STRINGS "
            + json.dumps(futex_strings, sort_keys=True, separators=(",", ":"))[:8000],
            flush=True,
        )
    if stack_words is not None:
        print(
            "FIREFOX_DRM_PROBE_STACK_WORDS "
            + json.dumps(stack_words, sort_keys=True, separators=(",", ":"))[:8000],
            flush=True,
        )
    if main_stack_words is not None:
        print(
            "FIREFOX_DRM_PROBE_MAIN_STACK_WORDS "
            + json.dumps(main_stack_words, sort_keys=True, separators=(",", ":"))[:12000],
            flush=True,
        )
    print(
        "FIREFOX_DRM_PROBE_SYSCALL "
        f"pid={process.pid} {_probe_syscall_summary(process_snapshot_value := receipt['process'].get('asterinas_syscall'))}",
        flush=True,
    )
    for thread in receipt["process"]["threads"][:64]:
        print(
            "FIREFOX_DRM_PROBE_THREAD "
            f"tid={thread['tid']} name={thread.get('Name', 'unknown')} "
            f"state={thread.get('State', 'unknown')} "
            f"wchan={thread.get('wchan', 'unknown') or 'unknown'} "
            f"syscall={_probe_syscall_summary(thread.get('asterinas_syscall'))}",
            flush=True,
        )
    for thread in receipt["process"]["threads"]:
        name = str(thread.get("Name", ""))
        if any(
            marker in name
            for marker in ("Compositor", "Renderer", "Software", "WRWorker", "IPC I/O")
        ):
            print(
                "FIREFOX_DRM_PROBE_COMPOSITOR "
                f"tid={thread['tid']} name={name} "
                f"state={thread.get('State', 'unknown')} "
                f"schedstat={'_'.join(str(thread.get('schedstat', '')).split()) or 'unknown'} "
                f"raw={' '.join(str(thread.get('asterinas_syscall', '')).split())[:160] or 'none'} "
                f"syscall={_probe_syscall_summary(thread.get('asterinas_syscall'))}",
                flush=True,
            )
    for thread in receipt["process"]["threads"]:
        name = str(thread.get("Name", ""))
        if any(
            marker in name
            for marker in (
                "Compositor",
                "IPC I/O",
                "Renderer",
                "Softwar",
                "Timer",
                "WRWorkerLP#0",
            )
        ):
            _probe_thread_stack_marker(process.pid, thread)
    for thread in receipt["process"]["threads"]:
        _probe_pollfds(process.pid, thread)
    _probe_external_processes()
    for item in _probe_thread_futexes(process.pid, receipt["process"]["threads"]):
        print(
            "FIREFOX_DRM_PROBE_THREAD_FUTEX "
            f"tid={item['tid']} name={item['name']} "
            f"schedstat={item['schedstat'] or 'unknown'} "
            f"addr={item['addr']} "
            f"word={json.dumps(item['word'], sort_keys=True, separators=(',', ':'))}",
            flush=True,
        )
    for item in _probe_ipc_fds(process.pid, receipt["process"]["threads"]):
        print(
            "FIREFOX_DRM_PROBE_IPC_FD "
            + json.dumps(item, sort_keys=True, separators=(",", ":"))[:8000],
            flush=True,
        )
    for child in receipt["process"].get("children", [])[:PROBE_CHILD_LIMIT]:
        child_status = child.get("status", {})
        print(
            "FIREFOX_DRM_PROBE_CHILD "
            f"pid={child.get('pid', 'unknown')} "
            f"name={child_status.get('Name', 'unknown')} "
            f"state={child_status.get('State', 'unknown')} "
            f"threads={child_status.get('Threads', 'unknown')} "
            f"rss={child_status.get('VmRSS', 'unknown')} "
            f"schedstat={'_'.join(str(child.get('schedstat', '')).split()) or 'unknown'} "
            f"syscall={_probe_syscall_summary(child.get('asterinas_syscall'))}",
            flush=True,
        )
    for name, log in receipt["logs"].items():
        print(
            "FIREFOX_DRM_PROBE_LOG "
            f"name={name} bytes={log['bytes']} sha256={log['sha256']} "
            f"tail={log['tail'] or 'none'}",
            flush=True,
        )
        print(
            "FIREFOX_DRM_PROBE_LOG_LINES "
            f"name={name} "
            f"tail_end={(log['tail_end'] or 'none')[:2048]} "
            "last_lines="
            + json.dumps(log["last_lines"], separators=(",", ":"))[:6000]
            + " marionette_lines="
            + json.dumps(log["marionette_lines"], separators=(",", ":"))[:6000]
            + " ipc_lines="
            + json.dumps(log["ipc_lines"], separators=(",", ":"))[:6000]
            + " session_lines="
            + json.dumps(log["session_lines"], separators=(",", ":"))[:6000],
            flush=True,
        )


def _probe_main_block_age(pid: int) -> int | None:
    """Return how long the main thread has been in its current futex wait.

    ``asterinas_syscall`` reports both the syscall entry time and the snapshot
    time in milliseconds, so the age is available without sampling twice.  A
    multi-second wait on the leader is the compositor Resume block; the probe
    anchors its poke schedule on that observation instead of guessing a
    startup-wide offset.
    """

    try:
        raw = (Path("/proc") / str(pid) / "asterinas_syscall").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except (ValueError, TypeError):
        return None
    current = document.get("current") if isinstance(document, dict) else None
    if not isinstance(current, dict) or current.get("number") != 98:
        return None
    entered = current.get("entered_jiffies")
    snapshot = document.get("snapshot_jiffies")
    if not isinstance(entered, int) or not isinstance(snapshot, int):
        return None
    return max(0, snapshot - entered)


def _probe_new_session_with_pokes(
    client: object,
    capabilities: dict[str, object],
    deadline: float,
    plan: list[tuple[str, float]],
    pid: int,
) -> object:
    """Send NewSession, then wake sleeping partner threads on a schedule.

    NewSession is issued on a worker thread so the probe thread can deliver a
    bounded SIGCONT while the main thread is blocked in the compositor Resume
    sync IPC.  The schedule is anchored on the first observation of a
    multi-second leader futex wait, because the window map lands anywhere from
    ~40 s to ~80 s after the request and fixed offsets missed the block.  Each
    step reports the target's scheduler/syscall state before and after
    delivery, so a poke that releases queued work is distinguishable from one
    that changes nothing.
    """

    holder: dict[str, object] = {}
    started = time.monotonic()

    def run() -> None:
        try:
            holder["value"] = _command(
                client,
                "WebDriver:NewSession",
                capabilities,
                deadline,
                limit=PROBE_TIMEOUT,
            )
        except BaseException as error:
            holder["error"] = error
        holder["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    # Reserve enough time for the plan itself, then wait for the block.
    longest_offset = max((offset for _, offset in plan), default=0.0)
    detect_deadline = max(
        started,
        deadline - longest_offset - 20.0,
    )
    anchor: float | None = None
    detected_age: int | None = None
    while worker.is_alive() and time.monotonic() < detect_deadline:
        age = _probe_main_block_age(pid)
        if age is not None and age >= 10000:
            anchor = time.monotonic()
            detected_age = age
            break
        worker.join(timeout=0.5)
    print(
        "FIREFOX_DRM_PROBE_BLOCK "
        f"detected={int(anchor is not None)} "
        f"age_jiffies={detected_age if detected_age is not None else 'none'} "
        f"elapsed_ms={round((time.monotonic() - started) * 1000, 1)}",
        flush=True,
    )
    # Fall back to the send time when no multi-second leader wait was seen.
    anchor = anchor if anchor is not None else started
    poked = 0
    for index, (target, offset) in enumerate(plan):
        while worker.is_alive():
            remaining = offset - (time.monotonic() - anchor)
            if remaining <= 0:
                break
            worker.join(timeout=min(0.5, remaining))
        if not worker.is_alive():
            print(
                "FIREFOX_DRM_PROBE_POKE_SKIPPED "
                f"index={index} target={target} offset_s={offset} "
                "reason=session-finished",
                flush=True,
            )
            break
        if target == "xstim":
            stimulus = _probe_x_stimulus()
            poked += 1
            print(
                "FIREFOX_DRM_PROBE_XSTIM "
                f"index={index} "
                f"elapsed_ms={round((time.monotonic() - anchor) * 1000, 1)} "
                + json.dumps(stimulus, sort_keys=True, separators=(",", ":")),
                flush=True,
            )
            continue
        tids = [pid] if target == "all" else _probe_thread_tids(pid, target)
        before = {str(tid): _probe_thread_state(pid, tid) for tid in tids}
        poke = _probe_poke(pid, target)
        poked += 1
        print(
            "FIREFOX_DRM_PROBE_POKE "
            f"index={index} "
            f"elapsed_ms={round((time.monotonic() - started) * 1000, 1)} "
            f"target={target} signal={PROBE_POKE_SIGNAL} "
            + json.dumps(poke, sort_keys=True, separators=(",", ":"))[:2000],
            flush=True,
        )
        worker.join(timeout=2.0)
        after = {
            str(tid): _probe_thread_state(pid, tid)
            for tid in (
                [pid] if target == "all" else _probe_thread_tids(pid, target)
            )
        }
        print(
            "FIREFOX_DRM_PROBE_POKE_EFFECT "
            f"index={index} target={target} "
            f"session_ready={int(not worker.is_alive())} "
            + json.dumps(
                {"before": before, "after": after},
                sort_keys=True,
                separators=(",", ":"),
            )[:4000],
            flush=True,
        )
    while worker.is_alive() and time.monotonic() < deadline + 2.0:
        worker.join(timeout=0.5)
    print(
        "FIREFOX_DRM_PROBE_NEWSESSION_RESULT "
        f"elapsed_ms={holder.get('elapsed_ms', 'unknown')} pokes={poked} "
        f"finished={int(not worker.is_alive())}",
        flush=True,
    )
    if "error" in holder:
        raise holder["error"]  # type: ignore[misc]
    if "value" not in holder:
        raise TimeoutError("Marionette read deadline expired")
    return holder["value"]


def _run_probe(process: subprocess.Popen[bytes]) -> bool:
    """Run only greeting + NewSession, then publish process evidence."""

    started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    deadline = time.monotonic() + PROBE_TIMEOUT
    phase = "connect"
    status = "fail"
    reason = "probe did not complete"
    client = None
    samples: list[dict[str, object]] = []
    sample_lock = threading.Lock()
    try:
        _phase("probe-connect")
        client = _connect("127.0.0.1", MARIONETTE_PORT, deadline)
        phase = "greeting"
        print("FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass", flush=True)
        _phase("probe-new-session")
        phase = "new-session"
        # Capture a baseline before sending NewSession.  The periodic sampler
        # can be delayed by a slow procfs read on this guest, so relying on it
        # alone made short failures lose the only useful thread snapshot.
        _probe_sample(process, samples, sample_lock)
        sample_stop = threading.Event()
        sampler = threading.Thread(
            target=_probe_sampler,
            args=(process, sample_stop, samples, sample_lock),
            daemon=True,
        )
        sampler.start()
        try:
            capabilities = _new_session_capabilities()
            capabilities_json = json.dumps(
                capabilities, sort_keys=True, separators=(",", ":")
            )
            print(
                "FIREFOX_DRM_PROBE_NEWSESSION_REQUEST "
                f"mode={os.environ.get('ASTERINAS_FIREFOX_NEWSESSION_CAPS', 'minimal')} "
                f"bytes={len(capabilities_json.encode('utf-8'))} "
                f"sha256={hashlib.sha256(capabilities_json.encode('utf-8')).hexdigest()} "
                f"json={capabilities_json}",
                flush=True,
            )
            poke_plan = _probe_poke_plan()
            if poke_plan:
                print(
                    "FIREFOX_DRM_PROBE_POKE_PLAN "
                    + json.dumps(poke_plan, separators=(",", ":")),
                    flush=True,
                )
                session = _probe_new_session_with_pokes(
                    client,
                    capabilities,
                    deadline,
                    poke_plan,
                    process.pid,
                )
            else:
                session = _command(
                    client,
                    "WebDriver:NewSession",
                    capabilities,
                    deadline,
                    limit=PROBE_TIMEOUT,
                )
        finally:
            try:
                # Take a terminal sample while Firefox is still alive, before
                # the receipt and process cleanup.  This is the boundary that
                # distinguishes a guest IPC wait from a dead browser.
                _probe_sample(process, samples, sample_lock)
            except BaseException as error:
                print(
                    "FIREFOX_DRM_PROBE_SAMPLE_ERROR "
                    f"phase=terminal type={type(error).__name__}",
                    flush=True,
                )
            sample_stop.set()
            sampler.join(timeout=2.0)
        if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str):
            raise GateError("probe NewSession response had no sessionId")
        status = "pass"
        reason = "NewSession returned a sessionId"
        print("FIREFOX_DRM_PROBE_BOUNDARY name=new-session status=pass", flush=True)
        return True
    except BaseException as error:
        reason = f"{type(error).__name__}: {error}"
        print(
            "FIREFOX_DRM_PROBE_BOUNDARY "
            f"name={phase} status=fail type={type(error).__name__}",
            flush=True,
        )
        return False
    finally:
        if client is not None:
            try:
                client.close()
            except BaseException:
                pass
        _publish_probe_receipt(
            started_utc=started_utc,
            status=status,
            phase=phase,
            reason=reason,
            process=process,
            samples=samples,
        )
        print(
            "FIREFOX_DRM_PROBE_DONE "
            f"status={status} phase={phase} reason={'_'.join(reason.split())[:160]}",
            flush=True,
        )


def _browser_log_marker() -> None:
    try:
        full_payload = FIREFOX_LOG.read_bytes()
    except OSError:
        return
    payload = full_payload[:BROWSER_LOG_MAX_BYTES]
    text = payload.decode("utf-8", "replace")
    diagnostic_lines: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        compact = " ".join(line.split())
        if compact and re.search(
            r"egl|glx|webgl|gpu|compositor|renderer|blacklist|sanity|accelerat"
            r"|mesa|gl_out_of_memory|out.of.memory",
            compact,
            re.IGNORECASE,
        ):
            # Repeated crash annotations used to fill the whole budget before
            # the first WebGL failure line was reached; keep distinct lines and
            # report the most recent ones.
            if compact in seen:
                continue
            seen.add(compact)
            diagnostic_lines.append(compact[:BROWSER_DIAGNOSTIC_LINE_BYTES])
    diagnostic_lines = diagnostic_lines[-BROWSER_DIAGNOSTIC_MAX_LINES:]
    print(
        "FIREFOX_DRM_BROWSER_DIAG "
        f"lines={len(diagnostic_lines)} truncated={int(len(full_payload) > len(payload))} "
        f"text={' | '.join(diagnostic_lines) or 'none'}",
        flush=True,
    )
    # A crash-probe backtrace is the only evidence left by a silent signal
    # death, and neither the diagnostic filter nor the bounded head/tail
    # token would keep it.  Preserve the frames verbatim.
    crash_lines = [
        " ".join(line.split())[:BROWSER_DIAGNOSTIC_LINE_BYTES]
        for line in text.splitlines()
        if line.startswith(
            (
                "ASTERINAS_CRASH_PROBE",
                "ASTERINAS_CRASH_FRAME",
                "ASTERINAS_CRASH_REG",
                "ASTERINAS_CRASH_MODULE",
            )
        )
    ]
    if crash_lines:
        print(
            f"FIREFOX_DRM_CRASH_TRACE lines={len(crash_lines)}",
            flush=True,
        )
        for line in crash_lines[:64]:
            print(f"FIREFOX_DRM_CRASH_TRACE_FRAME {line}", flush=True)
    # The tail is where an OOM/crash narrative ends; the bounded head/tail
    # token below would cut it off. Four KiB is affordable on failure and is
    # often the only record of Mesa's own error line.
    tail = text[-4096:]
    tail = " ".join(tail.split())[:4096]
    if tail:
        print(f"FIREFOX_DRM_BROWSER_TAIL text={tail}", flush=True)
    payload = payload[:256] + b" ... " + payload[-768:]
    printable = "".join(chr(byte) if 32 <= byte < 127 else " " for byte in payload)
    token = " ".join(printable.split())[:240]
    if token:
        print(f"FIREFOX_DRM_BROWSER_LOG text={token}", flush=True)


def _browser_renderer_from_log(scene_renderer: str) -> tuple[str, str, str]:
    """Extract WebRender/renderer evidence from the browser's stderr log.

    Debian's ESR build compiles NSPR logging out, so ``MOZ_LOG`` never
    produces a file in this guest (every run measured ``mozlog_bytes=0``).
    WebRender still identifies itself on stderr: its render thread and
    renderer log through the crash-annotation channel even in release
    builds. The page's WebGL renderer is the fallback label when the
    browser does not name the driver (Firefox sanitizes it to
    "Generic Renderer" on real GPUs).
    """

    payload = FIREFOX_LOG.read_bytes()
    text = payload.decode("utf-8", "replace")
    lowered = text.lower()
    webrender_evidence = any(
        marker in text
        for marker in ("RenderThread", "NewRenderer", "wr_renderer", "WebRender", "webrender")
    )
    print(
        "FIREFOX_DRM_BROWSER_EVIDENCE "
        f"bytes={len(payload)} sha256={hashlib.sha256(payload).hexdigest()} "
        f"webrender={int(webrender_evidence)} "
        f"virgl={int('virgl' in lowered)} "
        f"swgl={int('rendercompositorswgl' in lowered)} "
        f"llvmpipe={int('llvmpipe' in lowered)} "
        f"softpipe={int('softpipe' in lowered)} "
        f"scene_renderer={'_'.join(scene_renderer.split())[:80] or 'none'}",
        flush=True,
    )
    if any(
        marker in lowered
        for marker in (
            "rendercompositorswgl",
            "llvmpipe",
            "softpipe",
            "software webrender",
        )
    ):
        raise GateError("Firefox browser log contains software renderer evidence")
    if not webrender_evidence:
        raise GateError("Firefox browser log has no WebRender compositor evidence")
    if "virgl" in lowered:
        renderer = "virgl"
    elif scene_renderer:
        renderer = scene_renderer
    else:
        raise GateError("Firefox browser log has no renderer label")
    return "WebRender", "available", renderer


def _one_run(process: subprocess.Popen[bytes]) -> dict[str, object]:
    _phase("connect")
    # Keep a short process-state receipt before the socket wait.  A full
    # exception string is not reliable on the early serial console, while a
    # single bounded marker survives console backpressure.
    _phase("connect-state")
    print(
        f"FIREFOX_DRM_PROCESS state={'exited' if process.poll() is not None else 'running'}",
        flush=True,
    )
    deadline = time.monotonic() + SCENE_TIMEOUT
    client = _connect("127.0.0.1", MARIONETTE_PORT, deadline)
    try:
        _phase("new-session")
        try:
            session = _command(
                client,
                "WebDriver:NewSession",
                _new_session_capabilities(),
                deadline,
                # TCG plus a cold Firefox profile can take longer than the
                # ordinary 15-second Marionette command bound before the
                # first browser session exists. Keep the larger bound local
                # to this bootstrap command; the whole run remains capped by
                # deadline.
                limit=NEW_SESSION_TIMEOUT,
            )
        except BaseException as error:
            print(
                "FIREFOX_DRM_BOUNDARY "
                f"name=new-session status=error type={type(error).__name__} "
                f"elapsed_ms={(time.monotonic() - _GATE_START_MONOTONIC) * 1000.0:.1f}",
                flush=True,
            )
            raise
        print(
            "FIREFOX_DRM_BOUNDARY "
            f"name=new-session status=pass "
            f"elapsed_ms={(time.monotonic() - _GATE_START_MONOTONIC) * 1000.0:.1f}",
            flush=True,
        )
        if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str):
            raise GateError("Firefox WebRender scene did not create a session")
        _phase("window-rect")
        _command(
            client,
            "WebDriver:SetWindowRect",
            {"x": 0, "y": 0, "width": WINDOW_WIDTH, "height": WINDOW_HEIGHT},
            deadline,
        )
        _phase("window-context")
        handles = _command(client, "WebDriver:GetWindowHandles", None, deadline)
        if not isinstance(handles, list) or not handles or not isinstance(handles[0], str):
            raise GateError("Firefox did not expose a usable browsing context")
        _command(
            client,
            "WebDriver:SwitchToWindow",
            {"handle": handles[0], "focus": False},
            deadline,
        )
        _phase("transport-probe")
        probe = _script(client, "return 1;", deadline=deadline)
        if probe != 1:
            raise GateError("Firefox Marionette transport probe returned an unexpected value")
        print("FIREFOX_DRM_TRANSPORT probe=pass", flush=True)
        _phase("about-support")
        # Marionette cannot reliably inspect privileged about:support on
        # current ESR builds. A timed-out privileged command leaves an
        # unresolved response and makes the session unsafe to reuse. Keep
        # the browser session dedicated to the fixed local scene and defer
        # the equivalent WebRender/virgl proof to the flushed MOZ log after
        # Firefox exits.
        print(
            "FIREFOX_DRM_SUPPORT_FALLBACK reason=privileged_page_deferred",
            flush=True,
        )
        values = None
        _phase("scene")
        _script(client, _INSTALL_SCENE_ERROR_HOOK, deadline=deadline)
        # The default 15 s command limit is below a cold scene load on TCG:
        # the page's script and first paint exceed it, so the response arrived
        # after the read deadline and the session was abandoned.  Bound the
        # navigation by the scene phase, which the poll loop below shares.
        _command(
            client,
            "WebDriver:Navigate",
            {"url": SCENE_URL},
            deadline,
            limit=SCENE_TIMEOUT,
        )
        samples, scene_renderer, webgl_context, webgl_debug = _wait_scene(client, deadline)
        # Record what the browser itself says when a WebGL context cannot be
        # created: the scene only sees a null result, and the browser log may
        # not reach the diagnostic window.
        try:
            webgl_creation = _script(
                client, _WEBGL_CREATION_SCRIPT, deadline=deadline
            )
        except BaseException as error:
            webgl_creation = {"error": type(error).__name__}
        if isinstance(webgl_creation, str):
            print(f"FIREFOX_DRM_WEBGL_CREATION {webgl_creation}", flush=True)
        else:
            print(
                "FIREFOX_DRM_WEBGL_CREATION "
                + json.dumps(
                    webgl_creation, sort_keys=True, separators=(",", ":")
                ),
                flush=True,
            )
        if len(samples) < 16:
            raise GateError("Firefox scene produced too few frame samples")
        if not scene_renderer:
            raise GateError(
                "Firefox WebGL renderer was not reported "
                f"context={int(webgl_context)} debug={int(webgl_debug)}"
            )
        if any(
            marker in scene_renderer.lower()
            for marker in ("llvmpipe", "softpipe", "software rasterizer")
        ):
            raise GateError(f"Firefox WebGL renderer is software: {scene_renderer}")
        _phase("screenshot")
        viewport_width, viewport_height = _viewport(client, deadline)
        print(
            "FIREFOX_DRM_VIEWPORT "
            f"width={viewport_width} height={viewport_height} dpr=1",
            flush=True,
        )
        width, height, digest = _screenshot(
            client, deadline, (viewport_width, viewport_height)
        )
        return {
            "support": values,
            # about:support is intentionally deferred until the browser
            # process has been stopped and its MOZ log has been flushed.
            # Keep the per-run record explicit so a successful scene cannot
            # hit an unbound local while the renderer evidence is deferred.
            "support_source": "browser-log-deferred",
            "scene_renderer": scene_renderer,
            "samples": samples,
            "screenshot": (width, height, digest),
        }
    finally:
        client.close()


def main() -> int:
    os.environ["ASTERINAS_MARIONETTE_DEBUG_ERRORS"] = "1"
    # The Marionette client reads diagnostics in its own process. Setting the
    # flag here (rather than only in Firefox's child environment) makes the
    # command/send/response boundary observable in the serial receipt.
    os.environ["ASTERINAS_MARIONETTE_DIAGNOSTICS"] = "1"
    _wait_for_display()
    if PROBE_ONLY:
        _render_access_marker()
        _glxtest_marker()
        _phase("probe-profile")
        _write_profile()
        print(f"FIREFOX_DRM_START url={START_URL[:200]}", flush=True)
        _phase("probe-spawn")
        process = _spawn()
        watch_stop, watch_threads = _start_browser_watchers(process)
        try:
            return 0 if _run_probe(process) else 1
        finally:
            _stop_browser_watchers(process, watch_stop, watch_threads)
            _stop(process)
            _probe_log_after_stop()
    _render_access_marker()
    _egl_probe_marker()
    _glxtest_marker()
    _phase("profile")
    _write_profile()
    _phase("spawn-first")
    first = _spawn()
    first_watch_stop, first_watch_threads = _start_browser_watchers(first)
    first_evidence: dict[str, object] | None = None
    try:
        first_evidence = _one_run(first)
    finally:
        _stop_browser_watchers(first, first_watch_stop, first_watch_threads)
        _stop(first)
        _browser_log_marker()
    if first_evidence is None:
        raise GateError("Firefox fixed scene did not produce evidence")
    scene_renderer = str(first_evidence["scene_renderer"])
    values = first_evidence["support"]
    if not isinstance(values, dict):
        compositing, webrender, renderer = _browser_renderer_from_log(scene_renderer)
        values = {
            "compositing": compositing,
            "webrender": webrender,
            "renderer": renderer,
        }
        support_source = "browser-log"
    else:
        support_source = str(first_evidence["support_source"])
    renderer_value = str(values["renderer"])
    print(
        "FIREFOX_DRM_SUPPORT "
        f"compositing={str(values['compositing']).replace(' ', '_')} "
        f"webrender={str(values['webrender']).replace(' ', '_')} "
        f"renderer={renderer_value.replace(' ', '_')} "
        f"source={support_source}",
        flush=True,
    )
    print(
        "FIREFOX_DRM_GL "
        f"renderer={'_'.join(scene_renderer.split()) or 'unreported'}",
        flush=True,
    )
    width, height, digest = first_evidence["screenshot"]
    print(f"FIREFOX_DRM_SCREENSHOT width={width} height={height} sha256={digest}", flush=True)
    samples = first_evidence["samples"]
    if not isinstance(samples, list) or not samples:
        raise GateError("Firefox scene evidence has no frame samples")
    print(
        "FIREFOX_DRM_FRAMES "
        f"samples={len(samples)} min_ms={min(samples):.3f} "
        f"p50_ms={_percentile(samples, 0.50):.3f} "
        f"p95_ms={_percentile(samples, 0.95):.3f} max_ms={max(samples):.3f}",
        flush=True,
    )
    _phase("spawn-second")
    _recovery_residue_marker()
    _clear_profile_lock()
    second = _spawn()
    second_watch_stop, second_watch_threads = _start_browser_watchers(second)
    recovered = False
    try:
        _phase("recovery")
        deadline = time.monotonic() + SCENE_TIMEOUT
        client = _connect("127.0.0.1", MARIONETTE_PORT, deadline)
        try:
            # Marionette does not implement WebDriver:Status (it answers
            # "unknown command"); only a real NewSession proves the restarted
            # browser can serve a session again.
            session = _command(
                client,
                "WebDriver:NewSession",
                _new_session_capabilities(),
                deadline,
                limit=NEW_SESSION_TIMEOUT,
            )
            if not isinstance(session, dict) or not isinstance(
                session.get("sessionId"), str
            ):
                raise GateError("Firefox did not recover after restart")
        finally:
            client.close()
        print("FIREFOX_DRM_RECOVERY restart=pass", flush=True)
        recovered = True
    finally:
        _stop_browser_watchers(second, second_watch_stop, second_watch_threads)
        _stop(second)
        if not recovered:
            # The second browser writes to the same captured log; without
            # this a reset during recovery has no explainable cause.
            _browser_log_marker()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException as error:
        print(
            f"FIREFOX_DRM_FAIL_TYPE type={type(error).__name__}",
            flush=True,
        )
        reason = "_".join(str(error).split())[:160]
        print(f"FIREFOX_DRM_FAIL_REASON reason={reason}", flush=True)
        print(f"FIREFOX_DRM_FAIL reason={type(error).__name__}:{error}", file=sys.stderr, flush=True)
        raise
