# SPDX-License-Identifier: MPL-2.0

"""Tests for the guest clock seed used before systemd targets.

The seed used to be the image build timestamp alone, which ages with the image:
a board without an RTC then boots months in the past and every certificate reads
"not yet valid". The bootstrap now prefers `/var/lib/asterinas/last-time`, the
newest time the machine itself has seen, and falls back to the build timestamp.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/megrez_clock_bootstrap.sh"

BUILD_TIME = "1704067200"


class MegrezClockBootstrapTests(unittest.TestCase):
    def _run(
        self,
        directory: Path,
        *,
        build_time: str | None,
        current: str,
        last_time: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        build_file = directory / "asterinas-build-time"
        if build_time is not None:
            build_file.write_text(f"{build_time}\n", encoding="utf-8")
        last_time_file = directory / "last-time"
        if last_time is not None:
            last_time_file.write_text(f"{last_time}\n", encoding="utf-8")
        log = directory / "date.log"
        fake_date = directory / "date"
        fake_date.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "+%s" ]; then\n'
            '    printf "%s\\n" "$CURRENT"\n'
            "    exit 0\n"
            "fi\n"
            'printf "%s\\n" "$*" >>"$DATE_LOG"\n',
            encoding="utf-8",
        )
        fake_date.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            ASTERINAS_BUILD_TIME_FILE=str(build_file),
            ASTERINAS_LAST_TIME_FILE=str(last_time_file),
            ASTERINAS_DATE_COMMAND=str(fake_date),
            CURRENT=current,
            DATE_LOG=str(log),
        )
        result = subprocess.run(
            ["/bin/sh", str(BOOTSTRAP)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result, log, build_file

    def test_far_future_boot_clock_is_replaced_by_build_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, _ = self._run(
                Path(temporary), build_time=BUILD_TIME, current="1776000000"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ASTERINAS_CLOCK_SEED_APPLIED", result.stdout)
            self.assertIn(f"seed={BUILD_TIME}", result.stdout)
            self.assertIn("source=build-time", result.stdout)
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                [f"--utc --set @{BUILD_TIME}"],
            )

    def test_plausible_clock_is_not_moved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, _ = self._run(
                Path(temporary), build_time=BUILD_TIME, current="1704067300"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ASTERINAS_CLOCK_SEED_SKIPPED", result.stdout)
            self.assertFalse(log.exists())

    def test_missing_build_time_is_reported_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, _ = self._run(
                Path(temporary), build_time=None, current="1776000000"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ASTERINAS_CLOCK_BUILD_TIME_MISSING", result.stdout)
            self.assertFalse(log.exists())

    def test_newer_last_time_wins_over_the_build_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, _ = self._run(
                Path(temporary),
                build_time=BUILD_TIME,
                current="1704067300",
                last_time="1795000000",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("source=last-time", result.stdout)
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                ["--utc --set @1795000000"],
            )

    def test_last_time_older_than_the_image_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, _ = self._run(
                Path(temporary),
                build_time=BUILD_TIME,
                current="1704067300",
                last_time="1600000000",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ASTERINAS_CLOCK_LAST_TIME_REJECTED", result.stdout)
            self.assertFalse(log.exists())

    def test_corrupt_last_time_is_reported_and_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, _ = self._run(
                Path(temporary),
                build_time=BUILD_TIME,
                current="1704067300",
                last_time="not-a-timestamp",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ASTERINAS_CLOCK_LAST_TIME_INVALID", result.stdout)
            self.assertFalse(log.exists())

    def test_last_time_past_the_sanity_upper_bound_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, _ = self._run(
                Path(temporary),
                build_time=BUILD_TIME,
                current="1704067300",
                last_time="4200000000",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ASTERINAS_CLOCK_LAST_TIME_REJECTED", result.stdout)
            self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
