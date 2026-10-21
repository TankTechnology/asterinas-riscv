#!/usr/bin/python3
# SPDX-License-Identifier: MPL-2.0

"""E3 fast ladder: bounded L0/L1/L2 probes for the guest Firefox stack."""

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

HOME = Path("/home/asterinas")
PROFILE = Path("/tmp/e3-ladder-profile")
SHOT = Path("/tmp/e3-ladder-l2.png")
MOZ_LOG = Path("/tmp/e3-ladder-moz.log")
BROWSER_LOG = Path("/tmp/e3-ladder-browser.log")

PROFILE_PREFS = [
    'user_pref("gfx.webrender.all", true);',
    'user_pref("gfx.webrender.enabled", true);',
    'user_pref("webgl.force-enabled", true);',
    'user_pref("dom.ipc.processCount", 1);',
    'user_pref("fission.autostart", false);',
    'user_pref("browser.startup.page", 0);',
    'user_pref("browser.sessionstore.resume_from_crash", false);',
    'user_pref("browser.newtabpage.enabled", false);',
    'user_pref("app.update.enabled", false);',
    'user_pref("network.captive-portal-service.enabled", false);',
    'user_pref("network.connectivity-service.enabled", false);',
]


def _compact(value: object, limit: int = 1600) -> str:
    return " ".join(str(value).split())[:limit]


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _run(
    argv: list[str],
    timeout: float,
    *,
    env: dict[str, str] | None = None,
    stdout_path: Path | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    output = None
    try:
        if stdout_path is None:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        else:
            output = stdout_path.open("wb")
            process = subprocess.Popen(
                argv,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
    except OSError as error:
        if output is not None:
            output.close()
        return {
            "rc": None,
            "error": type(error).__name__,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }
    timed_out = False
    try:
        captured, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        captured, _ = process.communicate()
    finally:
        if output is not None:
            output.close()
    return {
        "rc": process.returncode,
        "timed_out": timed_out,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "output": _compact((captured or b"").decode("utf-8", "replace")),
    }


def _receipt(step: str, payload: dict[str, object]) -> None:
    payload = {"step": step, **payload}
    print(
        "E3_LADDER_RECEIPT "
        + json.dumps(payload, sort_keys=True, separators=(",", ":")),
        flush=True,
    )


def _write_profile() -> dict[str, object]:
    PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
    user_js = PROFILE / "user.js"
    user_js.write_text("\n".join(PROFILE_PREFS) + "\n", encoding="utf-8")
    return {
        "path": str(PROFILE),
        "exists": PROFILE.is_dir(),
        "user_js_bytes": user_js.stat().st_size,
        "user_js_sha256": _sha256(user_js),
    }


def _wait_for_display(timeout: float = 20.0) -> dict[str, object]:
    started = time.monotonic()
    deadline = started + timeout
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        result = _run(
            ["/usr/bin/xdpyinfo", "-display", ":0"],
            3.0,
            env={**os.environ, "DISPLAY": ":0"},
        )
        if result.get("rc") == 0:
            return {
                "ready": True,
                "attempts": attempts,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            }
        time.sleep(1.0)
    return {
        "ready": False,
        "attempts": attempts,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
    }


def _display_env() -> dict[str, str]:
    xauthority = (
        "/run/asterinas.Xauthority"
        if Path("/run/asterinas.Xauthority").is_file()
        else "/home/asterinas/.Xauthority"
    )
    return {**os.environ, "DISPLAY": ":0", "XAUTHORITY": xauthority}


def _run_l2(
    label: str,
    extra_env: dict[str, str],
    extra_prefs: list[str],
    timeout: float = 30.0,
) -> None:
    profile = Path(f"/tmp/e3-ladder-profile-{label}")
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    (profile / "user.js").write_text(
        "\n".join(PROFILE_PREFS + extra_prefs) + "\n",
        encoding="utf-8",
    )
    shot = Path(f"/tmp/e3-ladder-{label}.png")
    moz_log = Path(f"/tmp/e3-ladder-{label}-moz.log")
    browser_log = Path(f"/tmp/e3-ladder-{label}-browser.log")
    for path in (shot, moz_log, browser_log):
        path.unlink(missing_ok=True)
    url = (
        'data:text/html,<canvas id=c></canvas><script>'
        'document.title=!!document.getElementById("c").getContext("webgl")'
        "</script>"
    )
    env = {
        **_display_env(),
        "MOZ_LOG": "WebRender:5,Compositor:5,GLContext:5,gfx:5",
        "MOZ_LOG_FILE": str(moz_log),
        "RUST_LOG": "webrender=debug",
        "MOZ_WEBGL_FORCE_EGL": "1",
        **extra_env,
    }
    result = _run(
        [
            "/usr/bin/firefox-esr",
            "--no-remote",
            "--new-instance",
            "--offline",
            "--headless",
            "--profile",
            str(profile),
            "--screenshot",
            str(shot),
            url,
        ],
        timeout,
        env=env,
        stdout_path=browser_log,
    )
    moz_lines: list[str] = []
    if moz_log.is_file():
        for line in moz_log.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            compact = " ".join(line.split())
            if any(
                marker in compact
                for marker in (
                    "WebRender",
                    "Compositor",
                    "GLContext",
                    "WebGL",
                    "gfx",
                    "Render",
                )
            ):
                moz_lines.append(compact[:300])
    _receipt(
        label,
        {
            "command": "firefox-esr --headless --screenshot",
            "profile": str(profile),
            "extra_env": extra_env,
            "extra_prefs": extra_prefs,
            "result": result,
            "screenshot": {
                "exists": shot.is_file(),
                "bytes": shot.stat().st_size if shot.is_file() else 0,
                "sha256": _sha256(shot),
            },
            "moz_log": {
                "exists": moz_log.is_file(),
                "bytes": moz_log.stat().st_size if moz_log.is_file() else 0,
                "sha256": _sha256(moz_log),
                "lines": moz_lines[-40:],
            },
            "browser_log": {
                "exists": browser_log.is_file(),
                "bytes": browser_log.stat().st_size
                if browser_log.is_file()
                else 0,
                "tail": _compact(
                    browser_log.read_text(
                        encoding="utf-8", errors="replace"
                    )[-4096:]
                    if browser_log.is_file()
                    else ""
                ),
            },
        },
    )


def main() -> int:
    started = time.monotonic()
    _receipt(
        "L0",
        {
            "firefox_version": _run(["/usr/bin/firefox-esr", "--version"], 10.0),
            "profile": _write_profile(),
            "moz_log_env": os.environ.get("MOZ_LOG", ""),
            "moz_log_file_env": os.environ.get("MOZ_LOG_FILE", ""),
            "rust_log_env": os.environ.get("RUST_LOG", ""),
        },
    )
    display = _wait_for_display()
    _receipt(
        "L1",
        {
            "display": display,
            "glxtest": _run(
                ["/usr/lib/firefox-esr/glxtest"],
                10.0,
                env={**_display_env(), "MOZ_WEBGL_FORCE_EGL": "1"},
            ),
        },
    )

    _run_l2("L2b", {}, [], timeout=120.0)
    print(
        "E3_LADDER_DONE "
        f"elapsed_ms={round((time.monotonic() - started) * 1000, 1)}",
        flush=True,
    )
    # The host gate treats this contract failure marker as an early stop so a
    # completed ladder does not wait for a Firefox probe receipt that this
    # service intentionally never produces.
    print("DEBIAN_DESKTOP_M4_FAIL reason=e3-ladder-complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
