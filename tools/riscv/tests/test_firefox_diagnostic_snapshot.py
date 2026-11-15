# SPDX-License-Identifier: MPL-2.0

import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE = "tools.riscv.debian.rootfs.firefox_diagnostic_snapshot"
SOURCE = Path(__file__).parents[1] / "debian/rootfs/firefox_diagnostic_snapshot.py"


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SOURCE.is_file(), "bounded diagnostic collector is missing")
        self.collector = importlib.import_module(MODULE)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.proc = Path(self.temporary.name)

    def process(self, pid, ppid, tids=(), *, name="firefox", diagnostic=True):
        root = self.proc / str(pid)
        root.mkdir()
        # Fields 3..22: state, PPid, then seventeen fields, then starttime.
        stat = f"{pid} ({name}) S {ppid} " + "0 " * 17 + "100\n"
        (root / "stat").write_text(stat)
        (root / "status").write_text(
            f"Name:\t{name}\nState:\tS (sleeping)\nPid:\t{pid}\n"
            f"PPid:\t{ppid}\nTgid:\t{pid}\nThreads:\t{len(tids) + 1}\n"
        )
        (root / "comm").write_text(name + "\n")
        (root / "environ").write_text("DO_NOT_EXPORT_SECRET=token")
        (root / "fd").mkdir()
        (root / "fdinfo").mkdir()
        for tid in (pid, *tids):
            thread = root / "task" / str(tid)
            thread.mkdir(parents=True)
            (thread / "stat").write_text(stat.replace(f"{pid} (", f"{tid} (", 1))
            (thread / "status").write_text((root / "status").read_text())
            (thread / "comm").write_text(name + "\n")
            if diagnostic:
                (thread / "asterinas_syscall").write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "enabled": True,
                            "pid": pid,
                            "tid": tid,
                            "snapshot_jiffies": 150,
                            "completed": None,
                            "current": {
                                "sequence": 1,
                                "number": 212,
                                "args": [7, 0, 0, 0, 0, 0],
                                "entered_jiffies": 100,
                            },
                        }
                    )
                )
        return root

    def snapshot(self, **limits):
        return self.collector.collect_snapshot(
            10, proc_root=self.proc, limits=self.collector.Limits(**limits)
        )

    def test_selects_descendants_and_all_threads_without_unrelated_payloads(self):
        self.process(10, 1, (11,))
        self.process(20, 10)
        self.process(30, 20)
        self.process(40, 1, name="UNRELATED_PAYLOAD")
        result = self.snapshot()
        self.assertEqual([p["pid"] for p in result["processes"]], [10, 20, 30])
        self.assertEqual(
            [t["tid"] for t in result["processes"][0]["threads"]], [10, 11]
        )
        self.assertTrue(result["complete"])
        self.assertFalse(result["physical"])
        self.assertNotIn("DO_NOT_EXPORT_SECRET", json.dumps(result))
        self.assertNotIn("UNRELATED_PAYLOAD", json.dumps(result))
        current = result["processes"][0]["threads"][0]["syscall"]["value"]["current"]
        self.assertEqual(current["number"], 212)

    def test_missing_interface_is_unsupported_not_empty_success(self):
        self.process(10, 1, diagnostic=False)
        result = self.snapshot()
        diagnostic = result["processes"][0]["threads"][0]["syscall"]
        self.assertEqual(diagnostic["status"], "unsupported")
        self.assertFalse(result["complete"])

    def test_absent_root_is_reported_as_gone(self):
        result = self.snapshot()
        self.assertFalse(result["complete"])
        self.assertIn("root_process_gone", result["limitations"])

    def test_process_thread_and_fd_caps_are_explicit(self):
        root = self.process(10, 1, (11, 12))
        self.process(20, 10)
        for fd in (3, 4):
            (root / "fd" / str(fd)).symlink_to("socket:[123]")
            (root / "fdinfo" / str(fd)).write_text("flags:\t04002\n")
        result = self.snapshot(max_processes=1, max_threads=1, max_fds=1)
        self.assertEqual(len(result["processes"]), 1)
        self.assertEqual(len(result["processes"][0]["threads"]), 1)
        self.assertEqual(len(result["processes"][0]["fds"]), 1)
        self.assertTrue(
            {"process_limit", "thread_limit", "fd_limit"} <= set(result["limitations"])
        )

    def test_invalid_diagnostic_json_and_disabled_capture_are_distinct(self):
        root = self.process(10, 1)
        path = root / "task/10/asterinas_syscall"
        for value, expected in (("{bad", "invalid_json"), ("{}", "invalid_schema")):
            path.write_text(value)
            result = self.snapshot()
            self.assertEqual(
                result["processes"][0]["threads"][0]["syscall"]["status"], expected
            )
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "enabled": False,
                    "pid": 10,
                    "tid": 10,
                    "snapshot_jiffies": 0,
                    "current": None,
                    "completed": None,
                }
            )
        )
        result = self.snapshot()
        self.assertEqual(
            result["processes"][0]["threads"][0]["syscall"]["status"], "disabled"
        )

    def test_oversize_and_total_byte_budget_are_bounded(self):
        root = self.process(10, 1)
        (root / "task/10/asterinas_syscall").write_text("x" * 2000)
        result = self.snapshot(max_file_bytes=1024)
        self.assertEqual(
            result["processes"][0]["threads"][0]["syscall"]["status"], "too_large"
        )
        result = self.snapshot(max_total_bytes=32)
        self.assertIn("byte_limit", result["limitations"])
        self.assertLessEqual(result["bytes_read"], 32)

    def test_rejects_symlink_content_and_reports_permission_errors(self):
        root = self.process(10, 1)
        path = root / "task/10/asterinas_syscall"
        path.unlink()
        path.symlink_to(root / "environ")
        result = self.snapshot()
        self.assertEqual(
            result["processes"][0]["threads"][0]["syscall"]["status"], "io_error"
        )
        original = self.collector.os.open

        def denied(name, flags):
            if str(name).endswith("asterinas_syscall"):
                raise PermissionError(13, "permission denied")
            return original(name, flags)

        with mock.patch.object(self.collector.os, "open", side_effect=denied):
            result = self.snapshot()
        self.assertEqual(
            result["processes"][0]["threads"][0]["syscall"]["status"],
            "permission_denied",
        )

    def test_rejects_invalid_limits_and_pid(self):
        for limits in (
            {"max_threads": 0},
            {"max_seconds": float("nan")},
            {"max_file_bytes": -1},
        ):
            with self.assertRaises(ValueError):
                self.collector.Limits(**limits)
        with self.assertRaises(ValueError):
            self.collector.collect_snapshot(0, proc_root=self.proc)

    def test_deadline_preserves_partial_evidence_as_unverified(self):
        self.process(10, 1)
        original = self.collector._identity

        def unavailable_at_end(reader, directory):
            if directory == self.proc / "10" and reader.bytes_read > 300:
                reader.limitations.add("time_limit")
                return None
            return original(reader, directory)

        with mock.patch.object(
            self.collector, "_identity", side_effect=unavailable_at_end
        ):
            result = self.snapshot()
        self.assertFalse(result["complete"])
        self.assertEqual(result["processes"][0]["identity_verified_after"], False)
        self.assertTrue(result["processes"][0]["threads"])
        self.assertIn("root_identity_unverified", result["limitations"])

    def test_deadline_and_scan_limits_are_explicit(self):
        self.process(10, 1)
        self.process(20, 10)
        with mock.patch.object(
            self.collector.time, "monotonic", side_effect=[0] + [5] * 20
        ):
            result = self.snapshot(max_seconds=1)
        self.assertIn("time_limit", result["limitations"])
        self.assertEqual(result["bytes_read"], 0)
        self.assertIn("scan_limit", self.snapshot(max_scan=1)["limitations"])

    def test_replaced_ancestor_invalidates_its_selected_descendants(self):
        self.process(10, 1)
        self.process(20, 10)
        self.process(30, 20, name="UNRELATED_REUSED_PARENT")
        original = self.collector._identity
        for replace_at in (2, 4):
            with self.subTest(replace_at=replace_at):
                calls = 0

                def replaced(reader, directory):
                    nonlocal calls
                    result = original(reader, directory)
                    if directory == self.proc / "20":
                        calls += 1
                        if calls >= replace_at and result is not None:
                            result = {**result, "start_time_ticks": 200}
                    return result

                with mock.patch.object(
                    self.collector, "_identity", side_effect=replaced
                ):
                    result = self.snapshot()
                self.assertNotIn("UNRELATED_REUSED_PARENT", json.dumps(result))
                self.assertFalse(result["complete"])

    def test_invalid_unsigned_registers_and_completion_fields_are_rejected(self):
        root = self.process(10, 1)
        path = root / "task/10/asterinas_syscall"
        baseline = json.loads(path.read_text())
        for key, value in (
            ("args", [-1, 0, 0, 0, 0, 0]),
            ("args", [2**64, 0, 0, 0, 0, 0]),
            ("entered_jiffies", 2**64),
        ):
            invalid = {**baseline, "current": {**baseline["current"], key: value}}
            path.write_text(json.dumps(invalid))
            self.assertEqual(
                self.snapshot()["processes"][0]["threads"][0]["syscall"]["status"],
                "invalid_schema",
            )
        completion = {
            **baseline["current"],
            "finished_jiffies": 151,
            "outcome": "no_return",
            "result": None,
        }
        for invalid in (
            {key: value for key, value in completion.items() if key != "result"},
            {**completion, "finished_jiffies": -1},
        ):
            path.write_text(json.dumps({**baseline, "completed": invalid}))
            self.assertEqual(
                self.snapshot()["processes"][0]["threads"][0]["syscall"]["status"],
                "invalid_schema",
            )

    def test_v2_bounded_completion_history_is_validated(self):
        root = self.process(10, 1)
        path = root / "task/10/asterinas_syscall"
        call = {
            "sequence": 1,
            "number": 207,
            "args": [46, 0, 521, 0, 0, 0],
            "entered_jiffies": 100,
            "finished_jiffies": 101,
            "result": 521,
            "outcome": "return",
        }
        value = {
            "version": 2,
            "enabled": True,
            "pid": 10,
            "tid": 10,
            "snapshot_jiffies": 150,
            "current": None,
            "completed": call,
            "history": [call],
        }
        path.write_text(json.dumps(value))
        result = self.snapshot()["processes"][0]["threads"][0]["syscall"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["value"]["history"][0]["result"], 521)

        for invalid in (
            {key: item for key, item in value.items() if key != "history"},
            {**value, "history": [call] * 33},
            {**value, "history": [{**call, "sequence": 2}, call]},
            {**value, "completed": {**call, "result": 0}},
        ):
            path.write_text(json.dumps(invalid))
            status = self.snapshot()["processes"][0]["threads"][0]["syscall"]["status"]
            self.assertEqual(status, "invalid_schema")


if __name__ == "__main__":
    unittest.main()
