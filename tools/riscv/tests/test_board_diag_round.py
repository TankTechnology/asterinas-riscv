#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Guards for the board diagnostic round and its console-marker discipline."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

from tools.riscv.board import megrez_diag_round

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BOARD_DIRECTORY = REPOSITORY_ROOT / "tools/riscv/board"

# A (command, expected regex) pair, the shape board tooling uses to wait for a
# command to finish.
EXPECTATION_PAIR = re.compile(r'\("(?P<command>[^"\n]*)",\s*r"(?P<expect>[^"\n]*)"')
LITERAL_MARKER = re.compile(r"[A-Z][A-Z0-9_]{3,}")


class ConsoleMarkerTests(unittest.TestCase):
    """The console echoes what it is sent, so a marker must not echo itself."""

    def offenders(self, text: str) -> list[str]:
        found = []
        for match in EXPECTATION_PAIR.finditer(text):
            expect = match.group("expect")
            if not LITERAL_MARKER.fullmatch(expect):
                continue
            if re.search(rf"echo\s+{re.escape(expect)}\b", match.group("command")):
                found.append(match.group("command"))
        return found

    def test_no_expectation_matches_its_own_echo(self) -> None:
        for source in sorted(BOARD_DIRECTORY.glob("*.py")):
            with self.subTest(source=source.name):
                self.assertEqual(self.offenders(source.read_text()), [])

    def test_the_scan_rejects_the_marker_it_was_written_for(self) -> None:
        # Without this, a typo in the pattern would make the suite above pass
        # on every file no matter what it contains.
        text = '("sync; echo SYNCD", r"SYNCD", 180),'
        self.assertEqual(self.offenders(text), ["sync; echo SYNCD"])
        safe = '("M=SYN; sync; echo ${M}CD", r"SYNCD", 180),'
        self.assertEqual(self.offenders(safe), [])


class DiagnosticRoundTests(unittest.TestCase):
    def test_install_step_prepares_the_persistent_journal_offline(self) -> None:
        # journald only takes /var/log/journal when it exists as journald
        # starts; a later mkdir leaves the running instance in /run, and the
        # reset the round is waiting for throws that away.
        commands: list[str] = []

        def record(line: str, expect: str, timeout: float = 120.0, tries: int = 4):
            commands.append(line)
            return "recorded"

        with mock.patch.object(megrez_diag_round, "run", record):
            self.assertTrue(
                megrez_diag_round.install_helpers("/tmp/heartbeat", "/tmp/heartbeat.service")
            )

        journal_steps = [c for c in commands if "/var/log/journal" in c]
        self.assertTrue(journal_steps, commands)
        self.assertIn("chroot /mnt2", journal_steps[0])
        self.assertIn("M=SYN; sync; echo ${M}CD", commands)

    def test_journal_stage_reads_the_previous_boot(self) -> None:
        commands: list[str] = []

        def record(line: str, expect: str, timeout: float = 120.0, tries: int = 4):
            commands.append(line)
            return "recorded"

        with (
            mock.patch.object(megrez_diag_round, "run", record),
            mock.patch.object(megrez_diag_round, "park", lambda *a, **k: True),
            mock.patch.object(megrez_diag_round, "vendor_shell", lambda: True),
            mock.patch.object(megrez_diag_round, "mount_root", lambda: True),
        ):
            self.assertEqual(megrez_diag_round.journal(), 0)

        readback = [c for c in commands if "journalctl" in c]
        self.assertTrue(readback, commands)
        self.assertTrue(any("-b -1 -k" in c for c in readback), readback)
        self.assertTrue(any("-D /var/log/journal" in c for c in readback), readback)


if __name__ == "__main__":
    unittest.main()
