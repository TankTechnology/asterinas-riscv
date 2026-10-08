# SPDX-License-Identifier: MPL-2.0
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.riscv.diagnostics.firefox_actor_records import ActorRecords, parse_record


def marker(stage="driver.loaded", request=0):
    return (
        b"A_FF_ACTOR "
        + json.dumps(
            dict(
                version=1, stage=stage, pid=67, request=request, context=0, target_pid=0
            )
        ).encode()
        + b"\r\n"
    )


class ActorRecordsTests(unittest.TestCase):
    def test_real_systemd_forwarder_prefix_preserves_firefox_pid(self):
        records = ActorRecords()
        transcript = b"browser-web-firefox[95]: " + marker()
        self.assertEqual(records.consume(transcript)[0]["pid"], 67)
        self.assertEqual(
            parse_record(transcript.decode().rstrip())["stage"], "driver.loaded"
        )

    def test_non_objects_and_invalid_scalars_are_rejected(self):
        for payload in ("null", "3", "[]", '["version"]', '"secret"'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_record("A_FF_ACTOR " + payload)
        value = json.loads(marker()[11:])
        for key, wrong in (
            ("stage", "secret"),
            ("pid", True),
            ("version", True),
            ("context", 1.5),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_record("A_FF_ACTOR " + json.dumps(dict(value, **{key: wrong})))

    def test_scans_startup_and_partial_lines_once(self):
        records = ActorRecords()
        transcript = b"boot\n" + marker() + marker("server.command", 4)[:-2]
        self.assertEqual(len(records.consume(transcript)), 1)
        self.assertEqual(records.consume(transcript), [])
        self.assertEqual(records.consume(transcript + b"\r\n")[0]["request"], 4)
        self.assertEqual(len(records.records), 2)

    def test_malformed_line_does_not_hide_following_record(self):
        records = ActorRecords()
        self.assertEqual(len(records.consume(b"A_FF_ACTOR null\n" + marker())), 1)
        self.assertEqual(len(records.errors), 1)

    def test_bounded_records_and_errors(self):
        records = ActorRecords(max_records=2, max_errors=1)
        records.consume(marker() * 10)
        self.assertEqual(len(records.records), 2)
        self.assertEqual(len(records.errors), 1)


RUNNER = (
    Path(__file__).parents[3]
    / "target/firefox-diagnostics-20260908/browser_actor_experiment.py"
)
PREPARE = RUNNER.with_name("prepare_actor_experiment.py")


@unittest.skipUnless(RUNNER.exists(), "requires the local experimental runner")
class ActorRunnerTests(unittest.TestCase):
    def test_preparation_hash_validation_survives_python_optimization(self):
        source = f"""import runpy
module=runpy.run_path({str(PREPARE)!r})
try:
    module['verify_installed_archive'](b'wrong installed archive')
except ValueError:
    print('rejected')
"""
        result = subprocess.run(
            [sys.executable, "-O", "-c", source], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "rejected")

    def test_protocol_and_final_drain_keep_all_markers_without_changing_exit(self):
        from tools.riscv import physical_graphics_qemu_gate as gate

        spec = importlib.util.spec_from_file_location("actor_runner_test", RUNNER)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        serial = type(
            "Serial", (), {"transcript": b"browser-web-firefox[95]: " + marker()}
        )()

        def fake_gate_main(_):
            gate._next_line(serial, 0, 0)
            serial.transcript += b"A_FF_ACTOR null\n" + marker("server.command", 4)
            gate.PhysicalGraphicsQemuOperations.drain_serial(
                None, {"serial": serial}, None
            )
            return 1

        with (
            tempfile.TemporaryDirectory() as directory,
            contextlib.ExitStack() as stack,
        ):
            args = [str(RUNNER), "--output-directory", directory]
            for flag in (
                "kernel",
                "uboot",
                "dtb",
                "stage1-initramfs",
                "root-image",
                "root-manifest",
                "packages-lock",
                "package-checksums",
            ):
                args += ["--" + flag, "/unused-test-input"]
            stack.enter_context(patch.object(sys, "argv", args))
            stack.enter_context(patch.object(gate, "main", fake_gate_main))
            stack.enter_context(
                patch.object(gate, "_next_line", lambda *a, **kw: ("ignored", 0))
            )
            for attr in ("physical_graphics_qemu_argv", "physical_cycle_command"):
                stack.enter_context(patch.object(gate, attr, getattr(gate, attr)))
            cls = gate.PhysicalGraphicsQemuOperations
            for attr in ("BOOTARGS", "_run_interaction_cycle"):
                stack.enter_context(patch.object(cls, attr, getattr(cls, attr)))
            stack.enter_context(patch.object(cls, "drain_serial", lambda *a: b""))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(runner.main(), 1)
            output = Path(directory)
            rows = [
                json.loads(line)
                for line in (output / "actor-stages.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [r["stage"] for r in rows], ["driver.loaded", "server.command"]
            )
            self.assertEqual(
                [r["collection_phase"] for r in rows], ["protocol_scan", "final_drain"]
            )
            summary = json.loads((output / "actor-experiment.json").read_text())
            self.assertEqual(summary["markers"], 2)
            self.assertEqual(len(summary["marker_errors"]), 1)
            self.assertFalse(summary["browser_acceptance"])
            previous = (output / "actor-stages.jsonl").read_bytes()
            with self.assertRaises(FileExistsError):
                runner.main()
            self.assertEqual((output / "actor-stages.jsonl").read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
