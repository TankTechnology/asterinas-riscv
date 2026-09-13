# SPDX-License-Identifier: MPL-2.0

import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

from tools.riscv.diagnostics import firefox_dmesg_guest as guest_dmesg
from tools.riscv.diagnostics.firefox_dmesg_guest import BoundedDmesg
from tools.riscv.diagnostics.firefox_dmesg_records import DmesgFrames
from tools.riscv.diagnostics import firefox_dmesg_experiment as experiment


class FakeOutput:
    def __init__(self):
        self.items = queue.Queue()

    def feed(self, value):
        self.items.put(value)

    def close(self):
        self.items.put(None)

    def read(self, _size):
        value = self.items.get(timeout=2)
        return b"" if value is None else value


class FakeProcess:
    def __init__(self, *, ignore_term=False):
        self.pid = 123
        self.stdout = FakeOutput()
        self.returncode = None
        self.ignore_term = ignore_term
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if not self.ignore_term:
            self.returncode = -15
            self.stdout.close()

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self.stdout.close()

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("dmesg", timeout)
        return self.returncode

    def exit_early(self, status=7):
        self.returncode = status
        self.stdout.close()


class CollectorHarness:
    def __init__(self, *, limit=4 * 1024 * 1024, ignore_term=False):
        self.process = FakeProcess(ignore_term=ignore_term)
        self.lines = []
        self.writes = []

        def write(_fd, message):
            self.writes.append(message)
            payload = message.removeprefix(b"<14>")
            raw = b"<14>[    1.000000] " + payload + b"\n"
            self.process.stdout.feed(raw[:17])
            self.process.stdout.feed(raw[17:])
            return len(message)

        self.collector = BoundedDmesg(
            firefox_pid=70,
            client_pid=252,
            limit=limit,
            process_factory=lambda *args, **kwargs: self.process,
            open_kmsg=lambda: 9,
            write_kmsg=write,
            close_kmsg=lambda _fd: None,
            emit=self.lines.append,
            ready_timeout=1,
            cleanup_timeout=0.01,
        )

    def start(self):
        self.collector.start(["/usr/bin/dmesg", "--follow-new", "--raw"])


class BoundedDmesgTests(unittest.TestCase):
    def test_pipe_drain_prefers_one_available_buffered_read(self):
        class BufferedPipe:
            def read1(self, size):
                self.size = size
                return b"available"

            def read(self, _size):
                raise AssertionError("BufferedReader.read may wait for the full size")

        stream = BufferedPipe()
        self.assertEqual(guest_dmesg._read_available(stream, 65536), b"available")
        self.assertEqual(stream.size, 65536)

    def test_partial_lines_are_drained_and_emit_one_valid_sequence(self):
        harness = CollectorHarness()
        harness.start()
        harness.process.stdout.feed(b"<6>[    2.0] partial")
        harness.process.stdout.feed(b" line\n")
        harness.collector.set_request(4)
        harness.collector.mark("request_enter")
        lines = harness.collector.stop_and_emit()
        self.assertEqual(lines, harness.lines)
        self.assertEqual(sum(line.startswith("A_FF_DMESG_META ") for line in lines), 1)
        receiver = DmesgFrames()
        completed = [
            value for line in lines if (value := receiver.accept(line)) is not None
        ]
        receiver.finish()
        self.assertEqual(completed, [receiver.payload])
        self.assertIn(b"partial line\n", receiver.payload)
        self.assertEqual(harness.process.terminate_calls, 1)
        self.assertEqual(harness.process.kill_calls, 0)

    def test_over_limit_keeps_draining_with_exact_discard_counts(self):
        harness = CollectorHarness()
        harness.start()
        payload = b"x" * (4 * 1024 * 1024 + 3) + b"\n"
        harness.process.stdout.feed(payload)
        deadline = time.monotonic() + 2
        while (
            harness.collector.raw_bytes < len(payload) and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        harness.collector.stop_and_emit()
        meta = json.loads(harness.lines[0].removeprefix("A_FF_DMESG_META "))
        self.assertEqual(meta["raw_bytes"], harness.collector.raw_bytes)
        self.assertEqual(meta["retained_bytes"], 4 * 1024 * 1024)
        self.assertEqual(meta["discarded_bytes"], meta["raw_bytes"] - 4 * 1024 * 1024)
        self.assertGreaterEqual(meta["discarded_lines"], 1)

    def test_early_exit_is_reported_and_fail_closed(self):
        harness = CollectorHarness()
        harness.start()
        harness.process.exit_early()
        deadline = time.monotonic() + 1
        while not harness.collector.early_exit and time.monotonic() < deadline:
            time.sleep(0.001)
        harness.collector.stop_and_emit()
        receiver = DmesgFrames()
        for line in harness.lines:
            receiver.accept(line)
        with self.assertRaisesRegex(ValueError, "before targeted shutdown"):
            receiver.finish()

    def test_ignored_sigterm_gets_one_targeted_kill_and_finite_cleanup(self):
        harness = CollectorHarness(ignore_term=True)
        harness.start()
        started = time.monotonic()
        harness.collector.stop_and_emit()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(harness.process.terminate_calls, 1)
        self.assertEqual(harness.process.kill_calls, 1)
        self.assertEqual(harness.process.returncode, -9)


class RunnerConstructionTests(unittest.TestCase):
    def test_installed_diagnostic_sources_must_match_the_generated_sources(self):
        generated = "print('driver')\n"
        helper = experiment.GUEST_HELPER.read_bytes()
        outputs = [generated.encode(), helper]

        def run(_arguments, **_kwargs):
            return SimpleNamespace(returncode=0, stdout=outputs.pop(0))

        hashes = experiment.verify_installed_diagnostics(
            Path("root.ext2"), generated, run=run
        )
        self.assertEqual(
            hashes["guest_source_sha256"],
            hashlib.sha256(generated.encode()).hexdigest(),
        )

        outputs[:] = [b"wrong", helper]
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            experiment.verify_installed_diagnostics(
                Path("root.ext2"), generated, run=run
            )

    def test_preparer_installs_the_matching_snapshot_parser(self):
        preparer = (
            Path(experiment.__file__).with_name("firefox_dmesg_prepare.py").read_text()
        )
        self.assertIn("firefox_diagnostic_snapshot.py", preparer)
        self.assertIn(
            'destination": "/usr/lib/asterinas/firefox-diagnostic-snapshot"', preparer
        )

    def test_pinned_baseline_and_exact_transform_anchors(self):
        baseline = experiment.load_baseline(experiment.DEFAULT_BASELINE)
        source = experiment.build_guest_source(baseline)
        for marker in (
            "BoundedDmesg",
            "snapshot_before_start",
            "snapshot_during_start",
            "snapshot_after_start",
            "request_selected",
            "request_enter",
            "request_return",
            "request_error",
            "collector_stopping",
            "client.set_timeout(300.0)",
        ):
            self.assertIn(marker, source)
        self.assertIn("lambda:spawn('during',1)", source)
        self.assertNotIn("lambda:spawn('during',30)", source)
        self.assertIn("str(float(delay)+45)", source)
        self.assertIn("worker.wait(timeout=50)", source)
        terminal_hook = (
            "module['main'].__globals__['print'] = terminal_print\n"
            "exit_code = 1\n"
            "try:\n"
            "    exit_code = module['main']()"
        )
        self.assertIn(terminal_hook, source)
        self.assertIn(
            "if line.startswith('ASTERINAS_PHYSICAL_GRAPHICS_FAIL '):\n"
            "        export_collector()\n"
            "    return original_print(*args, **kwargs)",
            source,
        )
        compile(source, "<firefox-dmesg-driver>", "exec")
        with self.assertRaisesRegex(ValueError, "exactly once"):
            experiment.replace_exact("anchor anchor", "anchor", "changed")

    def test_changed_baseline_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "baseline.py"
            changed.write_text("changed")
            with self.assertRaisesRegex(ValueError, "hash"):
                experiment.load_baseline(changed)

    def test_generated_serial_command_is_bounded_and_has_one_replacement(self):
        from tools.riscv.megrez_physical_graphics import physical_cycle_command

        baseline = experiment.load_baseline(experiment.DEFAULT_BASELINE)
        original = physical_cycle_command(
            1,
            "0123456789abcdef",
            300,
            expected_browser_pid=70,
            expected_width=1280,
            expected_height=1024,
            setup_timeout=600,
        )
        command = experiment.build_serial_command(original, baseline)
        self.assertLessEqual(len(command.encode()), 4000)
        self.assertEqual(command.count("python3 -c"), 1)
        with self.assertRaisesRegex(RuntimeError, "contract"):
            experiment.build_serial_command(
                original.replace("physical-graphics-gate", "other"), baseline
            )

    def test_boot_arguments_are_exact_and_idempotence_is_rejected(self):
        value = "root=x loglevel=off -- --root-init=systemd"
        transformed = experiment.diagnostic_bootargs(value)
        self.assertIn("asterinas.klog_capture=info", transformed)
        self.assertIn("asterinas.syscall_diag=1", transformed)
        self.assertIn("asterinas.tcp_diagnostic_port=2828", transformed)
        self.assertIn(
            "systemd.setenv=ASTERINAS_FIREFOX_ACTOR_DIAGNOSTICS=1", transformed
        )
        self.assertNotIn("MOZ_PROFILER_STARTUP", transformed)
        with self.assertRaises(RuntimeError):
            experiment.diagnostic_bootargs(transformed)

    def test_nonempty_output_is_never_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            experiment.prepare_output(output)
            (output / "evidence").write_text("keep")
            with self.assertRaises(FileExistsError):
                experiment.prepare_output(output)

    def test_fake_complete_run_publishes_immutable_diagnostic_artifacts(self):
        from tools.riscv import physical_graphics_qemu_gate as gate

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            baseline = experiment.load_baseline(experiment.DEFAULT_BASELINE)
            marker_phases = (
                ("collector_started", 0),
                ("request_selected", 4),
                ("snapshot_before_start", 4),
                ("snapshot_before_end", 4),
                ("request_enter", 4),
                ("snapshot_during_start", 4),
                ("snapshot_during_end", 4),
                ("request_error", 4),
                ("snapshot_after_start", 4),
                ("snapshot_after_end", 4),
                ("collector_stopping", 4),
            )
            raw = "".join(
                "<14>[  1.000000] ASTERINAS_FF_KLOG version=1 "
                f"phase={phase} request_id={request_id} "
                "firefox_pid=70 client_pid=252\n"
                for phase, request_id in marker_phases
            ).encode()
            encoded = base64.b64encode(zlib.compress(raw)).decode()
            digest = hashlib.sha256(raw).hexdigest()
            meta = {
                "version": 1,
                "raw_bytes": len(raw),
                "retained_bytes": len(raw),
                "discarded_bytes": 0,
                "discarded_lines": 0,
                "early_exit": False,
                "returncode": -15,
            }
            lines = [
                "A_FF_CHECKPOINT "
                + json.dumps(
                    {
                        "event": "command_selected",
                        "transport_budget_seconds": 300,
                    }
                ),
                "A_FF_CHECKPOINT "
                + json.dumps(
                    {
                        "event": "command_enter",
                        "transport_budget_seconds": 300,
                    }
                ),
                "A_WEB_MARIONETTE_TRANSPORT "
                + json.dumps(
                    {
                        "version": 1,
                        "event": "send_complete",
                        "pid": 252,
                        "monotonic_ns": 7,
                        "request_id": 4,
                        "command": "WebDriver:ExecuteScript",
                        "stage": "response_header",
                        "send_complete": True,
                        "header_bytes": 0,
                        "body_expected": None,
                        "body_received": 0,
                    }
                ),
                "A_FF_TRANSPORT "
                + json.dumps(
                    {
                        "version": 1,
                        "stage": "packet.dispatch",
                        "pid": 70,
                        "sequence": 1,
                        "available": 0,
                        "header": 0,
                        "expected": 0,
                        "received": 0,
                        "request": 4,
                    }
                ),
                "A_FF_DMESG_META " + json.dumps(meta, separators=(",", ":")),
                f"A_FF_DMESG part=0/1 sha256={digest} data={encoded}",
            ]
            serial = SimpleNamespace(transcript=("\n".join(lines) + "\n").encode())

            identity = {"pid": 70, "ppid": 1, "start_time_ticks": 100}

            def snapshot(phase):
                return {
                    "version": 1,
                    "phase": phase,
                    "tree": {
                        "root_pid": 70,
                        "root_identity": identity,
                        "limitations": [],
                        "processes": [{"pid": 70, "threads": []}],
                    },
                }

            def fake_main():
                for phase in ("before", "during", "after"):
                    (output / f"snapshot-{phase}.json").write_text(
                        json.dumps(snapshot(phase))
                    )
                gate._next_line(serial, 0, 1)
                gate.PhysicalGraphicsQemuOperations.drain_serial(
                    None, {"serial": serial}, SimpleNamespace()
                )
                (output / "result.json").write_text(
                    json.dumps({"input_sha256": {"kernel": "a" * 64}})
                )
                return 1

            baseline.main = fake_main
            config = SimpleNamespace(
                output_directory=output,
                root_image=Path(directory) / "diagnostic-root.ext2",
            )

            def original_next(*_args, **_kwargs):
                return lines[0], 1

            def original_drain(*_args, **_kwargs):
                return serial.transcript

            argv = ["runner", "--output-directory", str(output)]
            with (
                mock.patch.object(experiment, "load_baseline", return_value=baseline),
                mock.patch.object(
                    experiment,
                    "verify_installed_diagnostics",
                    return_value={
                        "guest_source_sha256": "b" * 64,
                        "guest_helper_sha256": "c" * 64,
                    },
                ),
                mock.patch.object(gate, "parse_gate_args", return_value=config),
                mock.patch.object(gate, "_next_line", original_next),
                mock.patch.object(
                    gate.PhysicalGraphicsQemuOperations, "drain_serial", original_drain
                ),
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(experiment.main(), 0)
                result = json.loads((output / "result.json").read_text())
                self.assertTrue(result["passed"])
                self.assertFalse(result["physical"])
                self.assertFalse(result["browser_acceptance"])
                self.assertEqual(
                    result["classification"]["first_missing_boundary"],
                    "actor_driver_entry",
                )
                self.assertTrue((output / "full-serial.log").is_file())
                transport_rows = [
                    json.loads(line)
                    for line in (output / "firefox-transport-stages.jsonl")
                    .read_text()
                    .splitlines()
                ]
                self.assertEqual(transport_rows[0]["request"], 4)
                self.assertTrue((output / "base-gate-result.json").is_file())
                with self.assertRaises(FileExistsError):
                    experiment.main()


if __name__ == "__main__":
    unittest.main()
