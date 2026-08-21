#!/usr/bin/env python3

# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from nixos_track_audit import (  # noqa: E402
    apply_overrides,
    load_overrides,
    main,
    parse_cherry,
    render_manifest,
)


HASH_A = "a" * 40
HASH_B = "b" * 40
HASH_C = "c" * 40


class TrackAdmissionParserTests(unittest.TestCase):
    def test_parse_cherry_distinguishes_equivalent_and_unique(self) -> None:
        records = parse_cherry(
            "- " + HASH_A + " already landed\n"
            "+ " + HASH_B + " portable tool\n"
        )

        self.assertEqual(records[0].automatic_disposition, "already-main")
        self.assertEqual(records[0].disposition, "already-main")
        self.assertEqual(records[1].automatic_disposition, "unclassified")
        self.assertEqual(records[1].disposition, "unclassified")

    def test_parse_cherry_ignores_only_blank_lines(self) -> None:
        records = parse_cherry(f"\n+ {HASH_A} subject\n   \n")

        self.assertEqual([record.source_commit for record in records], [HASH_A])

    def test_malformed_cherry_line_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid git cherry record"):
            parse_cherry("? not-a-hash subject\n")

    def test_commit_must_be_exact_lowercase_40_hex(self) -> None:
        invalid_commits = (
            "A" * 40,
            "a" * 39,
            "a" * 41,
            "g" * 40,
        )

        for commit in invalid_commits:
            with self.subTest(commit=commit):
                with self.assertRaisesRegex(ValueError, "invalid git cherry record"):
                    parse_cherry(f"+ {commit} subject\n")

    def test_empty_subject_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid git cherry record"):
            parse_cherry(f"+ {HASH_A} \n")

    def test_duplicate_commit_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate commit"):
            parse_cherry(f"+ {HASH_A} first\n- {HASH_A} second\n")

    def test_records_are_frozen(self) -> None:
        record = parse_cherry(f"+ {HASH_A} subject\n")[0]

        with self.assertRaises(FrozenInstanceError):
            record.subject = "changed"


class OverrideTests(unittest.TestCase):
    def _write_json(self, text: str) -> Path:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        path = Path(temporary_directory.name) / "overrides.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_override_supplies_auditable_human_disposition(self) -> None:
        record = parse_cherry(f"+ {HASH_B} portable tool\n")[0]
        classified = apply_overrides(
            [record],
            {
                HASH_B: {
                    "disposition": "portable",
                    "reason": "isolated userspace smoke",
                    "destination": "R0",
                }
            },
        )

        self.assertEqual(classified[0].disposition, "portable")
        self.assertEqual(classified[0].destination, "R0")
        self.assertEqual(classified[0].reason, "isolated userspace smoke")
        self.assertEqual(record.disposition, "unclassified")

    def test_apply_overrides_returns_new_frozen_records(self) -> None:
        record = parse_cherry(f"+ {HASH_B} portable tool\n")[0]

        classified = apply_overrides([record], {})

        self.assertEqual(classified, [record])
        self.assertIsNot(classified[0], record)
        with self.assertRaises(FrozenInstanceError):
            classified[0].reason = "changed"

    def test_override_can_enrich_an_automatic_record(self) -> None:
        record = parse_cherry(f"- {HASH_A} landed\n")[0]

        classified = apply_overrides(
            [record],
            {
                HASH_A: {
                    "reason": "patch-equivalent upstream",
                    "main_equivalent_or_pr": HASH_C,
                    "verification": "git cherry",
                }
            },
        )

        self.assertEqual(classified[0].disposition, "already-main")
        self.assertEqual(classified[0].main_equivalent_or_pr, HASH_C)
        self.assertEqual(classified[0].verification, "git cherry")

    def test_invalid_disposition_is_rejected(self) -> None:
        path = self._write_json(
            json.dumps(
                {
                    HASH_A: {
                        "disposition": "maybe",
                        "reason": "not an admission classification",
                    }
                }
            )
        )

        with self.assertRaisesRegex(ValueError, "invalid disposition"):
            load_overrides(path)

    def test_explicit_classification_requires_nonempty_reason(self) -> None:
        for reason in (None, ""):
            with self.subTest(reason=reason):
                fields = {"disposition": "retire", "destination": "not-applicable"}
                if reason is not None:
                    fields["reason"] = reason
                path = self._write_json(json.dumps({HASH_A: fields}))

                with self.assertRaisesRegex(ValueError, "nonempty reason"):
                    load_overrides(path)

    def test_explicit_classification_requires_nonempty_destination(self) -> None:
        for destination in (None, ""):
            with self.subTest(destination=destination):
                fields = {
                    "disposition": "retire",
                    "reason": "superseded by the upstream implementation",
                }
                if destination is not None:
                    fields["destination"] = destination
                path = self._write_json(json.dumps({HASH_A: fields}))

                with self.assertRaisesRegex(ValueError, "nonempty destination"):
                    load_overrides(path)

    def test_override_hash_must_be_exact_lowercase_40_hex(self) -> None:
        path = self._write_json(json.dumps({"A" * 40: {"reason": "metadata"}}))

        with self.assertRaisesRegex(ValueError, "invalid override commit"):
            load_overrides(path)

    def test_override_document_and_entries_must_be_objects(self) -> None:
        for document in ([], {HASH_A: []}):
            with self.subTest(document=document):
                path = self._write_json(json.dumps(document))
                with self.assertRaisesRegex(ValueError, "JSON object"):
                    load_overrides(path)

    def test_override_fields_are_closed_and_strings(self) -> None:
        documents = (
            ({HASH_A: {"unknown": "value"}}, "unknown override field"),
            ({HASH_A: {"reason": 3}}, "must be a string"),
        )

        for document, message in documents:
            with self.subTest(document=document):
                path = self._write_json(json.dumps(document))
                with self.assertRaisesRegex(ValueError, message):
                    load_overrides(path)

    def test_duplicate_json_commit_is_rejected(self) -> None:
        path = self._write_json(
            f'{{"{HASH_A}": {{"reason": "first"}}, '
            f'"{HASH_A}": {{"reason": "second"}}}}'
        )

        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            load_overrides(path)

    def test_unknown_commit_override_is_rejected(self) -> None:
        records = parse_cherry(f"+ {HASH_A} subject\n")

        with self.assertRaisesRegex(ValueError, "unknown commit"):
            apply_overrides(records, {HASH_B: {"reason": "not present"}})

    def test_override_cannot_contradict_patch_equivalence(self) -> None:
        records = parse_cherry(f"- {HASH_A} landed\n")

        with self.assertRaisesRegex(ValueError, "patch-equivalent"):
            apply_overrides(
                records,
                {
                    HASH_A: {
                        "disposition": "retire",
                        "reason": "superseded",
                        "destination": "not-applicable",
                    }
                },
            )


class ManifestTests(unittest.TestCase):
    def test_manifest_is_deterministic_and_preserves_record_order(self) -> None:
        records = apply_overrides(
            parse_cherry(f"+ {HASH_B} second\n- {HASH_A} first\n"),
            {
                HASH_B: {
                    "disposition": "portable",
                    "reason": "independent",
                    "destination": "R0",
                    "subsystem": "userspace",
                    "main_equivalent_or_pr": "PR #63",
                    "verification": "smoke test",
                }
            },
        )

        first = render_manifest(HASH_A, HASH_C, records)
        second = render_manifest(HASH_A, HASH_C, records)

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["base"], HASH_A)
        self.assertEqual(first["track"], HASH_C)
        self.assertEqual(first["counts"], {"already-main": 1, "portable": 1})
        self.assertEqual(
            [entry["source_commit"] for entry in first["records"]],
            [HASH_B, HASH_A],
        )
        self.assertEqual(first["records"][0]["classification"], "portable")
        self.assertEqual(first["records"][0]["subsystem"], "userspace")
        self.assertNotIn("disposition", first["records"][0])

    def test_manifest_renders_unclassified_records_honestly(self) -> None:
        manifest = render_manifest(
            HASH_A,
            HASH_B,
            parse_cherry(f"+ {HASH_C} pending review\n"),
        )

        self.assertEqual(manifest["counts"], {"unclassified": 1})
        self.assertEqual(
            manifest["records"][0]["classification"], "unclassified"
        )

    def test_manifest_requires_exact_commit_ids(self) -> None:
        records = parse_cherry(f"+ {HASH_C} subject\n")

        for base, track in (("main", HASH_B), (HASH_A, "B" * 40)):
            with self.subTest(base=base, track=track):
                with self.assertRaisesRegex(ValueError, "40-character"):
                    render_manifest(base, track, records)

    def test_manifest_rejects_duplicate_records(self) -> None:
        record = parse_cherry(f"+ {HASH_C} subject\n")[0]

        with self.assertRaisesRegex(ValueError, "duplicate commit"):
            render_manifest(HASH_A, HASH_B, [record, record])


class CliTests(unittest.TestCase):
    def _arguments(self, directory: Path) -> list[str]:
        overrides = directory / "overrides.json"
        overrides.write_text("{}\n", encoding="utf-8")
        return [
            "--base",
            HASH_A,
            "--track",
            HASH_B,
            "--overrides",
            str(overrides),
            "--output",
            str(directory / "manifest.json"),
        ]

    @mock.patch("nixos_track_audit.subprocess.run")
    def test_cli_requires_exact_lowercase_commit_ids_before_running_git(
        self, run: mock.Mock
    ) -> None:
        invalid_values = ("main", "a" * 12, "A" * 40)

        for option in ("--base", "--track"):
            for invalid_value in invalid_values:
                with self.subTest(option=option, invalid_value=invalid_value):
                    with tempfile.TemporaryDirectory() as directory_name:
                        directory = Path(directory_name)
                        arguments = self._arguments(directory)
                        arguments[arguments.index(option) + 1] = invalid_value

                        with mock.patch("sys.stderr") as stderr:
                            result = main(arguments)

                        error_text = "".join(
                            call.args[0] for call in stderr.write.call_args_list
                        )
                        self.assertFalse((directory / "manifest.json").exists())

                    self.assertEqual(result, 1)
                    self.assertIn(
                        f"{option[2:]} must be a lowercase 40-character hex commit ID",
                        error_text,
                    )
                    run.assert_not_called()
                    run.reset_mock()

    @mock.patch("nixos_track_audit.subprocess.run")
    def test_cli_validates_commits_runs_cherry_and_writes_manifest(
        self, run: mock.Mock
    ) -> None:
        run.side_effect = (
            subprocess.CompletedProcess([], 0, stdout=HASH_A + "\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=HASH_B + "\n", stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout=f"- {HASH_C} already present\n", stderr=""
            ),
        )
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            arguments = self._arguments(directory)

            with mock.patch("sys.stdout") as stdout:
                result = main(arguments)

            manifest = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8")
            )
            output_text = "".join(call.args[0] for call in stdout.write.call_args_list)

        self.assertEqual(result, 0)
        self.assertEqual(manifest["base"], HASH_A)
        self.assertEqual(manifest["track"], HASH_B)
        self.assertIn("already-main: 1", output_text)
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    [
                        "git",
                        "rev-parse",
                        "--verify",
                        "--end-of-options",
                        f"{HASH_A}^{{commit}}",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ),
                mock.call(
                    [
                        "git",
                        "rev-parse",
                        "--verify",
                        "--end-of-options",
                        f"{HASH_B}^{{commit}}",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ),
                mock.call(
                    ["git", "cherry", "-v", HASH_A, HASH_B],
                    check=True,
                    capture_output=True,
                    text=True,
                ),
            ],
        )

    @mock.patch("nixos_track_audit.subprocess.run")
    def test_cli_reports_missing_ref_without_publishing_output(
        self, run: mock.Mock
    ) -> None:
        run.side_effect = subprocess.CalledProcessError(
            128,
            ["git", "rev-parse"],
            stderr="fatal: Needed a single revision\n",
        )
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            output = directory / "manifest.json"

            with mock.patch("sys.stderr") as stderr:
                result = main(self._arguments(directory))

            error_text = "".join(call.args[0] for call in stderr.write.call_args_list)
            self.assertFalse(output.exists())

        self.assertNotEqual(result, 0)
        self.assertIn("git rev-parse failed", error_text)

    @mock.patch("nixos_track_audit.subprocess.run")
    def test_cli_rejects_malformed_cherry_without_replacing_output(
        self, run: mock.Mock
    ) -> None:
        run.side_effect = (
            subprocess.CompletedProcess([], 0, stdout=HASH_A + "\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=HASH_B + "\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="malformed\n", stderr=""),
        )
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            output = directory / "manifest.json"
            output.write_text("original\n", encoding="utf-8")

            with mock.patch("sys.stderr") as stderr:
                result = main(self._arguments(directory))

            error_text = "".join(call.args[0] for call in stderr.write.call_args_list)
            self.assertEqual(output.read_text(encoding="utf-8"), "original\n")

        self.assertNotEqual(result, 0)
        self.assertIn("invalid git cherry record", error_text)

    @mock.patch("nixos_track_audit.os.replace")
    @mock.patch("nixos_track_audit.subprocess.run")
    def test_cli_cleans_temporary_output_when_atomic_replace_fails(
        self, run: mock.Mock, replace: mock.Mock
    ) -> None:
        run.side_effect = (
            subprocess.CompletedProcess([], 0, stdout=HASH_A + "\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=HASH_B + "\n", stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout=f"- {HASH_C} already present\n", stderr=""
            ),
        )
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            output = directory / "manifest.json"
            output.write_text("original\n", encoding="utf-8")

            def fail_replace(source: Path, destination: Path) -> None:
                self.assertTrue(Path(source).is_file())
                self.assertEqual(Path(destination), output)
                self.assertTrue(
                    Path(source).read_text(encoding="utf-8").endswith("\n")
                )
                raise OSError("atomic replace denied")

            replace.side_effect = fail_replace

            with mock.patch("sys.stderr") as stderr:
                result = main(self._arguments(directory))

            error_text = "".join(call.args[0] for call in stderr.write.call_args_list)
            temporary_outputs = list(directory.glob(".manifest.json.*"))
            self.assertEqual(output.read_text(encoding="utf-8"), "original\n")

        self.assertNotEqual(result, 0)
        self.assertIn("atomic replace denied", error_text)
        self.assertEqual(temporary_outputs, [])
        replace.assert_called_once()


if __name__ == "__main__":
    unittest.main()
