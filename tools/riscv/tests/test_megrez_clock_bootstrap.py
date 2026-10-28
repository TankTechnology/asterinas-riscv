# SPDX-License-Identifier: MPL-2.0

"""Tests for the build-time guest clock seed used before systemd targets."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/megrez_clock_bootstrap.sh"


class MegrezClockBootstrapTests(unittest.TestCase):
    def _run(
        self, directory: Path, *, build_time: str | None, current: str
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        build_file = directory / "asterinas-build-time"
        if build_time is not None:
            build_file.write_text(f"{build_time}\n", encoding="utf-8")
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
                Path(temporary), build_time="1704067200", current="1776000000"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ASTERINAS_CLOCK_BUILD_TIME_APPLIED", result.stdout)
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                ["--utc --set @1704067200"],
            )

    def test_plausible_clock_is_not_moved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, _ = self._run(
                Path(temporary), build_time="1704067200", current="1704067300"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ASTERINAS_CLOCK_BUILD_TIME_SKIPPED", result.stdout)
            self.assertFalse(log.exists())

    def test_missing_build_time_is_reported_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, log, _ = self._run(
                Path(temporary), build_time=None, current="1776000000"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ASTERINAS_CLOCK_BUILD_TIME_MISSING", result.stdout)
            self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
