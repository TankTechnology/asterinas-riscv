#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""The board's resolver must not stay pointed at the QEMU slirp address."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOTFS = Path(__file__).parents[1] / "debian/rootfs"
SCRIPT = ROOTFS / "asterinas_resolv.sh"


def run(directory: Path, cmdline: str | None) -> subprocess.CompletedProcess[str]:
    """Run the script with `cmdline` standing in for /proc/cmdline.

    The script reads /proc/cmdline directly, so it is exercised through a
    small wrapper that shadows the file with a here-string.
    """

    resolv = directory / "resolv.conf"
    wrapper = directory / "run.sh"
    proc = directory / "cmdline"
    if cmdline is None:
        proc.write_text("", encoding="utf-8")
    else:
        proc.write_text(cmdline + "\n", encoding="utf-8")
    wrapper.write_text(
        "#!/bin/sh\n"
        f"RESOLV={resolv}\n"
        f"cp {SCRIPT} {directory}/script.sh\n"
        # Re-point the script's two fixed paths at the sandbox.
        f"sed -e 's#/proc/cmdline#{proc}#' -e 's#/etc/resolv.conf#{resolv}#' "
        f"{SCRIPT} > {directory}/script.sh\n"
        f"exec /bin/sh {directory}/script.sh\n",
        encoding="utf-8",
    )
    os.chmod(wrapper, 0o755)
    return subprocess.run(
        ["/bin/sh", str(wrapper)], check=False, capture_output=True, text=True
    )


class ResolvTests(unittest.TestCase):
    def test_the_board_replaces_the_slirp_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "resolv.conf").write_text(
                "nameserver 10.0.2.3\n", encoding="utf-8"
            )
            result = run(
                directory,
                "console=ttyS0 asterinas.net=eic7700-rj45,192.0.2.10/24,192.0.2.1",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ASTERINAS_RESOLV_SET", result.stdout)
            resolv = (directory / "resolv.conf").read_text()
            self.assertNotIn("10.0.2.3", resolv)
            self.assertIn("nameserver 114.114.114.114", resolv)

    def test_a_qemu_boot_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "resolv.conf").write_text(
                "nameserver 10.0.2.3\n", encoding="utf-8"
            )
            result = run(directory, "console=hvc0 root=/dev/vda")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("ASTERINAS_RESOLV_SET", result.stdout)
            self.assertIn("10.0.2.3", (directory / "resolv.conf").read_text())

    def test_a_hand_written_resolver_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "resolv.conf").write_text(
                "nameserver 10.100.19.1\n", encoding="utf-8"
            )
            result = run(
                directory,
                "asterinas.net=eic7700-rj45,192.0.2.10/24,192.0.2.1",
            )

            self.assertNotIn("ASTERINAS_RESOLV_SET", result.stdout)
            self.assertIn("10.100.19.1", (directory / "resolv.conf").read_text())


if __name__ == "__main__":
    unittest.main()
