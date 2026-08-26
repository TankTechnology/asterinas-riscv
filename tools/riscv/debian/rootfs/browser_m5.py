#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Static admission contract for the offline Debian browser workload."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess


BROWSER_PACKAGE = "firefox-esr"
UNSUPPORTED_BROWSER_PACKAGES = ("chromium",)
PROBE_MARKERS = (
    "ASTERINAS_BROWSER_M5_JS_PASS",
    "ASTERINAS_BROWSER_M5_VIDEO_CANPLAY",
    "ASTERINAS_BROWSER_M5_VIDEO_ENDED",
)
PROBE_RESULT_IDS = (
    "js-result",
    "video-canplay-result",
    "video-ended-result",
)
WEBM_EBML_HEADER = bytes.fromhex("1a45dfa3")


def validate_probe_assets(html_path: Path, video_base64_path: Path) -> bytes:
    """Reject a probe that can silently depend on network, audio, or DRM APIs."""

    html = html_path.read_text(encoding="utf-8")
    lowered = html.lower()
    for marker in PROBE_MARKERS:
        if html.count(marker) != 1:
            raise ValueError(f"missing browser probe marker: {marker}")
    result_positions = []
    for result_id in PROBE_RESULT_IDS:
        declaration = f'id="{result_id}"'
        if html.count(declaration) != 1:
            raise ValueError(f"missing unique browser result node: {result_id}")
        selector = f'document.querySelector("#{result_id}")'
        if html.count(selector) != 1:
            raise ValueError(f"browser result node is not independently addressed: {result_id}")
        result_positions.append(html.index(declaration))
    if result_positions != sorted(result_positions):
        raise ValueError("browser result nodes are not in evidence order")
    marker_writes = (
        'js.textContent = "ASTERINAS_BROWSER_M5_JS_PASS"',
        'canplay.textContent = "ASTERINAS_BROWSER_M5_VIDEO_CANPLAY"',
        'ended.textContent = "ASTERINAS_BROWSER_M5_VIDEO_ENDED"',
    )
    if any(html.count(write) != 1 for write in marker_writes):
        raise ValueError("browser probe markers do not have independent result writes")
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


def probe_video_file(video_path: Path) -> dict[str, object]:
    """Use the media implementation to prove one decodable silent VP8 stream."""

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=format_name,duration:stream=codec_name,codec_type,width,height",
            "-of", "json", str(video_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = json.loads(probe.stdout)
    streams = metadata.get("streams")
    if not isinstance(streams, list) or len(streams) != 1:
        raise ValueError("browser fixture must contain exactly one stream")
    stream = streams[0]
    if not isinstance(stream, dict) or stream.get("codec_type") != "video":
        raise ValueError("browser fixture must contain video and no audio")
    if stream.get("codec_name") != "vp8":
        raise ValueError("browser fixture must use VP8")
    if (stream.get("width"), stream.get("height")) != (160, 90):
        raise ValueError("browser fixture dimensions changed")
    format_data = metadata.get("format")
    if not isinstance(format_data, dict) or "webm" not in str(
        format_data.get("format_name", "")
    ):
        raise ValueError("browser fixture must use WebM")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video_path), "-f", "null", "-"],
        check=True,
        capture_output=True,
    )
    return metadata
