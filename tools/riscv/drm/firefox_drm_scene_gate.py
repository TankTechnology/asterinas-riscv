#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Classify the bounded Firefox DRM/WebRender scene receipt.

The browser workload emits a render-node access record followed by five
ordered records after it has connected to the DRM desktop: a renderer record,
a WebGL renderer record, a screenshot digest, a frame-time summary, and a
recovery record. The renderer record may come from ``about:support`` when the
Firefox build exposes that page to Marionette, or from the browser's bounded
WebRender log plus the page's WebGL renderer probe.  Keeping this parser
independent from Marionette makes the host gate deterministic and lets
fault-injection tests exercise the acceptance contract without starting
Firefox.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
import sys
from typing import Sequence

try:
    from tools.riscv.debian.rootfs.firefox_compat import runtime_provenance_gate
except ModuleNotFoundError:  # Direct invocation with tools/riscv on PYTHONPATH.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from debian.rootfs.firefox_compat import runtime_provenance_gate  # type: ignore[no-redef]


MAX_TRANSCRIPT_BYTES = 256 * 1024
MAX_SERIAL_BYTES = 8 * 1024 * 1024
# The guest fixes the outer browser window at 1280x800. Marionette's
# non-full screenshot is the content viewport, whose measured size varies a
# few pixels with the toolkit's resize timing (1280x696 and 1278x690 were
# both observed). The guest reports the page's innerWidth/innerHeight and the
# PNG must equal it exactly; these bounds keep that viewport tied to the fixed
# outer window instead of accepting an arbitrary page.
VIEWPORT_MIN_WIDTH = 1200
VIEWPORT_MAX_WIDTH = 1280
VIEWPORT_MIN_HEIGHT = 600
VIEWPORT_MAX_HEIGHT = 760
MIN_FRAME_SAMPLES = 16
MAX_FRAME_TIME_MS = 5000.0

_ACCESS = re.compile(
    r"^FIREFOX_DRM_ACCESS .*device=renderD128 .*read=1 write=1$"
)
_SUPPORT = re.compile(
    r"^FIREFOX_DRM_SUPPORT compositing=(?P<compositing>\S+) "
    r"webrender=(?P<webrender>\S+) renderer=(?P<renderer>\S+)"
    r"(?: source=(?P<source>\S+))?$"
)
_GL = re.compile(r"^FIREFOX_DRM_GL renderer=(?P<renderer>\S+)$")
_VIEWPORT = re.compile(
    r"^FIREFOX_DRM_VIEWPORT width=(?P<width>[0-9]+) "
    r"height=(?P<height>[0-9]+) dpr=1$"
)
_SCREENSHOT = re.compile(
    r"^FIREFOX_DRM_SCREENSHOT width=(?P<width>[0-9]+) "
    r"height=(?P<height>[0-9]+) sha256=(?P<sha256>[0-9a-f]{64})$"
)
_FRAMES = re.compile(
    r"^FIREFOX_DRM_FRAMES samples=(?P<samples>[0-9]+) "
    r"min_ms=(?P<min>[0-9]+(?:\.[0-9]+)?) "
    r"p50_ms=(?P<p50>[0-9]+(?:\.[0-9]+)?) "
    r"p95_ms=(?P<p95>[0-9]+(?:\.[0-9]+)?) "
    r"max_ms=(?P<max>[0-9]+(?:\.[0-9]+)?)$"
)
_RECOVERY = re.compile(r"^FIREFOX_DRM_RECOVERY restart=(?P<restart>pass)$")


def classify_browser_log(log: bytes | str) -> tuple[str, str, str]:
    """Extract independent WebRender/virgl evidence from Firefox's GFX log.

    ``about:support`` is a privileged document and recent ESR builds reject
    Marionette navigation or content-script access to it.  The log fallback
    must remain strict: a WebRender-looking line is insufficient when SWGL or
    a software renderer also appears, and a virgl renderer without a WebRender
    compositor is not an acceleration proof.
    """

    raw = log.encode("utf-8", "replace") if isinstance(log, str) else log
    if len(raw) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("Firefox browser log exceeds the byte cap")
    text = raw.decode("utf-8", "replace")
    lowered = text.lower()
    if any(marker in lowered for marker in ("rendercompositorswgl", "llvmpipe", "softpipe")):
        raise ValueError("Firefox browser log contains software renderer evidence")
    if "webrender" not in lowered and "rendercompositorogl" not in lowered:
        raise ValueError("Firefox browser log has no WebRender compositor evidence")
    renderer = re.search(r"(?:renderer|driver renderer)\s*[=:]\s*([^\r\n]+)", text, re.IGNORECASE)
    if renderer is None:
        raise ValueError("Firefox browser log has no renderer evidence")
    renderer_name = renderer.group(1).strip()
    if not renderer_name:
        raise ValueError("Firefox browser log has an empty renderer field")
    if any(marker in renderer_name.lower() for marker in ("llvmpipe", "softpipe", "software rasterizer")):
        raise ValueError("Firefox browser log contains software renderer evidence")
    # Firefox ESR can sanitize a raw GL_RENDERER=virgl value to
    # "Generic Renderer".  Independent renderD128 and pixel receipts prove
    # the hardware path; this parser must preserve the observed label rather
    # than require or invent the string "virgl".
    return "WebRender", "available", renderer_name


@dataclass(frozen=True)
class FirefoxDrmSceneResult:
    """Host-readable result for one fixed browser scene."""

    passed: bool
    reason: str
    renderer: str = ""
    screenshot_sha256: str = ""
    samples: int = 0
    p95_ms: float = 0.0
    package: str = ""
    package_version: str = ""
    packages_lock_sha256: str = ""


def _single_match(lines: list[str], pattern: re.Pattern[str], label: str) -> re.Match[str] | None:
    matches = [pattern.fullmatch(line) for line in lines]
    matches = [match for match in matches if match is not None]
    if len(matches) != 1:
        return None
    return matches[0]


def _finite_float(value: str, label: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or parsed > MAX_FRAME_TIME_MS:
        raise ValueError(f"{label} is outside the bounded frame-time range")
    return parsed


def classify_scene(transcript: bytes | str) -> FirefoxDrmSceneResult:
    """Require one ordered, virgl/WebRender fixed-scene receipt."""

    raw = transcript.encode("utf-8", "replace") if isinstance(transcript, str) else transcript
    if len(raw) > MAX_SERIAL_BYTES:
        raise ValueError("Firefox DRM scene serial exceeds the byte cap")
    text = raw.decode("utf-8", "replace")
    # QEMU/systemd prefixes guest lines with the unit and PID.  Transport
    # diagnostics can make the complete serial much larger than the compact
    # acceptance records, so strip the prefix before applying the parser's
    # tighter record cap.  Unknown FIREFOX_DRM_* lines remain harmless and
    # are ignored by the exact record regexes below.
    lines = []
    for line in text.splitlines():
        marker = line.find("FIREFOX_DRM_")
        if marker >= 0:
            lines.append(line[marker:])
    compact = "\n".join(lines).encode("utf-8", "replace")
    if len(compact) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("Firefox DRM scene records exceed the byte cap")
    access = _single_match(lines, _ACCESS, "render node access")
    support = _single_match(lines, _SUPPORT, "support")
    gl = _single_match(lines, _GL, "GL renderer")
    viewport = _single_match(lines, _VIEWPORT, "viewport")
    screenshot = _single_match(lines, _SCREENSHOT, "screenshot")
    frames = _single_match(lines, _FRAMES, "frames")
    recovery = _single_match(lines, _RECOVERY, "recovery")
    if access is None:
        return FirefoxDrmSceneResult(False, "missing or invalid /dev/dri/renderD128 access receipt")
    if support is None:
        return FirefoxDrmSceneResult(False, "missing or duplicate renderer receipt")
    if gl is None:
        return FirefoxDrmSceneResult(False, "missing or duplicate WebGL renderer receipt")
    if viewport is None:
        return FirefoxDrmSceneResult(False, "missing or duplicate viewport receipt")
    if screenshot is None:
        return FirefoxDrmSceneResult(False, "missing or duplicate screenshot receipt")
    if frames is None:
        return FirefoxDrmSceneResult(False, "missing or duplicate frame receipt")
    if recovery is None:
        return FirefoxDrmSceneResult(False, "missing or duplicate recovery receipt")

    compositing = support.group("compositing").lower()
    webrender = support.group("webrender").lower()
    renderer = support.group("renderer").lower()
    if compositing != "webrender":
        return FirefoxDrmSceneResult(False, "Firefox compositor is not WebRender")
    if webrender != "available":
        return FirefoxDrmSceneResult(False, "Firefox WebRender is unavailable")
    if not renderer:
        return FirefoxDrmSceneResult(False, "Firefox renderer label is empty")
    if any(marker in renderer for marker in ("llvmpipe", "softpipe", "software rasterizer")):
        return FirefoxDrmSceneResult(False, "Firefox renderer is software")
    gl_renderer = gl.group("renderer").lower()
    if not gl_renderer:
        return FirefoxDrmSceneResult(False, "Firefox WebGL renderer label is empty")
    if any(marker in gl_renderer for marker in ("llvmpipe", "softpipe", "software rasterizer")):
        return FirefoxDrmSceneResult(False, "Firefox WebGL renderer is software")

    viewport_width = int(viewport.group("width"))
    viewport_height = int(viewport.group("height"))
    if not (
        VIEWPORT_MIN_WIDTH <= viewport_width <= VIEWPORT_MAX_WIDTH
        and VIEWPORT_MIN_HEIGHT <= viewport_height <= VIEWPORT_MAX_HEIGHT
    ):
        return FirefoxDrmSceneResult(False, "content viewport is outside the fixed window")
    if (
        int(screenshot.group("width")) != viewport_width
        or int(screenshot.group("height")) != viewport_height
    ):
        return FirefoxDrmSceneResult(
            False, "screenshot geometry does not match the content viewport"
        )

    samples = int(frames.group("samples"))
    if samples < MIN_FRAME_SAMPLES:
        return FirefoxDrmSceneResult(False, "frame sample count is too small")
    try:
        minimum = _finite_float(frames.group("min"), "min_ms")
        p50 = _finite_float(frames.group("p50"), "p50_ms")
        p95 = _finite_float(frames.group("p95"), "p95_ms")
        maximum = _finite_float(frames.group("max"), "max_ms")
    except ValueError as error:
        return FirefoxDrmSceneResult(False, str(error))
    if not minimum <= p50 <= p95 <= maximum:
        return FirefoxDrmSceneResult(False, "frame-time quantiles are not ordered")

    # Check protocol ordering separately from per-record uniqueness.  This
    # prevents a stale screenshot from a previous run being paired with a
    # fresh renderer receipt.
    positions = [
        next(index for index, line in enumerate(lines) if pattern.fullmatch(line))
        for pattern in (
            _ACCESS,
            _VIEWPORT,
            _SUPPORT,
            _GL,
            _SCREENSHOT,
            _FRAMES,
            _RECOVERY,
        )
    ]
    if positions != sorted(positions):
        return FirefoxDrmSceneResult(False, "scene receipts are out of order")
    return FirefoxDrmSceneResult(
        True,
        "passed",
        renderer=support.group("renderer"),
        screenshot_sha256=screenshot.group("sha256"),
        samples=samples,
        p95_ms=p95,
    )


def classify_scene_with_runtime(
    transcript: bytes | str,
    packages_lock: Path,
    source_manifest: Path,
) -> FirefoxDrmSceneResult:
    """Require a passing scene and an identity-matched Firefox runtime.

    A scene transcript alone cannot establish which rootfs supplied the
    browser binary.  Bind the host-side receipt to the package lock used for
    that QEMU run before accepting the scene as a Task 6 candidate.
    """

    result = classify_scene(transcript)
    if not result.passed:
        return result
    try:
        runtime = runtime_provenance_gate.run_gate(packages_lock, source_manifest)
    except runtime_provenance_gate.GateError as error:
        return replace(result, passed=False, reason=f"runtime package identity: {error}")
    return replace(
        result,
        package=runtime.package,
        package_version=runtime.version,
        packages_lock_sha256=runtime.packages_lock_sha256,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--packages-lock", type=Path)
    parser.add_argument("--source-manifest", type=Path)
    values = parser.parse_args(arguments)
    if (values.packages_lock is None) != (values.source_manifest is None):
        parser.error("--packages-lock and --source-manifest must be provided together")
    if values.packages_lock is None:
        result = classify_scene(values.transcript.read_bytes())
    else:
        result = classify_scene_with_runtime(
            values.transcript.read_bytes(), values.packages_lock, values.source_manifest
        )
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
