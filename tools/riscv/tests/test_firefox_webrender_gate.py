#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from drm.firefox_webrender_gate import MAX_REPORT_BYTES, classify_report  # noqa: E402


class FirefoxWebRenderGateTests(unittest.TestCase):
    def test_accepts_virgl_webrender_report(self) -> None:
        result = classify_report(
            "Compositing: WebRender\nWebRender: available\nDriver Renderer: virgl (Mesa 25.0)\n"
        )
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.gpu_renderer, "virgl (Mesa 25.0)")

    def test_accepts_firefox_gpu_numbered_driver_renderer_field(self) -> None:
        result = classify_report(
            "Compositing: WebRender\nWebRender: available\n"
            "GPU #1 Driver Renderer: virgl (Mesa 25.0)\n"
        )
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.gpu_renderer, "virgl (Mesa 25.0)")

    def test_accepts_generic_renderer_after_firefox_sanitization(self) -> None:
        result = classify_report(
            "Compositing: WebRender\nWebRender: available\n"
            "GPU #1 Driver Renderer: Generic Renderer\n"
        )
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.gpu_renderer, "Generic Renderer")

    def test_rejects_llvmpipe_even_when_compositing_is_webrender(self) -> None:
        result = classify_report(
            "Compositing: WebRender\nWebRender: available\nDriver Renderer: llvmpipe (LLVM 20)\n"
        )
        self.assertFalse(result.passed)
        self.assertIn("software", result.reason)

    def test_rejects_unavailable_webrender_even_with_virgl(self) -> None:
        result = classify_report(
            "Compositing: WebRender\nWebRender: unavailable\n"
            "Driver Renderer: virgl (Mesa 25.0)\n"
        )
        self.assertFalse(result.passed)
        self.assertIn("unavailable", result.reason)

    def test_rejects_swgl_and_non_webrender_compositors(self) -> None:
        swgl = classify_report(
            "Compositing: WebRender\nWebRender: SWGL\nRenderer: virgl\nRenderCompositorSWGL\n"
        )
        self.assertFalse(swgl.passed)
        software = classify_report(
            "Compositing: Basic\nWebRender: unavailable\nDriver Renderer: virgl\n"
        )
        self.assertFalse(software.passed)

    def test_rejects_missing_fields_and_oversized_reports(self) -> None:
        self.assertFalse(classify_report("Driver Renderer: virgl\n").passed)
        with self.assertRaises(ValueError):
            classify_report(b"x" * (MAX_REPORT_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
