#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Static contract for the bounded QEMU TCG boot tracer."""

from __future__ import annotations

from pathlib import Path
import unittest


PLUGIN = Path(__file__).resolve().parents[1] / "qemu_plugins/eic7700_boot_trace.c"


class QemuTcgPluginTests(unittest.TestCase):
    def test_plugin_registers_execution_and_idle_boundaries(self) -> None:
        source = PLUGIN.read_text(encoding="utf-8")
        for marker in (
            "qemu_plugin_register_vcpu_tb_trans_cb",
            "qemu_plugin_register_vcpu_idle_cb",
            "qemu_plugin_register_vcpu_resume_cb",
            "qemu_plugin_register_atexit_cb",
            "ASTERINAS_QEMU_TCG_TRACE",
        ):
            self.assertIn(marker, source)

    def test_plugin_reports_aggregate_counts_without_guest_memory_claims(self) -> None:
        source = PLUGIN.read_text(encoding="utf-8")
        self.assertIn("qemu_plugin_tb_n_insns", source)
        self.assertIn("__atomic_fetch_add", source)
        self.assertIn("ASTERINAS_QEMU_TCG_TRACE_OUTPUT", source)
        self.assertIn("fopen", source)
        self.assertIn("ASTERINAS_QEMU_TCG_TRACE_FILE", source)
        self.assertNotIn("qemu_plugin_read_memory", source)
        self.assertNotIn("physical", source.lower())


if __name__ == "__main__":
    unittest.main()
