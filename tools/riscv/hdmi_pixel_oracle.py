#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded comparison of a Firefox witness and independent HDMI pixels."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


SAMPLE_WIDTH = 96
SAMPLE_HEIGHT = 54
MAX_IMAGE_BYTES = 64 * 1024 * 1024


class PixelMismatch(RuntimeError):
    """The capture cannot prove that the expected pixels reached HDMI."""


@dataclass(frozen=True)
class PixelComparison:
    matched_fraction: float
    mean_absolute_error: float


def _run_decoder(program: str, arguments: list[str], payload: bytes) -> bytes:
    try:
        result = subprocess.run(
            [program, "-v", "error", *arguments],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PixelMismatch(f"{program} could not decode HDMI evidence") from error
    if result.returncode != 0:
        raise PixelMismatch(f"{program} rejected HDMI evidence")
    return result.stdout


def _sample_pixels(payload: bytes, dimensions: tuple[int, int]) -> bytes:
    if not 8 < len(payload) <= MAX_IMAGE_BYTES:
        raise PixelMismatch("image size exceeds HDMI comparison contract")
    geometry = _run_decoder(
        "ffprobe",
        [
            "-f",
            "image2pipe",
            "-i",
            "pipe:0",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
        ],
        payload,
    )
    if geometry != f"{dimensions[0]}x{dimensions[1]}\n".encode():
        raise PixelMismatch("HDMI and Firefox image geometry differ")
    pixels = _run_decoder(
        "ffmpeg",
        [
            "-nostdin",
            "-threads",
            "1",
            "-f",
            "image2pipe",
            "-i",
            "pipe:0",
            "-frames:v",
            "1",
            "-vf",
            f"scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT}:flags=area",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        payload,
    )
    if len(pixels) != SAMPLE_WIDTH * SAMPLE_HEIGHT * 3:
        raise PixelMismatch("HDMI pixel sample is incomplete")
    return pixels


def compare_hdmi_pixels(
    firefox_png: bytes,
    hdmi_image: bytes,
    *,
    expected_dimensions: tuple[int, int] = (1920, 1080),
) -> PixelComparison:
    """Require a nonblank expected frame and a matching external HDMI image.

    This gate intentionally requires full-resolution capture with the same
    orientation. A rescaled or cropped capture cannot certify the native mode.
    """

    if (
        len(expected_dimensions) != 2
        or any(type(value) is not int or value <= 0 for value in expected_dimensions)
    ):
        raise ValueError("expected display dimensions are invalid")
    reference = _sample_pixels(firefox_png, expected_dimensions)
    captured = _sample_pixels(hdmi_image, expected_dimensions)
    reference_pixels = [
        reference[index : index + 3] for index in range(0, len(reference), 3)
    ]
    cyan = sum(
        red < 100 and green > 130 and blue > 120
        for red, green, blue in reference_pixels
    )
    dark = sum(max(pixel) < 70 for pixel in reference_pixels)
    if cyan < len(reference_pixels) // 100 or dark < len(reference_pixels) // 10:
        raise PixelMismatch("Firefox witness is blank or lacks the expected cyan page")

    differences = [abs(left - right) for left, right in zip(reference, captured)]
    matched = sum(
        max(differences[index : index + 3]) <= 32
        for index in range(0, len(differences), 3)
    )
    evidence = PixelComparison(
        matched_fraction=matched / len(reference_pixels),
        mean_absolute_error=sum(differences) / len(differences),
    )
    if evidence.matched_fraction < 0.95 or evidence.mean_absolute_error > 12:
        raise PixelMismatch(
            "HDMI pixels differ from the Firefox witness: "
            f"matched={evidence.matched_fraction:.3f} "
            f"mae={evidence.mean_absolute_error:.2f}"
        )
    return evidence
