#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Compile and exercise the real native scheduler handoff probe."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).with_name("sched_handoff.c")
        cls.build_dir = tempfile.TemporaryDirectory(prefix="sched-handoff-test-")
        cls.addClassCleanup(cls.build_dir.cleanup)
        cls.executable = str(Path(cls.build_dir.name) / "sched_handoff")
        if source.is_file():
            result = subprocess.run(
                ["cc", "-O2", "-pthread", "-Wall", "-Wextra", "-Werror",
                 str(source), "-o", cls.executable],
                capture_output=True, text=True, check=False,
            )
            if result.returncode:
                raise AssertionError(f"probe build failed:\n{result.stderr}")
        cls.cpus = sorted(cpu for cpu in os.sched_getaffinity(0) if cpu < 1024)
        if not cls.cpus:
            raise AssertionError("no CPU representable by the probe's cpu_set_t")

    def run_probe(self, *arguments):
        self.assertTrue(Path(self.executable).is_file(), "missing scheduler handoff probe")
        return subprocess.run(
            [self.executable, *map(str, arguments)],
            capture_output=True, text=True, check=False, timeout=25,
        )

    def assert_rejected(self, *arguments):
        result = self.run_probe(*arguments)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "", result.stdout)
        self.assertTrue(result.stderr, "failures must include a diagnostic")
        return result

    def assert_measurement(self, mode, cpu_a, cpu_b, iterations=100):
        result = self.run_probe(mode, cpu_a, cpu_b, iterations)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        record = json.loads(result.stdout)
        expected = {
            "mode": mode, "cpu_a": cpu_a, "cpu_b": cpu_b,
            "confirmed_cpu_a": cpu_a, "confirmed_cpu_b": cpu_b,
            "policy": "SCHED_OTHER", "iterations": iterations,
            "completed": iterations, "samples": iterations,
        }
        for key, value in expected.items():
            self.assertIs(type(record[key]), type(value), key)
            self.assertEqual(record[key], value, key)
        for key in ("elapsed_ns", "min_ns", "p50_ns", "p95_ns", "p99_ns", "max_ns"):
            self.assertIs(type(record[key]), int, key)
            self.assertGreaterEqual(record[key], 0, key)
        self.assertGreater(record["elapsed_ns"], 0)
        quantiles = [record[key] for key in ("min_ns", "p50_ns", "p95_ns", "p99_ns", "max_ns")]
        self.assertEqual(quantiles, sorted(quantiles))
        self.assertGreaterEqual(record["elapsed_ns"], record["max_ns"])
        return record

    def test_missing_arguments(self):
        self.assert_rejected()
        self.assert_rejected("yield", self.cpus[0], self.cpus[0])

    def test_invalid_mode(self):
        self.assert_rejected("spin", self.cpus[0], self.cpus[0], 100)

    def test_invalid_cpus(self):
        for cpu in (-1, 1024, "x", "1x", "999999999999999999999999"):
            for position in (0, 1):
                with self.subTest(cpu=cpu, position=position):
                    cpus = [self.cpus[0], self.cpus[0]]
                    cpus[position] = cpu
                    self.assert_rejected("yield", *cpus, 100)

    def test_invalid_iterations(self):
        for iterations in (0, -1, 100001, "x", "1x", "999999999999999999999999"):
            with self.subTest(iterations=iterations):
                self.assert_rejected("blocking", self.cpus[0], self.cpus[0], iterations)

    def test_unavailable_affinity(self):
        # Use an offline CPU, since an inherited taskset mask can be expanded.
        # CPU IDs need not be contiguous, so a processor count is insufficient.
        online = set()
        for cpu_range in Path("/sys/devices/system/cpu/online").read_text().strip().split(","):
            bounds = [int(value) for value in cpu_range.split("-")]
            online.update(range(bounds[0], bounds[-1] + 1))
        unavailable = sorted(set(range(1024)) - online)
        if not unavailable:
            self.skipTest("no known unavailable CPU within cpu_set_t")
        cpu = unavailable[0]
        for cpus in ((cpu, self.cpus[0]), (self.cpus[0], cpu)):
            with self.subTest(cpus=cpus):
                result = self.assert_rejected("blocking", *cpus, 100)
                self.assertIn("affinity", result.stderr)

    def test_same_cpu_yield(self):
        self.assert_measurement("yield", self.cpus[0], self.cpus[0])

    def test_same_cpu_blocking(self):
        self.assert_measurement("blocking", self.cpus[0], self.cpus[0])

    def test_distinct_cpu_blocking(self):
        if len(self.cpus) < 2:
            self.skipTest("two available CPUs required")
        self.assert_measurement("blocking", self.cpus[0], self.cpus[1])

    def test_one_iteration_quantiles(self):
        for mode in ("yield", "blocking"):
            with self.subTest(mode=mode):
                record = self.assert_measurement(mode, self.cpus[0], self.cpus[0], 1)
                for key in ("p50_ns", "p95_ns", "p99_ns", "max_ns"):
                    self.assertEqual(record[key], record["min_ns"])

    def test_watchdog_bounds_blocked_output(self):
        stderr = self.assert_watchdog_bounds_blocked_output(subprocess.PIPE)
        self.assertEqual(stderr, "")

    def test_watchdog_bounds_blocked_combined_output(self):
        self.assert_watchdog_bounds_blocked_output(subprocess.STDOUT)

    def assert_watchdog_bounds_blocked_output(self, stderr_destination):
        read_fd, write_fd = os.pipe()
        try:
            # Keep a reader open but fill the pipe so the result cannot flush.
            os.set_blocking(write_fd, False)
            try:
                while True:
                    os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                pass
            os.set_blocking(write_fd, True)

            with subprocess.Popen(
                [self.executable, "blocking", str(self.cpus[0]), str(self.cpus[0]), "1"],
                stdout=write_fd, stderr=stderr_destination, text=True,
            ) as process:
                try:
                    _, stderr = process.communicate(timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    self.fail("process watchdog did not bound blocked stdout")
                self.assertEqual(process.returncode, 124, stderr)
                return stderr
        finally:
            os.close(write_fd)
            os.close(read_fd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
