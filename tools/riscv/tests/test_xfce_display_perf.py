#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path


XFCE_TOOLS = Path(__file__).resolve().parents[1] / "xfce"
sys.path.insert(0, str(XFCE_TOOLS))

from display_perf_matrix import (  # noqa: E402
    _input_digest,
    parse_benchmark,
    rotated_paths,
    summarize_results,
)
from boot_xfce_drm import DisplayPath, positive_seconds, renderer_is_ready  # noqa: E402


def benchmark_transcript(
    *, renderer: str = "virgl", direct: str = "yes", values: tuple[float, ...]
) -> bytes:
    frames = len(values)
    elapsed_ms = sum(values)
    sorted_values = sorted(values)

    def nearest_rank(percentile: int) -> float:
        rank = (percentile * frames + 99) // 100
        return sorted_values[rank - 1]

    lines = [
        f"XFCE_GL_DIRECT {direct}",
        f"XFCE_GL_RENDERER {renderer}",
        "XFCE_GL_PIXEL 32,128,223,255",
        f"XFCE_GL_BENCH frames={frames} elapsed_ms={elapsed_ms:.3f} "
        f"fps={frames * 1000.0 / elapsed_ms:.3f}",
    ]
    lines.extend(
        f"XFCE_GL_FRAME index={index} elapsed_ms={value:.3f}"
        for index, value in enumerate(values)
    )
    cpu_ms = frames * 2.0
    lines.extend(
        (
            f"XFCE_GL_FRAME_TIMES frames={frames} "
            f"mean_ms={sum(values) / frames:.3f} "
            f"p50_ms={nearest_rank(50):.3f} "
            f"p95_ms={nearest_rank(95):.3f} "
            f"p99_ms={nearest_rank(99):.3f} "
            f"max_ms={max(values):.3f} cpu_ms={cpu_ms:.3f} "
            f"cpu_ms_per_frame={cpu_ms / frames:.3f}",
            "XFCE_GL_BENCH_PASS",
        )
    )
    return ("\n".join(lines) + "\n").encode()


class BenchmarkParserTests(unittest.TestCase):
    def test_parses_and_recomputes_frame_percentiles(self) -> None:
        values = tuple(float(value) for value in range(1, 21))
        sample = parse_benchmark(benchmark_transcript(values=values))

        self.assertEqual(sample.renderer, "virgl")
        self.assertTrue(sample.is_direct)
        self.assertEqual(sample.frames, 20)
        self.assertEqual(sample.p50_ms, 10.0)
        self.assertEqual(sample.p95_ms, 19.0)
        self.assertEqual(sample.p99_ms, 20.0)
        self.assertEqual(sample.cpu_ms_per_frame, 2.0)

    def test_accepts_serial_crlf_records(self) -> None:
        transcript = benchmark_transcript(values=(1.0, 2.0, 3.0))
        sample = parse_benchmark(transcript.replace(b"\n", b"\r\n"))

        self.assertEqual(sample.renderer, "virgl")
        self.assertEqual(sample.p95_ms, 3.0)

    def test_rejects_duplicate_pass_and_missing_frame_records(self) -> None:
        transcript = benchmark_transcript(values=(1.0, 2.0, 3.0))
        with self.assertRaisesRegex(ValueError, "one benchmark pass"):
            parse_benchmark(transcript + b"XFCE_GL_BENCH_PASS\n")
        with self.assertRaisesRegex(ValueError, "missing, duplicated, or reordered"):
            parse_benchmark(
                transcript.replace(b"XFCE_GL_FRAME index=1 elapsed_ms=2.000\n", b"")
            )

    def test_rejects_summary_that_disagrees_with_raw_frames(self) -> None:
        transcript = benchmark_transcript(values=(1.0, 2.0, 3.0))
        with self.assertRaisesRegex(ValueError, "p95 mismatch"):
            parse_benchmark(transcript.replace(b"p95_ms=3.000", b"p95_ms=2.000"))

    def test_rejects_invalid_validation_pixel(self) -> None:
        transcript = benchmark_transcript(values=(1.0, 2.0, 3.0))
        with self.assertRaisesRegex(ValueError, "validation pixel mismatch"):
            parse_benchmark(
                transcript.replace(
                    b"XFCE_GL_PIXEL 32,128,223,255", b"XFCE_GL_PIXEL 0,0,0,0"
                )
            )


class MatrixPolicyTests(unittest.TestCase):
    def test_rotates_path_order_between_rounds(self) -> None:
        paths = ("virgl", "software-drm", "fbdev")
        self.assertEqual(rotated_paths(paths, 0), list(paths))
        self.assertEqual(
            rotated_paths(paths, 1), ["software-drm", "fbdev", "virgl"]
        )
        self.assertEqual(rotated_paths(paths, 2), ["fbdev", "virgl", "software-drm"])

    def test_requires_complete_runs_and_two_x_p95_speedup(self) -> None:
        def result(display_path: str, p95_ms: float, fps: float) -> dict[str, object]:
            return {
                "display_path": display_path,
                "passed": True,
                "sample": {
                    "fps": fps,
                    "mean_ms": p95_ms / 2,
                    "p50_ms": p95_ms / 2,
                    "p95_ms": p95_ms,
                    "p99_ms": p95_ms,
                    "max_ms": p95_ms,
                    "cpu_ms_per_frame": 1.0,
                },
            }

        results = [
            result("software-drm", 20.0, 50.0),
            result("virgl", 8.0, 125.0),
        ]
        summary = summarize_results(
            results, ("software-drm", "virgl"), rounds=1, minimum_p95_speedup=2.0
        )
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["p95_speedup_software_over_virgl"], 2.5)

        incomplete = summarize_results(
            results[:1], ("software-drm", "virgl"), rounds=1, minimum_p95_speedup=2.0
        )
        self.assertFalse(incomplete["complete"])
        self.assertFalse(incomplete["passed"])


class DisplayGateTests(unittest.TestCase):
    def test_boot_durations_must_be_finite_and_positive(self) -> None:
        self.assertEqual(positive_seconds("1.5"), 1.5)
        for invalid in ("0", "-1", "nan", "inf", "-inf", "not-a-number"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(argparse.ArgumentTypeError):
                    positive_seconds(invalid)

    def test_dri3_is_required_only_for_virgl(self) -> None:
        common = b"XFCE_GL_BENCH_PASS XFCE_GL_DIRECT yes "
        software = common + b"XFCE_GL_RENDERER llvmpipe"
        virgl = common + b"XFCE_GL_RENDERER virgl"

        self.assertTrue(renderer_is_ready(DisplayPath.SOFTWARE_DRM, software))
        self.assertFalse(renderer_is_ready(DisplayPath.VIRGL, virgl))
        self.assertTrue(
            renderer_is_ready(
                DisplayPath.VIRGL, virgl + b" Using DRI3 for screen 0"
            )
        )


class InputDigestTests(unittest.TestCase):
    def test_detects_same_size_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            payload = root / "payload"
            payload.write_bytes(b"first")
            first_digest = _input_digest(root)

            payload.write_bytes(b"other")
            self.assertNotEqual(_input_digest(root), first_digest)


if __name__ == "__main__":
    unittest.main()
