# SPDX-License-Identifier: MPL-2.0

import unittest

from tools.riscv.diagnostics import log_profile


def stats(count):
    return f"DurationStats {{ count: {count}, total_ticks: {count * 10}, max_ticks: {10 if count else 0}, invalid: 0 }}"


def transcript():
    lines = [f"LOG_PROFILE overhead frequency=1000000 stats={stats(128)}"]
    for rep in range(5):
        for level in (4, 7):
            for measured in (True, False):
                memory = 32 if measured else 0
                sent = 32 if measured and level == 7 else 0
                lines.append(
                    f"LOG_PROFILE batch rep={rep} level={level} measured={str(measured).lower()} records=32 "
                    f"elapsed={stats(1)} memory={stats(memory)} lock_wait={stats(sent)} "
                    f"locked_send={stats(sent)} attempted_bytes={sent * 100}"
                )
    lines.append("LOG_PROFILE end records=640 source=user")
    return "\n".join(lines)


class LogProfileTests(unittest.TestCase):
    def test_complete_measured_and_unmeasured_batches(self):
        result = log_profile.validate(transcript(), 0)
        self.assertEqual(result["frequency"], 1000000)
        self.assertEqual(len(result["batches"]), 20)

    def test_missing_duplicate_or_outside_phase(self):
        lines = transcript().splitlines()
        for invalid in (
            "\n".join(lines[:-1]),
            transcript() + "\n" + lines[-1],
            "\n".join(lines[:1] + lines[2:]),
            "\n".join(lines[:2] + [lines[1]] + lines[3:]),
            "\n".join(lines[1:] + lines[:1]),
        ):
            with self.subTest(invalid=invalid[:100]), self.assertRaises(ValueError):
                log_profile.validate(invalid, 0)

    def test_rejects_failures_and_invalid_statistics(self):
        for old, new in (
            ("invalid: 0", "invalid: 1"),
            ("frequency=1000000", "frequency=0"),
            ("count: 32", "count: 31"),
            ("max_ticks: 10", "max_ticks: 999999"),
            ("attempted_bytes=3200", "attempted_bytes=0"),
            ("rep=0", "rep=9"),
        ):
            with self.subTest(old=old), self.assertRaises(ValueError):
                log_profile.validate(transcript().replace(old, new, 1), 0)
        for exit_code in (1, 124, -9):
            with self.assertRaises(ValueError):
                log_profile.validate(transcript(), exit_code)
        with self.assertRaises(ValueError):
            log_profile.validate(transcript() + "\nAn uncaught panic occurred", 0)

    def test_missing_clock_is_not_zero_cost_success(self):
        with self.assertRaises(ValueError):
            log_profile.validate("LOG_PROFILE unavailable=clock", 0)

    def test_fatal_shutdown_without_backtrace_is_rejected(self):
        for fatal in (
            "The panic handler panicked",
            "Panicked in `PanicGuard`, aborting the system",
        ):
            with self.subTest(fatal=fatal), self.assertRaises(ValueError):
                log_profile.validate(transcript() + "\n" + fatal, 0)


if __name__ == "__main__":
    unittest.main()
