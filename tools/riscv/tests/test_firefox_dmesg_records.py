# SPDX-License-Identifier: MPL-2.0

import base64
import hashlib
import json
import unittest
import zlib

from tools.riscv.diagnostics.firefox_dmesg_records import (
    DmesgFrames,
    correlate,
    parse_dmesg,
)


def encoded_frames(raw=b"<6>[    1.000000] hello\n", *, meta=None, chunk=19):
    values = {
        "version": 1,
        "raw_bytes": len(raw),
        "retained_bytes": len(raw),
        "discarded_bytes": 0,
        "discarded_lines": 0,
        "early_exit": False,
        "returncode": -15,
    }
    if meta:
        values.update(meta)
    packed = base64.b64encode(zlib.compress(raw)).decode("ascii")
    parts = [packed[index : index + chunk] for index in range(0, len(packed), chunk)]
    digest = hashlib.sha256(raw).hexdigest()
    lines = ["A_FF_DMESG_META " + json.dumps(values, separators=(",", ":"))]
    lines += [
        f"A_FF_DMESG part={index}/{len(parts)} sha256={digest} data={part}"
        for index, part in enumerate(parts)
    ]
    return lines


def receive(lines):
    receiver = DmesgFrames()
    completed = []
    for line in lines:
        value = receiver.accept(line)
        if value is not None:
            completed.append(value)
    receiver.finish()
    return receiver, completed


class DmesgFrameTests(unittest.TestCase):
    def test_accepts_one_complete_sequence_and_ignores_other_lines(self):
        raw = b"<6>[  510.852000] syscall_diag lifecycle=wait pid=70 tid=70\n"
        receiver, completed = receive(["boot", *encoded_frames(raw), "shutdown"])
        self.assertEqual(completed, [raw])
        self.assertEqual(receiver.payload, raw)
        self.assertEqual(receiver.metadata["retained_bytes"], len(raw))

    def test_metadata_schema_uses_canonical_scalar_types(self):
        for change in (
            {"raw_bytes": True},
            {"early_exit": 0},
            {"version": 2},
            {"retained_bytes": -1},
            {"raw_bytes": 0},
            {"extra": 1},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                receive(encoded_frames(meta=change))

    def test_duplicate_incomplete_and_noncontiguous_sequences_fail(self):
        lines = encoded_frames(chunk=5)
        variants = (
            lines[:1],
            [lines[0], lines[0], *lines[1:]],
            [lines[0], lines[2], lines[1], *lines[3:]],
            [*lines, lines[-1]],
        )
        for invalid in variants:
            with self.subTest(lines=len(invalid)), self.assertRaises(ValueError):
                receive(invalid)

    def test_integrity_base64_zlib_hash_and_trailing_stream_are_checked(self):
        raw = b"hello"
        lines = encoded_frames(raw, chunk=4096)
        invalid_base64 = [*lines]
        invalid_base64[-1] = invalid_base64[-1].replace("data=", "data=!")
        invalid_hash = [*lines]
        invalid_hash[-1] = invalid_hash[-1].replace(
            hashlib.sha256(raw).hexdigest(), "0" * 64
        )
        packed = base64.b64encode(zlib.compress(raw) + zlib.compress(b"tail")).decode()
        trailing = [lines[0], lines[1].split(" data=", 1)[0] + " data=" + packed]
        for invalid in (invalid_base64, invalid_hash, trailing):
            with self.assertRaises(ValueError):
                receive(invalid)

    def test_decoded_limit_and_loss_or_early_exit_are_fail_closed(self):
        with self.assertRaises(ValueError):
            receive(encoded_frames(b"x" * (4 * 1024 * 1024 + 1), chunk=4096))
        for meta in (
            {"discarded_bytes": 1, "raw_bytes": 2},
            {"discarded_lines": 1},
            {"early_exit": True, "returncode": 1},
            {"returncode": 7},
        ):
            with self.subTest(meta=meta), self.assertRaises(ValueError):
                receive(encoded_frames(meta=meta))


class DmesgParsingTests(unittest.TestCase):
    def test_parses_lifecycle_and_strict_request_marker(self):
        raw = (
            b"<6>[  510.852000] syscall_diag lifecycle=wait pid=70 tid=70 "
            b"ppid=1 result=-10 outcome=error finished_jiffies=510852\n"
            b"<14>[  510.853000] ASTERINAS_FF_KLOG version=1 phase=request_enter "
            b"request_id=4 firefox_pid=70 client_pid=252\n"
        )
        records = parse_dmesg(raw)
        self.assertEqual(records[0]["kind"], "lifecycle")
        self.assertEqual(records[0]["fields"]["result"], -10)
        self.assertEqual(records[1]["kind"], "marker")
        self.assertEqual(records[1]["request_id"], 4)
        self.assertEqual(records[1]["timestamp_seconds"], 510.853)

    def test_rejects_bad_clock_marker_fields_and_non_utf8(self):
        variants = (
            b"<14>[ nan] ASTERINAS_FF_KLOG version=1 phase=request_enter request_id=4 firefox_pid=70 client_pid=252\n",
            b"<14>[ 1.0] ASTERINAS_FF_KLOG version=1 phase=request_enter request_id=0 firefox_pid=70 client_pid=252\n",
            b"<14>[ 1.0] ASTERINAS_FF_KLOG version=1 phase=secret request_id=4 firefox_pid=70 client_pid=252\n",
            b"<14>[ 1.0] ASTERINAS_FF_KLOG version=1 phase=request_enter request_id=4 firefox_pid=70 client_pid=252 extra=1\n",
            b"\xff\n",
        )
        for raw in variants:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_dmesg(raw)


def actor(stage, *, request=4, pid=70, target=0, host=900):
    return {
        "version": 1,
        "stage": stage,
        "pid": pid,
        "request": request,
        "context": 1,
        "target_pid": target,
        "host_received_monotonic_ns": host,
    }


def transport(event, *, stage="response_header", request=4, complete=True):
    return {
        "version": 1,
        "event": event,
        "pid": 252,
        "monotonic_ns": 700,
        "request_id": request,
        "command": "WebDriver:ExecuteScript",
        "stage": stage,
        "send_complete": complete,
        "header_bytes": 0,
        "body_expected": None,
        "body_received": 0,
    }


def snapshot(phase, syscall=22):
    return {
        "version": 1,
        "phase": phase,
        "host_received_monotonic_ns": 800,
        "tree": {
            "root_pid": 70,
            "root_identity": {"pid": 70, "ppid": 1, "start_time_ticks": 100},
            "processes": [
                {
                    "pid": 70,
                    "threads": [
                        {
                            "tid": 70,
                            "syscall": {
                                "status": "ok",
                                "value": {
                                    "current": {"number": syscall},
                                    "completed": None,
                                },
                            },
                        }
                    ],
                }
            ],
            "limitations": ["fd_limit"],
        },
    }


def dmesg_marker(phase="request_enter", *, request=4):
    return (
        f"<14>[  510.853000] ASTERINAS_FF_KLOG version=1 phase={phase} "
        f"request_id={request} firefox_pid=70 client_pid=252\n"
    ).encode()


def dmesg_window(terminal="request_error"):
    phases = (
        ("collector_started", 0),
        ("request_selected", 4),
        ("snapshot_before_start", 4),
        ("snapshot_before_end", 4),
        ("request_enter", 4),
        ("snapshot_during_start", 4),
        ("snapshot_during_end", 4),
        (terminal, 4),
        ("snapshot_after_start", 4),
        ("snapshot_after_end", 4),
        ("collector_stopping", 4),
    )
    return b"".join(dmesg_marker(phase, request=request) for phase, request in phases)


class CorrelationTests(unittest.TestCase):
    def test_missing_markers_and_unusable_process_snapshots_are_incomplete(self):
        snapshots = [snapshot(name) for name in ("before", "during", "after")]
        for value in snapshots:
            value["tree"]["root_identity"] = {}
            value["tree"]["processes"] = []
            value["tree"]["limitations"] = ["time_limit"]
        with self.assertRaisesRegex(ValueError, "identity|marker"):
            correlate(b"", [], snapshots, [transport("send_complete")])
        with self.assertRaisesRegex(ValueError, "marker"):
            correlate(
                b"",
                [],
                [snapshot(name) for name in ("before", "during", "after")],
                [transport("send_complete")],
            )

    def test_host_and_guest_clocks_remain_separate(self):
        result = correlate(
            dmesg_window(),
            [actor("driver.enter")],
            [snapshot(name) for name in ("before", "during", "after")],
            [transport("send_complete")],
        )
        clocks = {item["clock"] for item in result["timeline"]}
        self.assertIn("kernel_dmesg_seconds", clocks)
        self.assertIn("host_monotonic_ns", clocks)
        self.assertFalse(
            any("normalized_timestamp" in item for item in result["timeline"])
        )

    def test_send_without_driver_entry_selects_no_kernel_primitive(self):
        result = correlate(
            dmesg_window(),
            [],
            [snapshot(name, syscall=22) for name in ("before", "during", "after")],
            [transport("send_complete")],
        )
        self.assertEqual(result["first_missing_boundary"], "actor_driver_entry")
        self.assertIsNone(result["selected_subsystem"])
        self.assertNotIn("missed_wakeup", json.dumps(result))

    def test_actor_boundaries_choose_only_the_first_missing_stage(self):
        cases = (
            (
                ["driver.enter", "parent.query_sent"],
                "child_receive",
                "firefox_parent_content_ipc",
            ),
            (
                ["driver.enter", "parent.query_sent", "child.receive"],
                "child_script_complete",
                "firefox_content_execution",
            ),
            (
                [
                    "driver.enter",
                    "parent.query_sent",
                    "child.receive",
                    "child.script_complete",
                ],
                "parent_query_complete",
                "firefox_reply_delivery",
            ),
        )
        snapshots = [snapshot(name) for name in ("before", "during", "after")]
        for stages, missing, subsystem in cases:
            with self.subTest(missing=missing):
                result = correlate(
                    dmesg_window(),
                    [actor(stage) for stage in stages],
                    snapshots,
                    [transport("send_complete")],
                )
                self.assertEqual(result["first_missing_boundary"], missing)
                self.assertEqual(result["selected_subsystem"], subsystem)

    def test_stable_request_identity_and_complete_snapshots_are_required(self):
        snapshots = [snapshot(name) for name in ("before", "during", "after")]
        for actors, values in (
            ([actor("driver.enter"), actor("parent.query_sent", request=5)], snapshots),
            ([actor("driver.enter")], snapshots[:-1]),
        ):
            with self.assertRaises(ValueError):
                correlate(dmesg_window(), actors, values, [transport("send_complete")])

    def test_request_return_means_the_failure_was_not_reproduced(self):
        records = [
            actor(stage)
            for stage in (
                "driver.enter",
                "parent.query_sent",
                "child.receive",
                "child.script_complete",
                "parent.query_complete",
                "server.response_queued",
            )
        ]
        result = correlate(
            dmesg_window("request_return"),
            records,
            [snapshot(name) for name in ("before", "during", "after")],
            [transport("send_complete"), transport("complete", stage="complete")],
        )
        self.assertEqual(result["first_missing_boundary"], None)
        self.assertEqual(result["outcome"], "failure_not_reproduced")


if __name__ == "__main__":
    unittest.main()
