#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_NETWORK_MILESTONES,
    classify_desktop_m5_network,
)
from tools.riscv.debian.rootfs.contract import ContractError, load_manifest
from tools.riscv.debian.rootfs.profiles import get_profile


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BUILD_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh"
)


class DebianDesktopM5NetworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_profile_extends_m4_with_exact_network_identity(self) -> None:
        m4 = get_profile("desktop-m4")
        m5 = get_profile("desktop-m5-network")

        self.assertEqual(m5.schema_version, 5)
        self.assertEqual(m5.root_label, "ASTER_DEBIANM5")
        self.assertEqual(m5.root_uuid, "182e1ea4-296d-5383-8bcb-ea67e40db074")
        self.assertEqual(
            m5.requested_packages,
            tuple(sorted(m4.requested_packages + ("iproute2", "iputils-ping"))),
        )
        self.assertEqual(
            m5.identity_packages,
            m4.identity_packages + ("iproute2", "iputils-ping"),
        )

    def test_manifest_parser_accepts_only_the_m5_profile_for_schema_five(self) -> None:
        profile = get_profile("desktop-m5-network")
        payload = {
            "schema_version": 5,
            "profile": profile.name,
            "suite": "trixie",
            "debian_release": "13.6",
            "mirror_url": "https://deb.debian.org/debian",
            "architecture": "riscv64",
            "signed_metadata": {
                "url": "https://deb.debian.org/debian/dists/trixie/InRelease",
                "sha256": "0" * 64,
            },
            "packages_lock_sha256": "0" * 64,
            "downloaded_packages": [
                {
                    "name": "base-files",
                    "architecture": "riscv64",
                    "version": "13.8+deb13u1",
                    "sha256": "0" * 64,
                }
            ],
            "filesystem": {
                "type": "ext2",
                "label": profile.root_label,
                "uuid": profile.root_uuid,
                "size_bytes": 1024 * 1024 * 1024,
                "block_size_bytes": 4096,
            },
            "tool_versions": {"builder": "test"},
            "build_timestamp": "2026-08-27T00:00:00Z",
            "root_image_sha256": "0" * 64,
            "gate_packages": {name: "1" for name in profile.identity_packages},
        }
        manifest_path = self.directory / "m5-manifest.json"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        manifest = load_manifest(manifest_path)

        self.assertEqual(manifest.schema_version, 5)
        self.assertEqual(manifest.profile, "desktop-m5-network")
        payload["profile"] = "desktop-m4"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "profile schema version"):
            load_manifest(manifest_path)

    def test_builder_prints_m5_packages_without_build_tools(self) -> None:
        result = subprocess.run(
            [
                "/bin/bash",
                str(BUILD_SCRIPT),
                "--profile",
                "desktop-m5-network",
                "--print-packages",
            ],
            cwd=self.directory,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            tuple(result.stdout.splitlines()),
            get_profile("desktop-m5-network").requested_packages,
        )

    def test_builder_installs_m4_desktop_and_m5_network_evidence(self) -> None:
        work_directory = self.directory / "configure m5"
        stage = work_directory / "stage"
        for relative in (
            "etc",
            "etc/systemd/system",
            "home",
            "usr/bin",
            "var/lib/dbus",
            "var/lib/dpkg",
            "var/cache/apt/archives",
            "var/lib/apt/lists",
            "var/log",
            "tmp",
            "var/tmp",
        ):
            (stage / relative).mkdir(parents=True, exist_ok=True)
        (stage / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/bash\n")
        (stage / "etc/group").write_text("root:x:0:\n")
        (stage / "etc/shadow").write_text("root:!:0:0:99999:7:::\n")
        (stage / "etc/gshadow").write_text("root:!::\n")

        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                """source "$1"
PROFILE=desktop-m5-network
configure_profile 1
WORK_DIR="$2"
configure_and_normalize_rootfs
""",
                "builder-configure-m5-test",
                str(BUILD_SCRIPT),
                str(work_directory),
            ],
            cwd=self.directory,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((stage / "usr/lib/asterinas/desktop-m4-session").is_file())
        installed = stage / "usr/lib/asterinas/desktop-m5-network-evidence"
        self.assertEqual(installed.read_bytes(), EVIDENCE_SCRIPT.read_bytes())
        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o755)
        unit = stage / "etc/systemd/system/asterinas-desktop-m5-network.service"
        self.assertIn(
            "ExecStart=/usr/lib/asterinas/desktop-m5-network-evidence",
            unit.read_text(),
        )
        self.assertTrue(
            (
                stage / "etc/systemd/system/graphical.target.wants" / unit.name
            ).is_symlink()
        )

    def _fake_network_tools(self, *, address: str) -> tuple[Path, Path]:
        bin_directory = self.directory / "bin"
        bin_directory.mkdir(exist_ok=True)
        ping_log = self.directory / "ping.log"
        ip = bin_directory / "ip"
        ip.write_text(
            f"""#!/bin/sh
if [ "$1 $2 $3 $4 $5" = "-o link show dev eth0" ]; then
    printf '%s\n' '2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP'
    exit 0
fi
if [ "$1 $2 $3 $4 $5 $6 $7 $8" = "-o -4 addr show dev eth0 scope global" ]; then
    printf '%s\n' '2: eth0    inet {address} brd 10.100.23.255 scope global eth0'
    exit 0
fi
exit 2
""",
            encoding="utf-8",
        )
        ping = bin_directory / "ping"
        ping.write_text(
            """#!/bin/sh
printf '%s\n' "$*" >"$ASTERINAS_M5_PING_LOG"
exit 0
""",
            encoding="utf-8",
        )
        ip.chmod(0o755)
        ping.chmod(0o755)
        return bin_directory, ping_log

    def test_guest_evidence_requires_link_address_and_ten_pings(self) -> None:
        console = self.directory / "console"
        console.write_text("", encoding="utf-8")
        fake_bin, ping_log = self._fake_network_tools(address="10.100.19.200/21")
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_DESKTOP_M5_CONSOLE=str(console),
            ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS="0",
            ASTERINAS_M5_PING_LOG=str(ping_log),
        )

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            console.read_text().splitlines(), list(DESKTOP_M5_NETWORK_MILESTONES)
        )
        self.assertEqual(ping_log.read_text().strip(), "-n -c 10 -W 2 10.100.19.216")

    def test_guest_evidence_fails_once_for_wrong_address(self) -> None:
        console = self.directory / "bad-console"
        console.write_text("", encoding="utf-8")
        fake_bin, ping_log = self._fake_network_tools(address="10.100.19.201/21")
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_DESKTOP_M5_CONSOLE=str(console),
            ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS="0",
            ASTERINAS_M5_PING_LOG=str(ping_log),
        )

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            console.read_text().splitlines(),
            ["DEBIAN_NETWORK_M5_FAIL reason=link-or-address-timeout"],
        )
        self.assertFalse(ping_log.exists())

    def test_classifier_requires_order_and_scans_complete_transcript(self) -> None:
        transcript = "\n".join(DESKTOP_M5_NETWORK_MILESTONES).encode()
        self.assertTrue(
            classify_desktop_m5_network(
                transcript,
                expected_debian_release="13.6",
            ).passed
        )
        reordered = "\n".join(reversed(DESKTOP_M5_NETWORK_MILESTONES)).encode()
        self.assertEqual(
            classify_desktop_m5_network(
                reordered,
                expected_debian_release="13.6",
            ).reason,
            "desktop milestones out of order",
        )
        fatal_after_ready = transcript + b"\nKernel panic - not syncing"
        self.assertEqual(
            classify_desktop_m5_network(
                fatal_after_ready,
                expected_debian_release="13.6",
            ).reason,
            "kernel panic",
        )


if __name__ == "__main__":
    unittest.main()
