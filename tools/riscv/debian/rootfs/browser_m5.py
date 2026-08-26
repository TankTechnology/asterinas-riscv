#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Static admission contract for the offline Debian browser workload."""

from __future__ import annotations

import base64
from pathlib import Path


BROWSER_PACKAGE = "firefox-esr"
UNSUPPORTED_BROWSER_PACKAGES = ("chromium",)
PROBE_MARKERS = (
    "ASTERINAS_BROWSER_M5_JS_PASS",
    "ASTERINAS_BROWSER_M5_VIDEO_CANPLAY",
    "ASTERINAS_BROWSER_M5_VIDEO_ENDED",
)
WEBM_EBML_HEADER = bytes.fromhex("1a45dfa3")


def validate_probe_assets(html_path: Path, video_base64_path: Path) -> bytes:
    """Reject a probe that can silently depend on network, audio, or DRM APIs."""

    html = html_path.read_text(encoding="utf-8")
    lowered = html.lower()
    for marker in PROBE_MARKERS:
        if marker not in html:
            raise ValueError(f"missing browser probe marker: {marker}")
    for forbidden in ("http://", "https://", "getusermedia", "encryptedmedia"):
        if forbidden in lowered:
            raise ValueError(f"browser probe has forbidden dependency: {forbidden}")
    if "<audio" in lowered:
        raise ValueError("browser probe must not require audio")
    if 'src="browser-m5.webm"' not in lowered:
        raise ValueError("browser probe must use the local video fixture")

    encoded_video = b"".join(video_base64_path.read_bytes().splitlines())
    try:
        video = base64.b64decode(encoded_video, validate=True)
    except ValueError as error:
        raise ValueError("browser video fixture is not canonical base64") from error
    if not video.startswith(WEBM_EBML_HEADER):
        raise ValueError("browser video fixture is not WebM/EBML")
    if len(video) > 64 * 1024:
        raise ValueError("browser video fixture exceeds the admission size limit")
    return video
