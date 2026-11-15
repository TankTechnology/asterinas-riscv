# SPDX-License-Identifier: MPL-2.0

import errno
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).parents[1] / "diagnostics/ipc_event_probe.c"


class IpcEventProbeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cc"), "native C compiler is required")
    def test_linux_reference_closes_ipc_and_child_lifecycle(self):
        self.assertTrue(SOURCE.is_file(), "IPC reference probe is missing")
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "ipc_event_probe"
            compiled = subprocess.run(
                [
                    "cc",
                    "-std=gnu11",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    str(SOURCE),
                    "-o",
                    str(binary),
                ],
                timeout=30,
                capture_output=True,
                text=True,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run(
                [str(binary)], timeout=15, capture_output=True, text=True
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prefix = "IPC_DIAG "
        records = [
            json.loads(line[len(prefix) :])
            for line in result.stdout.splitlines()
            if line.startswith(prefix)
        ]
        by_phase = {record["phase"]: record for record in records}
        self.assertEqual(
            set(by_phase),
            {
                "empty_receive",
                "child_send",
                "epoll_ready",
                "rights_receive",
                "descriptor_read",
                "child_wait",
                "already_reaped",
                "complete",
            },
        )
        self.assertTrue(all(record["ok"] for record in records))
        self.assertTrue(all(record["physical"] is False for record in records))
        self.assertEqual(by_phase["rights_receive"]["result"], 1)
        self.assertEqual(by_phase["descriptor_read"]["result"], ord("F"))
        self.assertEqual(by_phase["child_wait"]["result"], 23)
        self.assertEqual(by_phase["already_reaped"]["errno"], errno.ECHILD)
        self.assertEqual(
            by_phase["child_send"]["pid"], by_phase["child_wait"]["peer_pid"]
        )
        self.assertEqual(by_phase["complete"]["case"], "unix_epoll_rights_wait")


if __name__ == "__main__":
    unittest.main()
