#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Classify a bounded Firefox about:support renderer report."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


MAX_REPORT_BYTES = 64 * 1024
COMPOSITING = re.compile(r"^\s*Compositing\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
WEBRENDER = re.compile(r"^\s*WebRender\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
GPU_RENDERER = re.compile(
    r"^\s*(?:(?:GPU\s*#\d+\s+)?Driver Renderer|GPU Renderer|Renderer)\s*:\s*(.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass(frozen=True)
class FirefoxWebRenderResult:
    passed: bool
    reason: str
    compositing: str = ""
    webrender: str = ""
    gpu_renderer: str = ""


def classify_report(report: bytes | str) -> FirefoxWebRenderResult:
    if isinstance(report, str):
        raw = report.encode("utf-8", "replace")
    else:
        raw = report
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("Firefox renderer report exceeds the byte cap")
    text = raw.decode("utf-8", "replace")
    compositing_match = COMPOSITING.search(text)
    webrender_match = WEBRENDER.search(text)
    renderer_match = GPU_RENDERER.search(text)
    compositing = compositing_match.group(1).strip() if compositing_match else ""
    webrender = webrender_match.group(1).strip() if webrender_match else ""
    renderer = renderer_match.group(1).strip() if renderer_match else ""

    if not compositing_match:
        return FirefoxWebRenderResult(False, "missing Compositing field", compositing, webrender, renderer)
    if compositing.lower() != "webrender":
        return FirefoxWebRenderResult(
            False, f"Firefox compositor is {compositing!r}, not WebRender", compositing, webrender, renderer
        )
    if not webrender_match:
        return FirefoxWebRenderResult(False, "missing WebRender field", compositing, webrender, renderer)
    if webrender.lower() != "available":
        return FirefoxWebRenderResult(
            False,
            f"Firefox WebRender status is {webrender!r}, not available",
            compositing,
            webrender,
            renderer,
        )
    lowered = text.lower()
    if any(marker in lowered for marker in ("swgl", "llvmpipe", "softpipe", "software rasterizer")):
        return FirefoxWebRenderResult(False, "software renderer marker present", compositing, webrender, renderer)
    if not renderer:
        return FirefoxWebRenderResult(False, "missing GPU renderer field", compositing, webrender, renderer)
    # Firefox ESR may sanitize a valid raw GL_RENDERER=virgl value to
    # "Generic Renderer".  The gate deliberately accepts that value; the
    # independent renderD128 and fixed-pixel receipts prove the GPU path.
    return FirefoxWebRenderResult(True, "passed", compositing, webrender, renderer)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parsed = parser.parse_args(arguments)
    result = classify_report(parsed.report.read_bytes())
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
