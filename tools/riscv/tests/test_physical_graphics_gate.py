#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the deterministic physical graphics interaction witness."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOTFS_DIRECTORY = Path(__file__).parents[1] / "debian" / "rootfs"
PAGE = ROOTFS_DIRECTORY / "physical_graphics_interaction.html"


class PhysicalGraphicsPageTests(unittest.TestCase):
    def _page(self) -> str:
        self.assertTrue(PAGE.is_file(), "physical graphics page is missing")
        return PAGE.read_text(encoding="utf-8")

    def test_page_exposes_the_exact_interaction_surface(self) -> None:
        page = self._page()
        for element_id in (
            "interaction-nonce",
            "interaction-button",
            "interaction-state",
            "interaction-instructions",
        ):
            self.assertIn(f'id="{element_id}"', page)
        self.assertIn("window.__asterinasPhysicalGraphicsSnapshot", page)

    def test_page_records_only_trusted_physical_events(self) -> None:
        page = self._page()
        for event_name in ("keydown", "input", "pointermove", "click"):
            self.assertIn(f'addEventListener("{event_name}"', page)
        self.assertGreaterEqual(page.count("event.isTrusted"), 4)
        self.assertIn('button.disabled = true', page)

    def test_page_is_self_contained_and_uses_the_frozen_state_names(self) -> None:
        page = self._page()
        self.assertNotIn("<script src=", page)
        self.assertNotIn("<link rel=", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        for state in (
            'trustedKey: false',
            'trustedInput: false',
            'trustedPointer: false',
            'trustedClick: false',
            'clickCount: 0',
            'color: "amber"',
        ):
            self.assertIn(state, page)


if __name__ == "__main__":
    unittest.main()
