#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for bounded Firefox/Xorg process and system-time evidence."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools.riscv.debian.rootfs import browser_system_time

from tools.riscv.debian.rootfs.browser_system_time import (
    Snapshot,
    TimeEvidenceError,
    interval,
    parse_cpu_stat,
    parse_mem_available,
    parse_pid_stat,
    parse_schedstat,
    read_proc_text,
    read_snapshot,
    run_sampler,
)


CPU_A = "cpu 100 0 20 80 0 0 0 0 0 0\nctxt 50\nprocs_running 2\n"
CPU_B = "cpu 110 0 25 100 0 0 0 0 0 0\nctxt 60\nprocs_running 1\n"
CPU_REGRESSED = "cpu 99 0 20 80 0 0 0 0 0 0\nctxt 50\nprocs_running 2\n"
CPU_CORES_A = (
    "cpu 100 0 20 80 0 0 0 0 0 0\n"
    "cpu0 50 0 10 40 0 0 0 0 0 0\n"
    "cpu1 50 0 10 40 0 0 0 0 0 0\n"
    "ctxt 50\nprocs_running 2\n"
)
CPU_CORES_B = (
    "cpu 110 0 25 100 0 0 0 0 0 0\n"
    "cpu0 60 0 15 40 0 0 0 0 0 0\n"
    "cpu1 50 0 10 60 0 0 0 0 0 0\n"
    "ctxt 60\nprocs_running 1\n"
)


def pid_stat(
    utime: int,
    stime: int,
    starttime: int,
    *,
    minor_faults: int = 0,
    major_faults: int = 0,
    rss_pages: int = 0,
) -> str:
    fields = ["S"] + ["0"] * 49
    fields[7] = str(minor_faults)
    fields[9] = str(major_faults)
    fields[11] = str(utime)
    fields[12] = str(stime)
    fields[19] = str(starttime)
    fields[21] = str(rss_pages)
    return "42 (Firefox (Main)) " + " ".join(fields)


PID_A = pid_stat(120, 30, 777)
PID_B = pid_stat(130, 35, 777)


class ProcParserTests(unittest.TestCase):
    def test_schedstat_reads_exact_linux_three_field_abi(self) -> None:
        parsed = parse_schedstat("123 456 7\n")

        self.assertEqual(parsed.cpu_runtime_ns, 123)
        self.assertEqual(parsed.runqueue_wait_ns, 456)
        self.assertEqual(parsed.dispatch_count, 7)

    def test_schedstat_rejects_noncanonical_or_overflowing_input(self) -> None:
        invalid = (
            "1 2\n",
            "1 2 3 4\n",
            "1 2 3",
            "1  2 3\n",
            "-1 2 3\n",
            "18446744073709551616 2 3\n",
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(TimeEvidenceError):
                parse_schedstat(raw)

    def test_pid_stat_handles_parentheses_in_comm(self) -> None:
        parsed = parse_pid_stat(
            pid_stat(
                120,
                30,
                777,
                minor_faults=12,
                major_faults=3,
                rss_pages=400,
            )
        )
        self.assertEqual(
            (
                parsed.pid,
                parsed.comm,
                parsed.utime_ticks,
                parsed.stime_ticks,
                parsed.starttime_ticks,
            ),
            (42, "Firefox (Main)", 120, 30, 777),
        )
        self.assertEqual(parsed.minor_faults, 12)
        self.assertEqual(parsed.major_faults, 3)
        self.assertEqual(parsed.rss_pages, 400)

    def test_meminfo_reads_one_linux_memavailable_value(self) -> None:
        self.assertEqual(
            parse_mem_available("MemTotal: 1000 kB\nMemAvailable: 700 kB\n"),
            700,
        )
        for invalid in (
            "MemTotal: 1000 kB\n",
            "MemAvailable: 1 MB\n",
            "MemAvailable: -1 kB\n",
            "MemAvailable: 1 kB\nMemAvailable: 2 kB\n",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(TimeEvidenceError):
                parse_mem_available(invalid)

    def test_cpu_stat_reads_ten_global_ticks_and_switch_count(self) -> None:
        parsed = parse_cpu_stat(CPU_A)
        self.assertEqual(parsed.ticks, (100, 0, 20, 80, 0, 0, 0, 0, 0, 0))
        self.assertEqual(parsed.ctxt, 50)
        self.assertEqual(parsed.procs_running, 2)

    def test_cpu_stat_reads_each_core_without_conflating_guest_ticks(self) -> None:
        parsed = parse_cpu_stat(CPU_CORES_A)
        self.assertEqual(parsed.per_cpu[0][0], 0)
        self.assertEqual(parsed.per_cpu[0][1], (50, 0, 10, 40, 0, 0, 0, 0, 0, 0))
        self.assertEqual(parsed.per_cpu[1][0], 1)

    def test_cpu_stat_rejects_duplicate_or_malformed_core(self) -> None:
        malformed = (
            CPU_CORES_A.replace("cpu1 ", "cpu0 "),
            CPU_CORES_A.replace("cpu1 50", "cpu1 -50"),
            CPU_CORES_A.replace("cpu1 50 0 10", "cpu1 50 0"),
        )
        for raw in malformed:
            with self.subTest(raw=raw), self.assertRaises(TimeEvidenceError):
                parse_cpu_stat(raw)

    def test_cpu_stat_rejects_missing_duplicate_and_negative_counts(self) -> None:
        invalid = (
            "cpu 1 2 3\nctxt 4\n",
            CPU_A + "ctxt 51\n",
            "cpu -1 2 3 4 5 6 7 8 9 10\nctxt 4\nprocs_running 1\n",
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(TimeEvidenceError):
                parse_cpu_stat(raw)

    def test_pid_stat_rejects_missing_and_malformed_cpu_fields(self) -> None:
        invalid = (
            "42 Firefox S 0 0",
            pid_stat(120, 30, 777).replace(" 120 ", " -120 "),
            "42 (Firefox (Main)) RS " + "0 " * 49,
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(TimeEvidenceError):
                parse_pid_stat(raw)


class IntervalTests(unittest.TestCase):
    @staticmethod
    def snapshot(monotonic_ns: int, cpu: str, pid: str) -> Snapshot:
        return Snapshot(
            guest_monotonic_ns=monotonic_ns,
            system=parse_cpu_stat(cpu),
            processes=(parse_pid_stat(pid),),
        )

    def test_interval_reports_cpu_deltas_and_separate_clock_domain(self) -> None:
        before = self.snapshot(
            1_000_000_000,
            CPU_A,
            pid_stat(120, 30, 777, minor_faults=10, major_faults=2, rss_pages=300),
        )
        after = self.snapshot(
            2_000_000_000,
            CPU_B,
            pid_stat(130, 35, 777, minor_faults=17, major_faults=3, rss_pages=350),
        )

        report = interval(before, after, clock_ticks_per_second=100)

        self.assertEqual(report["clock_domain"], "guest-monotonic")
        self.assertEqual(report["duration_ms"], 1000)
        self.assertEqual(report["guest_monotonic_start_ns"], 1_000_000_000)
        self.assertEqual(report["guest_monotonic_end_ns"], 2_000_000_000)
        self.assertEqual(report["processes"][0]["cpu_user_ms"], 100)
        self.assertEqual(report["processes"][0]["cpu_kernel_ms"], 50)
        self.assertEqual(report["processes"][0]["minor_faults"], 7)
        self.assertEqual(report["processes"][0]["major_faults"], 1)
        self.assertEqual(report["processes"][0]["rss_pages_after"], 350)
        self.assertEqual(
            report["system"]["cpu_ticks"], [10, 0, 5, 20, 0, 0, 0, 0, 0, 0]
        )
        self.assertEqual(report["system"]["context_switches"], 10)
        self.assertIn("per-process-io", report["unsupported"])
        self.assertIn("physical-hdmi-scanout", report["unsupported"])

    def test_interval_rejects_pid_reuse(self) -> None:
        before = self.snapshot(1_000_000_000, CPU_A, PID_A)
        reused = self.snapshot(2_000_000_000, CPU_B, pid_stat(130, 35, 778))

        with self.assertRaisesRegex(TimeEvidenceError, "identity"):
            interval(before, reused, clock_ticks_per_second=100)

    def test_interval_reports_per_core_busy_fraction(self) -> None:
        before = self.snapshot(1_000_000_000, CPU_CORES_A, PID_A)
        after = self.snapshot(2_000_000_000, CPU_CORES_B, PID_B)
        report = interval(before, after, clock_ticks_per_second=100)
        self.assertEqual(report["system"]["per_cpu"][0]["cpu_id"], 0)
        self.assertEqual(report["system"]["per_cpu"][0]["busy_fraction"], 1.0)
        self.assertEqual(report["system"]["per_cpu"][1]["busy_fraction"], 0.0)

    def test_interval_rejects_core_identity_change_or_counter_regression(self) -> None:
        before = self.snapshot(1_000_000_000, CPU_CORES_A, PID_A)
        replaced = self.snapshot(
            2_000_000_000, CPU_CORES_B.replace("cpu1 ", "cpu2 "), PID_B
        )
        regressed = self.snapshot(
            2_000_000_000, CPU_CORES_B.replace("cpu0 60", "cpu0 49"), PID_B
        )
        for after in (replaced, regressed):
            with self.assertRaises(TimeEvidenceError):
                interval(before, after, clock_ticks_per_second=100)

    def test_interval_rejects_impossible_per_core_tick_rate(self) -> None:
        before = self.snapshot(1_000_000_000, CPU_CORES_A, PID_A)
        impossible = self.snapshot(
            2_000_000_000,
            CPU_CORES_B.replace("cpu0 60", "cpu0 1060"),
            PID_B,
        )

        with self.assertRaisesRegex(TimeEvidenceError, "CPU tick rate"):
            interval(before, impossible, clock_ticks_per_second=100)

    def test_interval_rejects_system_counter_regression(self) -> None:
        before = self.snapshot(1_000_000_000, CPU_A, PID_A)
        regressed = self.snapshot(2_000_000_000, CPU_REGRESSED, PID_B)

        with self.assertRaisesRegex(TimeEvidenceError, "regressed"):
            interval(before, regressed, clock_ticks_per_second=100)

    def test_interval_rejects_nonadvancing_clock_and_invalid_tick_rate(self) -> None:
        before = self.snapshot(1_000_000_000, CPU_A, PID_A)
        after = self.snapshot(1_000_000_000, CPU_B, PID_B)

        with self.assertRaisesRegex(TimeEvidenceError, "clock"):
            interval(before, after, clock_ticks_per_second=100)
        with self.assertRaisesRegex(TimeEvidenceError, "ticks"):
            interval(
                before,
                self.snapshot(2_000_000_000, CPU_B, PID_B),
                clock_ticks_per_second=0,
            )


class SamplerTests(unittest.TestCase):
    def test_thread_sampler_rejects_existing_output_before_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc_root = self.fake_proc(directory)
            thread_stat = proc_root / "42" / "task" / "42" / "stat"
            thread_stat.parent.mkdir(parents=True)
            thread_stat.write_text(pid_stat(40, 12, 777))
            (thread_stat.parent / "schedstat").write_text("520000000 10000000 8\n")
            marker = Path(directory) / "ready"
            marker.write_text(f"{time.monotonic_ns()}\n")
            output = Path(directory) / "threads.json"
            output.write_text("user-owned")

            def sampling_started(_seconds: float) -> None:
                self.fail("sampling ran even though the output already existed")

            with self.assertRaisesRegex(TimeEvidenceError, "exclusive"):
                browser_system_time.run_thread_sampler(
                    proc_root,
                    42,
                    marker,
                    output,
                    interval_seconds=0.25,
                    samples=1,
                    sleep_fn=sampling_started,
                )
            self.assertEqual(output.read_text(), "user-owned")

    def test_phase_triggered_thread_sampler_publishes_scoped_cpu_deltas(self) -> None:
        run_threads = getattr(browser_system_time, "run_thread_sampler", None)
        self.assertIsNotNone(run_threads, "phase-triggered TID sampler is missing")

        with tempfile.TemporaryDirectory() as directory:
            proc_root = self.fake_proc(directory)
            thread_stat = proc_root / "42" / "task" / "42" / "stat"
            thread_stat.parent.mkdir(parents=True)
            thread_stat.write_text(pid_stat(40, 12, 777))
            (thread_stat.parent / "schedstat").write_text("520000000 10000000 8\n")
            marker_path = Path(directory) / "ready"
            marker_ns = time.monotonic_ns()
            marker_path.write_text(f"{marker_ns}\n")
            output = Path(directory) / "threads.json"
            timestamps = iter((marker_ns + 1_000_000, marker_ns + 1_001_000_000))

            def advance(_seconds: float) -> None:
                thread_stat.write_text(pid_stat(60, 17, 777))
                (thread_stat.parent / "schedstat").write_text("770000000 40000000 13\n")

            report = run_threads(
                proc_root,
                42,
                marker_path,
                output,
                interval_seconds=0.25,
                samples=1,
                physical=True,
                clock_ns=lambda: next(timestamps),
                sleep_fn=advance,
                clock_ticks_per_second=100,
            )

            self.assertEqual(report["ready_marker_ns"], marker_ns)
            self.assertTrue(report["physical"])
            self.assertEqual(report["clock_domain"], "guest-monotonic")
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["process_id"], 42)
            self.assertEqual(report["samples"], 1)
            self.assertEqual(report["intervals"][0]["threads"][0]["cpu_user_ms"], 200)
            self.assertEqual(report["intervals"][0]["threads"][0]["cpu_kernel_ms"], 50)
            self.assertEqual(
                report["intervals"][0]["threads"][0]["schedstat"],
                {
                    "before": {
                        "cpu_runtime_ns": 520000000,
                        "runqueue_wait_ns": 10000000,
                        "dispatch_count": 8,
                    },
                    "after": {
                        "cpu_runtime_ns": 770000000,
                        "runqueue_wait_ns": 40000000,
                        "dispatch_count": 13,
                    },
                    "delta": {
                        "cpu_runtime_ns": 250000000,
                        "runqueue_wait_ns": 30000000,
                        "dispatch_count": 5,
                    },
                },
            )
            self.assertNotIn("per-thread-runnable-wait", report["unsupported"])
            self.assertEqual(json.loads(output.read_text()), report)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_thread_interval_splits_user_kernel_and_reports_churn(self) -> None:
        before = {
            "guest_monotonic_ns": 1_000_000_000,
            "process_starttime_ticks": 77,
            "vanished_tids": [],
            "threads": {
                42: {
                    "comm": "GeckoMain",
                    "utime_ticks": 10,
                    "stime_ticks": 20,
                    "starttime_ticks": 77,
                    "last_cpu": 1,
                    "schedstat": {
                        "cpu_runtime_ns": 300_000_000,
                        "runqueue_wait_ns": 10_000_000,
                        "dispatch_count": 2,
                    },
                },
                43: {
                    "comm": "short-lived",
                    "utime_ticks": 0,
                    "stime_ticks": 0,
                    "starttime_ticks": 78,
                    "last_cpu": 0,
                    "schedstat": {
                        "cpu_runtime_ns": 0,
                        "runqueue_wait_ns": 0,
                        "dispatch_count": 0,
                    },
                },
            },
        }
        after = {
            "guest_monotonic_ns": 2_000_000_000,
            "process_starttime_ticks": 77,
            "vanished_tids": [45],
            "threads": {
                42: {
                    "comm": "GeckoMain",
                    "utime_ticks": 15,
                    "stime_ticks": 32,
                    "starttime_ticks": 77,
                    "last_cpu": 2,
                    "schedstat": {
                        "cpu_runtime_ns": 470_000_000,
                        "runqueue_wait_ns": 30_000_000,
                        "dispatch_count": 7,
                    },
                },
                44: {
                    "comm": "new-thread",
                    "utime_ticks": 0,
                    "stime_ticks": 0,
                    "starttime_ticks": 79,
                    "last_cpu": 2,
                    "schedstat": {
                        "cpu_runtime_ns": 0,
                        "runqueue_wait_ns": 0,
                        "dispatch_count": 0,
                    },
                },
            },
        }
        result = browser_system_time.thread_interval(
            before, after, clock_ticks_per_second=100
        )
        self.assertEqual(result["duration_ms"], 1000)
        self.assertEqual(result["new_tids"], [44])
        self.assertEqual(result["gone_tids"], [43])
        self.assertEqual(result["vanished_tids"], [45])
        self.assertEqual(result["threads"][0]["cpu_user_ms"], 50)
        self.assertEqual(result["threads"][0]["cpu_kernel_ms"], 120)
        self.assertEqual(
            result["threads"][0]["schedstat"]["delta"]["runqueue_wait_ns"],
            20_000_000,
        )

    def test_thread_sampler_stops_after_terminal_event_with_a_final_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc_root = self.fake_proc(directory)
            thread_stat = proc_root / "42" / "task" / "42" / "stat"
            thread_stat.parent.mkdir(parents=True)
            thread_stat.write_text(pid_stat(40, 12, 777))
            schedstat = thread_stat.parent / "schedstat"
            schedstat.write_text("520000000 10000000 8\n")
            marker_path = Path(directory) / "ready"
            marker_ns = time.monotonic_ns()
            marker_path.write_text(f"{marker_ns}\n")
            output = Path(directory) / "threads.json"
            stop = threading.Event()
            timestamps = iter((marker_ns + 1_000_000, marker_ns + 1_001_000_000))

            def initial_ready() -> None:
                thread_stat.write_text(pid_stat(60, 17, 777))
                schedstat.write_text("770000000 40000000 13\n")
                stop.set()

            report = browser_system_time.run_thread_sampler(
                proc_root,
                42,
                marker_path,
                output,
                interval_seconds=0.25,
                samples=64,
                ready_fn=initial_ready,
                stop_event=stop,
                clock_ns=lambda: next(timestamps),
                sleep_fn=lambda _seconds: self.fail("stop-aware sampler slept"),
                clock_ticks_per_second=100,
            )

            self.assertEqual(report["samples"], 1)
            self.assertEqual(len(report["intervals"]), 1)

    def test_thread_snapshot_reads_tid_cpu_and_last_cpu_from_proc_stat(self) -> None:
        read_threads = getattr(browser_system_time, "read_thread_snapshot", None)
        self.assertIsNotNone(read_threads, "bounded thread snapshot is missing")

        with tempfile.TemporaryDirectory() as directory:
            proc_root = self.fake_proc(directory)
            for tid, user_ticks, kernel_ticks, last_cpu, comm in (
                (42, 40, 12, 2, "Firefox (Main)"),
                (43, 10, 4, 1, "Renderer"),
            ):
                thread_dir = proc_root / "42" / "task" / str(tid)
                thread_dir.mkdir(parents=True)
                raw = pid_stat(user_ticks, kernel_ticks, 777)
                raw = raw.replace("42 (Firefox (Main))", f"{tid} ({comm})")
                prefix, fields_text = raw.rsplit(") ", 1)
                fields = fields_text.split()
                fields[36] = str(last_cpu)
                (thread_dir / "stat").write_text(
                    prefix + ") " + " ".join(fields) + "\n"
                )
                (thread_dir / "schedstat").write_text(
                    f"{(user_ticks + kernel_ticks) * 10000000} "
                    f"{tid * 1000000} {tid - 40}\n"
                )

            snapshot = read_threads(proc_root, 42, 1_000_000_000)

            self.assertEqual(snapshot["guest_monotonic_ns"], 1_000_000_000)
            self.assertEqual(snapshot["process_starttime_ticks"], 777)
            self.assertEqual(
                snapshot["threads"][42],
                {
                    "comm": "Firefox (Main)",
                    "utime_ticks": 40,
                    "stime_ticks": 12,
                    "starttime_ticks": 777,
                    "last_cpu": 2,
                    "schedstat": {
                        "cpu_runtime_ns": 520000000,
                        "runqueue_wait_ns": 42000000,
                        "dispatch_count": 2,
                    },
                },
            )
            self.assertEqual(snapshot["threads"][43]["last_cpu"], 1)

    def test_thread_snapshot_records_a_tid_that_exits_during_procfs_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc_root = self.fake_proc(directory)
            for tid in (42, 43):
                thread_dir = proc_root / "42" / "task" / str(tid)
                thread_dir.mkdir(parents=True)
                raw = pid_stat(40, 12, 777).replace(
                    "42 (Firefox (Main))", f"{tid} (Firefox)"
                )
                (thread_dir / "stat").write_text(raw)
                (thread_dir / "schedstat").write_text("520000000 40000000 8\n")

            real_read = browser_system_time.read_proc_text

            def exit_before_stat(path: Path) -> str:
                if path == proc_root / "42" / "task" / "43" / "stat":
                    (path.parent / "schedstat").unlink()
                    path.unlink()
                return real_read(path)

            with mock.patch.object(
                browser_system_time, "read_proc_text", side_effect=exit_before_stat
            ):
                snapshot = browser_system_time.read_thread_snapshot(
                    proc_root, 42, 1_000_000_000
                )

            self.assertEqual(tuple(snapshot["threads"]), (42,))
            self.assertEqual(snapshot["vanished_tids"], [43])

    def test_thread_sampler_retains_the_bounded_maximum_thread_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ready"
            marker_ns = time.monotonic_ns()
            marker.write_text(f"{marker_ns}\n")
            output = Path(directory) / "threads.json"
            timestamps = iter(
                marker_ns + 1 + index * 250_000_000 for index in range(65)
            )
            snapshot_index = 0

            def snapshot(_root: Path, _pid: int, guest_ns: int) -> dict[str, object]:
                nonlocal snapshot_index
                index = snapshot_index
                snapshot_index += 1
                return {
                    "guest_monotonic_ns": guest_ns,
                    "process_starttime_ticks": 777,
                    "threads": {
                        tid: {
                            "comm": f"Firefox-{tid}",
                            "utime_ticks": index,
                            "stime_ticks": index,
                            "starttime_ticks": 1000 + tid,
                            "last_cpu": tid % 4,
                            "schedstat": {
                                "cpu_runtime_ns": index * 20_000_000,
                                "runqueue_wait_ns": index * 10_000_000,
                                "dispatch_count": index,
                            },
                        }
                        for tid in range(42, 42 + 128)
                    },
                    "vanished_tids": [],
                }

            with (
                mock.patch.object(
                    browser_system_time,
                    "read_thread_snapshot",
                    side_effect=snapshot,
                ),
                mock.patch.object(os, "sched_getaffinity", return_value={0, 1}),
            ):
                report = browser_system_time.run_thread_sampler(
                    Path(directory) / "proc",
                    42,
                    marker,
                    output,
                    interval_seconds=0.25,
                    samples=64,
                    clock_ns=lambda: next(timestamps),
                    sleep_fn=lambda _seconds: None,
                    clock_ticks_per_second=100,
                )

            self.assertEqual(len(report["intervals"]), 64)
            self.assertEqual(len(report["intervals"][0]["threads"]), 128)
            self.assertGreater(output.stat().st_size, 256 * 1024)
            self.assertLessEqual(output.stat().st_size, 8 * 1024 * 1024)

    def test_thread_sampler_rejects_schedstat_counter_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc_root = self.fake_proc(directory)
            thread_dir = proc_root / "42" / "task" / "42"
            thread_dir.mkdir(parents=True)
            (thread_dir / "stat").write_text(pid_stat(40, 12, 777))
            (thread_dir / "schedstat").write_text("520000000 40000000 8\n")
            marker = Path(directory) / "ready"
            marker_ns = time.monotonic_ns()
            marker.write_text(f"{marker_ns}\n")
            timestamps = iter((marker_ns + 1, marker_ns + 1_000_000_001))

            def regress(_seconds: float) -> None:
                (thread_dir / "stat").write_text(pid_stat(41, 12, 777))
                (thread_dir / "schedstat").write_text("530000000 39999999 9\n")

            with self.assertRaisesRegex(TimeEvidenceError, "schedstat"):
                browser_system_time.run_thread_sampler(
                    proc_root,
                    42,
                    marker,
                    Path(directory) / "threads.json",
                    interval_seconds=0.25,
                    samples=1,
                    clock_ns=lambda: next(timestamps),
                    sleep_fn=regress,
                    clock_ticks_per_second=100,
                )

    def test_phase_wait_starts_only_after_a_real_ready_marker(self) -> None:
        wait = getattr(browser_system_time, "wait_for_ready_marker", None)
        self.assertIsNotNone(wait, "condition-based READY wait is missing")

        with tempfile.TemporaryDirectory() as directory:
            marker_path = Path(directory) / "ready"
            polls = []

            def publish(_seconds: float) -> None:
                polls.append(1)
                marker_path.write_text(f"{time.monotonic_ns()}\n")

            marker_ns = wait(
                marker_path,
                timeout_seconds=1,
                sleep_fn=publish,
            )

            self.assertEqual(len(polls), 1)
            self.assertEqual(marker_ns, int(marker_path.read_text().strip()))

    @staticmethod
    def fake_proc(directory: str) -> Path:
        proc_root = Path(directory) / "proc"
        (proc_root / "42").mkdir(parents=True)
        (proc_root / "stat").write_text(CPU_A)
        (proc_root / "42" / "stat").write_text(PID_A)
        return proc_root

    def test_read_snapshot_uses_requested_process_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc_root = self.fake_proc(directory)

            snapshot = read_snapshot(proc_root, (42,), 1_000_000_000)

            self.assertEqual(snapshot.guest_monotonic_ns, 1_000_000_000)
            self.assertEqual(snapshot.processes[0].pid, 42)
            self.assertEqual(snapshot.system.ctxt, 50)

            (proc_root / "42" / "stat").write_text(PID_A.replace("42 (", "43 ("))
            with self.assertRaisesRegex(TimeEvidenceError, "identity"):
                read_snapshot(proc_root, (42,), 1_000_000_000)

    def test_proc_read_rejects_oversized_and_symlink_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.write_text(CPU_A)
            self.assertEqual(read_proc_text(data), CPU_A)

            data.write_bytes(b"x" * 8193)
            with self.assertRaisesRegex(TimeEvidenceError, "oversized"):
                read_proc_text(data)

            link = Path(directory) / "link"
            link.symlink_to(data)
            with self.assertRaisesRegex(TimeEvidenceError, "procfs"):
                read_proc_text(link)

    def test_sampler_publishes_private_real_proc_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc_root = self.fake_proc(directory)
            output = Path(directory) / "time.json"
            ticks = iter((1_000_000_000, 2_000_000_000))

            def advance(_seconds: float) -> None:
                (proc_root / "stat").write_text(CPU_B)
                (proc_root / "42" / "stat").write_text(PID_B)

            report = run_sampler(
                proc_root,
                (42,),
                output,
                interval_seconds=0.25,
                samples=1,
                clock_ns=lambda: next(ticks),
                sleep_fn=advance,
                clock_ticks_per_second=100,
            )

            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["intervals"][0]["processes"][0]["cpu_user_ms"], 100)
            self.assertEqual(json.loads(output.read_text()), report)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

            ticks = iter((3_000_000_000, 4_000_000_000))
            with self.assertRaisesRegex(TimeEvidenceError, "exclusive"):
                run_sampler(
                    proc_root,
                    (42,),
                    output,
                    interval_seconds=0.25,
                    samples=1,
                    clock_ns=lambda: next(ticks),
                    sleep_fn=lambda _: None,
                    clock_ticks_per_second=100,
                )

    def test_sampler_signals_initial_snapshot_and_stops_after_terminal_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc_root = self.fake_proc(directory)
            output = Path(directory) / "time.json"
            stop = threading.Event()
            ready: list[str] = []
            ticks = iter((1_000_000_000, 2_000_000_000))

            def initial_ready() -> None:
                ready.append("initial-snapshot")
                (proc_root / "stat").write_text(CPU_B)
                (proc_root / "42" / "stat").write_text(PID_B)
                stop.set()

            report = run_sampler(
                proc_root,
                (42,),
                output,
                interval_seconds=0.25,
                samples=64,
                ready_fn=initial_ready,
                stop_event=stop,
                clock_ns=lambda: next(ticks),
                sleep_fn=lambda _seconds: self.fail("stop-aware sampler slept"),
                clock_ticks_per_second=100,
            )

            self.assertEqual(ready, ["initial-snapshot"])
            self.assertEqual(report["samples"], 1)
            self.assertEqual(len(report["intervals"]), 1)

    def test_sampler_rejects_unbounded_rate_and_duplicate_pids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc_root = self.fake_proc(directory)
            output = Path(directory) / "time.json"
            cases = ((0.249, 1, (42,)), (0.25, 65, (42,)), (0.25, 1, (42, 42)))
            for rate, samples, pids in cases:
                with self.subTest(rate=rate, samples=samples, pids=pids):
                    with self.assertRaises(TimeEvidenceError):
                        run_sampler(
                            proc_root,
                            pids,
                            output,
                            interval_seconds=rate,
                            samples=samples,
                        )
            self.assertFalse(output.exists())

    def test_cli_samples_existing_process_without_restarting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "host-time.json"
            process = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "tools.riscv.debian.rootfs.browser_system_time",
                    "--pid",
                    str(os.getpid()),
                    "--interval-seconds",
                    "0.25",
                    "--samples",
                    "1",
                    "--output",
                    str(output),
                ),
                capture_output=True,
                text=True,
                timeout=5,
            )

            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["samples"], 1)
            self.assertEqual(report["process_ids"], [os.getpid()])
            self.assertEqual(len(report["intervals"]), 1)

    def test_cli_phase_triggered_thread_mode_uses_short_guest_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "threads.json"
            marker = Path(directory) / "ready"
            marker.write_text(f"{time.monotonic_ns()}\n")
            process = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "tools.riscv.debian.rootfs.browser_system_time",
                    "--thread-pid",
                    str(os.getpid()),
                    "--ready-marker",
                    str(marker),
                    "--interval-seconds",
                    "0.25",
                    "--samples",
                    "1",
                    "--physical",
                    "--output",
                    str(output),
                ),
                capture_output=True,
                text=True,
                timeout=5,
            )

            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["process_id"], os.getpid())
            self.assertEqual(report["samples"], 1)
            self.assertEqual(report["clock_domain"], "guest-monotonic")
            self.assertTrue(report["physical"])
            self.assertTrue(report["intervals"][0]["threads"])


if __name__ == "__main__":
    unittest.main()
