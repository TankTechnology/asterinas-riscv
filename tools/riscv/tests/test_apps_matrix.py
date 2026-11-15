#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""The application matrix manifest and the probe protocol."""

from __future__ import annotations

import unittest

from tools.riscv.apps import manifest, runner


class AppManifestTests(unittest.TestCase):
    def test_names_and_packages_are_unique(self) -> None:
        names = [app.name for app in manifest.APPS]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(names)
        for app in manifest.APPS:
            with self.subTest(app=app.name):
                self.assertTrue(app.package.strip())
                self.assertIn(app.kind, (manifest.CLI, manifest.GUI))

    def test_gui_entries_have_a_window_and_cli_entries_an_expectation(self) -> None:
        for app in manifest.APPS:
            with self.subTest(app=app.name):
                if app.kind == manifest.GUI:
                    self.assertTrue(app.window)
                else:
                    self.assertTrue(app.expect)

    def test_select_preserves_manifest_order(self) -> None:
        selected = manifest.select(["xlogo", "git"])
        self.assertEqual([app.name for app in selected], ["git", "xlogo"])

    def test_select_rejects_unknown_application(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown application"):
            manifest.select(["git", "no-such-app"])

    def test_packages_are_deduplicated_in_order(self) -> None:
        apps = manifest.select(["zip", "git"])
        self.assertEqual(manifest.packages(apps), ["git", "zip", "unzip"])


class ProbeProtocolTests(unittest.TestCase):
    def test_render_tsv_keeps_the_six_fields(self) -> None:
        text = runner.render_tsv(manifest.select(["jq"]))
        fields = text.rstrip("\n").split(runner.FIELD_SEPARATOR)
        self.assertEqual(len(fields), 6)
        self.assertEqual(fields[0], "jq")
        self.assertEqual(fields[1], manifest.CLI)
        # TAB would collapse empty fields in the guest's `read`.
        self.assertNotIn("\t", text)
        self.assertEqual(text.count("\n"), 1)

    def test_parse_results_reads_pass_fail_skip(self) -> None:
        text = (
            "noise\n"
            "APP jq PASS rc=0\n"
            "APP feh FAIL no window matching feh\n"
            "APP xlogo SKIP xdotool is not installed\n"
            "APPS_DONE\n"
        )
        self.assertEqual(
            runner.parse_results(text),
            [
                {"name": "jq", "status": "PASS", "detail": "rc=0"},
                {
                    "name": "feh",
                    "status": "FAIL",
                    "detail": "no window matching feh",
                },
                {
                    "name": "xlogo",
                    "status": "SKIP",
                    "detail": "xdotool is not installed",
                },
            ],
        )

    def test_plan_defaults_to_the_whole_matrix(self) -> None:
        self.assertEqual(runner.build_plan([]), list(manifest.APPS))


if __name__ == "__main__":
    unittest.main()
