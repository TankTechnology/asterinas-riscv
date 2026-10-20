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
START_URL = SCENE_URL
MARIONETTE_PORT = 2828
# Cold ESR startup under RISC-V TCG has measured 249--458 seconds before the
# first NewSession response. Keep that phase bounded, but do not classify a
# known cold-start interval as a renderer failure. The service budget covers
# one scene run plus one bounded recovery launch.
SCENE_TIMEOUT = 900.0
NEW_SESSION_TIMEOUT = 840.0
SCENE_SETTLE_SECONDS = 30.0
SCENE_POLL_INTERVAL = 2.0
# The browser window is fixed at 1280x800 below. Marionette's non-full
# screenshot is the content viewport after Firefox chrome, which this guest
# reports as 1152x584. Keep the two contracts explicit instead of accepting
# arbitrary geometry.
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 800
SCREENSHOT_WIDTH = 1152
SCREENSHOT_HEIGHT = 584
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
        return {"strictFileInteractability": True}
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
BROWSER_DIAGNOSTIC_MAX_LINES = 12
BROWSER_DIAGNOSTIC_LINE_BYTES = 200
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


def _screenshot(client: object, deadline: float) -> tuple[int, int, str]:
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
    if (width, height) != (SCREENSHOT_WIDTH, SCREENSHOT_HEIGHT):
        raise GateError(
            f"Firefox screenshot geometry is {(width, height)}, "
            f"expected {SCREENSHOT_WIDTH}x{SCREENSHOT_HEIGHT} content viewport"
        )
    SCREENSHOT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    SCREENSHOT.write_bytes(payload)
    return width, height, hashlib.sha256(payload).hexdigest()


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
    # This is opt-in diagnostic tracing only.  It is enabled by a dedicated
    # short probe and never enters the normal Firefox scene acceptance path.
    if os.environ.get("ASTERINAS_FIREFOX_REMOTE_TRACE") == "1":
        preferences.append('user_pref("remote.log.level", "Trace");')
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
    moz_log = "timestamp,Widget:4,Layers:4,WebRender:4"
    if os.environ.get("ASTERINAS_FIREFOX_REMOTE_TRACE") == "1":
        moz_log += ",RemoteAgent:5,Marionette:5"
    return subprocess.Popen(
        command,
        env={
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
            "MOZ_LOG_FILE": str(FIREFOX_MOZ_LOG),
            # Transport diagnostics contain only command names, byte counts,
            # and timing state; they never include URLs, scripts, or response
            # payloads. They make a cold-session timeout auditable.
            "ASTERINAS_MARIONETTE_DIAGNOSTICS": "1",
        },
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
            process.wait(timeout=5)


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
        print(
            "FIREFOX_DRM_WATCH "
            f"pid={process.pid} state={state} rss_kib={rss_kib} "
            f"browser_log_bytes={log_bytes} mozlog_bytes={mozlog_bytes}",
            flush=True,
        )


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


def _probe_proc_snapshot(pid: int) -> dict[str, object]:
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
        thread_fields["asterinas_syscall"] = _probe_read(
            thread_root / "asterinas_syscall", 4096
        )[:2048]
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
            payload = path.read_bytes()
        except OSError:
            payload = b""
        tail = payload[-8192:].decode("utf-8", "replace")
        logs[name] = {
            "path": str(path),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "tail": " ".join(tail.split())[:4096],
        }
    return logs


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
        f"syscall={'present' if snapshot.get('asterinas_syscall', '').strip() else 'absent'}",
        flush=True,
    )
    print(
        "FIREFOX_DRM_PROBE_SOCKET "
        f"port={MARIONETTE_PORT} {_probe_socket_marker(socket_snapshot)}",
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
    receipt = {
        "schema": 1,
        "gate": "firefox-marionette-probe",
        "status": status,
        "started_utc": started_utc,
        "finished_utc": finished,
        "phase": phase,
        "reason": reason,
        "pid": process.pid,
        "process": _probe_proc_snapshot(process.pid),
        "logs": _probe_log_snapshot(),
        "sample_count": len(samples),
        "samples": samples[:PROBE_SAMPLE_LIMIT],
    }
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
        "FIREFOX_DRM_PROBE_SYSCALL "
        f"pid={process.pid} {_probe_syscall_summary(process_snapshot_value := receipt['process'].get('asterinas_syscall'))}",
        flush=True,
    )
    for thread in receipt["process"]["threads"][:32]:
        print(
            "FIREFOX_DRM_PROBE_THREAD "
            f"tid={thread['tid']} state={thread.get('State', 'unknown')} "
            f"wchan={thread.get('wchan', 'unknown') or 'unknown'} "
            f"syscall={_probe_syscall_summary(thread.get('asterinas_syscall'))}",
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
    diagnostic_lines: list[str] = []
    for line in payload.decode("utf-8", "replace").splitlines():
        compact = " ".join(line.split())
        if compact and re.search(
            r"egl|glx|webgl|gpu|compositor|renderer|blacklist|sanity|accelerat",
            compact,
            re.IGNORECASE,
        ):
            diagnostic_lines.append(compact[:BROWSER_DIAGNOSTIC_LINE_BYTES])
            if len(diagnostic_lines) == BROWSER_DIAGNOSTIC_MAX_LINES:
                break
    print(
        "FIREFOX_DRM_BROWSER_DIAG "
        f"lines={len(diagnostic_lines)} truncated={int(len(full_payload) > len(payload))} "
        f"text={' | '.join(diagnostic_lines) or 'none'}",
        flush=True,
    )
    payload = payload[:256] + b" ... " + payload[-768:]
    printable = "".join(chr(byte) if 32 <= byte < 127 else " " for byte in payload)
    token = " ".join(printable.split())[:240]
    if token:
        print(f"FIREFOX_DRM_BROWSER_LOG text={token}", flush=True)


def _browser_renderer_from_log() -> tuple[str, str, str]:
    payload = FIREFOX_MOZ_LOG.read_bytes()
    text = payload.decode("utf-8", "replace")
    lowered = text.lower()
    print(
        "FIREFOX_DRM_MOZLOG "
        f"bytes={len(payload)} sha256={hashlib.sha256(payload).hexdigest()} "
        f"webrender={int('webrender' in lowered)} "
        f"rendercompositorogl={int('rendercompositorogl' in lowered)} "
        f"virgl={int('virgl' in lowered)} "
        f"swgl={int('swgl' in lowered)} "
        f"llvmpipe={int('llvmpipe' in lowered)} "
        f"softpipe={int('softpipe' in lowered)}",
        flush=True,
    )
    if any(marker in lowered for marker in ("rendercompositorswgl", "llvmpipe", "softpipe")):
        raise GateError("Firefox browser log contains software renderer evidence")
    if "webrender" not in lowered and "rendercompositorogl" not in lowered:
        raise GateError("Firefox browser log has no WebRender compositor evidence")
    if "virgl" in lowered:
        renderer = "virgl"
    elif "generic renderer" in lowered:
        renderer = "Generic Renderer"
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
        _command(client, "WebDriver:Navigate", {"url": SCENE_URL}, deadline)
        samples, scene_renderer, webgl_context, webgl_debug = _wait_scene(client, deadline)
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
        width, height, digest = _screenshot(client, deadline)
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
        _phase("probe-spawn")
        process = _spawn()
        watch_stop = threading.Event()
        watch = threading.Thread(
            target=_browser_watch,
            args=(process, watch_stop),
            daemon=True,
        )
        watch.start()
        try:
            return 0 if _run_probe(process) else 1
        finally:
            watch_stop.set()
            watch.join(timeout=2.0)
            _stop(process)
    _render_access_marker()
    _egl_probe_marker()
    _glxtest_marker()
    _phase("profile")
    _write_profile()
    _phase("spawn-first")
    first = _spawn()
    first_watch_stop = threading.Event()
    first_watch = threading.Thread(
        target=_browser_watch,
        args=(first, first_watch_stop),
        daemon=True,
    )
    first_watch.start()
    first_evidence: dict[str, object] | None = None
    try:
        first_evidence = _one_run(first)
    finally:
        first_watch_stop.set()
        first_watch.join(timeout=2.0)
        _stop(first)
        _browser_log_marker()
    if first_evidence is None:
        raise GateError("Firefox fixed scene did not produce evidence")
    values = first_evidence["support"]
    if not isinstance(values, dict):
        compositing, webrender, renderer = _browser_renderer_from_log()
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
    scene_renderer = str(first_evidence["scene_renderer"])
    print(f"FIREFOX_DRM_GL renderer={scene_renderer or 'unreported'}", flush=True)
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
    second = _spawn()
    second_watch_stop = threading.Event()
    second_watch = threading.Thread(
        target=_browser_watch,
        args=(second, second_watch_stop),
        daemon=True,
    )
    second_watch.start()
    try:
        _phase("recovery")
        deadline = time.monotonic() + SCENE_TIMEOUT
        client = _connect("127.0.0.1", MARIONETTE_PORT, deadline)
        try:
            status = _command(client, "WebDriver:Status", None, deadline)
            if not isinstance(status, dict) or status.get("ready") is not True:
                raise GateError("Firefox did not recover after restart")
        finally:
            client.close()
        print("FIREFOX_DRM_RECOVERY restart=pass", flush=True)
    finally:
        second_watch_stop.set()
        second_watch.join(timeout=2.0)
        _stop(second)
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
