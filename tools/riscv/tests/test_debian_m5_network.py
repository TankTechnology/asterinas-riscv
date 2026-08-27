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

from tools.riscv.debian.rootfs.desktop_m4_gate import DESKTOP_M4_MILESTONES
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_NETWORK_MILESTONES,
    DESKTOP_M5_QEMU_MILESTONES,
    classify_desktop_m5_network,
    classify_desktop_m5_qemu,
)
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import (
    DESKTOP_M5_QEMU_BOOTARGS,
    DesktopM5QemuOperations,
    desktop_m5_qemu_argv,
)
from tools.riscv.debian.rootfs.contract import ContractError, load_manifest
from tools.riscv.debian.rootfs.profiles import get_profile


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BUILD_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
MAKEFILE = REPOSITORY_ROOT / "Makefile"
EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh"
)
EXPECTED_MEGREZ_MILESTONES = (
    "DEBIAN_NETWORK_M5_LINK interface=eth0 address=10.100.19.200/21 state=lower-up",
    "DEBIAN_NETWORK_M5_GUEST_PING peer=10.100.19.216 count=10",
    "DEBIAN_NETWORK_M5_MEGREZ_DNS resolver=10.2.0.5 fallback=10.2.0.6 host=www.baidu.com",
    "DEBIAN_NETWORK_M5_MEGREZ_HTTPS host=www.baidu.com status=200 address=10.100.19.200",
    "DEBIAN_NETWORK_M5_MEGREZ_ASSET host=www.baidu.com resource=logo-png",
    "DEBIAN_NETWORK_M5_MEGREZ_READY mode=static-rj45",
)
LEGACY_PHYSICAL_MILESTONES = (
    EXPECTED_MEGREZ_MILESTONES[0],
    EXPECTED_MEGREZ_MILESTONES[1],
    "DEBIAN_NETWORK_M5_READY interface=eth0",
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
            tuple(
                sorted(
                    m4.requested_packages
                    + ("curl", "iproute2", "iputils-ping", "xdotool")
                )
            ),
        )
        self.assertEqual(
            m5.identity_packages,
            m4.identity_packages + ("curl", "iproute2", "iputils-ping", "xdotool"),
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
        self.assertTrue(
            (stage / "usr/lib/asterinas/desktop-m6-browser-evidence").is_file()
        )
        self.assertTrue(
            (stage / "usr/share/asterinas/desktop-m6-javascript.html").is_file()
        )
        unit = stage / "etc/systemd/system/asterinas-desktop-m5-network.service"
        self.assertIn(
            "ExecStart=/usr/lib/asterinas/desktop-m5-network-evidence",
            unit.read_text(),
        )
        self.assertIn("Before=asterinas-desktop-m4.service", unit.read_text())
        self.assertNotIn(
            "After=asterinas-desktop-m4-evidence.service", unit.read_text()
        )
        self.assertTrue(
            (
                stage / "etc/systemd/system/graphical.target.wants" / unit.name
            ).is_symlink()
        )
        browser_unit = stage / "etc/systemd/system/asterinas-desktop-m6-browser.service"
        browser_unit_text = browser_unit.read_text(encoding="utf-8")
        self.assertIn("After=asterinas-desktop-m5-network.service", browser_unit_text)
        self.assertIn("asterinas-desktop-m4-evidence.service", browser_unit_text)
        self.assertTrue(
            (
                stage / "etc/systemd/system/graphical.target.wants" / browser_unit.name
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
            console.read_text().splitlines(), list(LEGACY_PHYSICAL_MILESTONES)
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

    def test_qemu_evidence_uses_dns_and_https_without_ip_or_ping(self) -> None:
        console = self.directory / "qemu-console"
        console.write_text("", encoding="utf-8")
        cmdline = self.directory / "cmdline"
        cmdline.write_text(
            "console=ttyS0 asterinas.debian_network=qemu-slirp\n",
            encoding="utf-8",
        )
        resolv_conf = self.directory / "resolv.conf"
        url_file = self.directory / "desktop-url"
        fake_bin = self.directory / "qemu-bin"
        fake_bin.mkdir()
        curl_log = self.directory / "curl.log"
        for name, body in {
            "getent": "#!/bin/sh\nprintf '%s\\n' '110.242.68.66 STREAM www.baidu.com'\n",
            "curl": "#!/bin/sh\nprintf '%s\\n' \"$*\" >\"$ASTERINAS_M5_CURL_LOG\"\nprintf '200\\t10.0.2.15'\n",
            "ip": "#!/bin/sh\nexit 97\n",
            "ping": "#!/bin/sh\nexit 98\n",
        }.items():
            executable = fake_bin / name
            executable.write_text(body, encoding="utf-8")
            executable.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_DESKTOP_M5_CONSOLE=str(console),
            ASTERINAS_DESKTOP_M5_CMDLINE_PATH=str(cmdline),
            ASTERINAS_DESKTOP_M5_RESOLV_CONF=str(resolv_conf),
            ASTERINAS_DESKTOP_M5_URL_FILE=str(url_file),
            ASTERINAS_M5_CURL_LOG=str(curl_log),
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
            console.read_text().splitlines(), list(DESKTOP_M5_QEMU_MILESTONES)
        )
        self.assertEqual(resolv_conf.read_text(), "nameserver 10.0.2.3\n")
        self.assertEqual(
            url_file.read_text(),
            "https://www.baidu.com/img/flexible/logo/pc/result.png\n",
        )
        self.assertIn("https://www.baidu.com/", curl_log.read_text())

    def test_qemu_classifier_and_adapter_bind_network_before_desktop(self) -> None:
        transcript = (
            "\n".join((*DESKTOP_M5_QEMU_MILESTONES, *DESKTOP_M4_MILESTONES)) + "\n"
        ).encode()

        result = classify_desktop_m5_qemu(transcript, expected_debian_release="13.6")

        self.assertTrue(result.passed, result.reason)
        reversed_result = classify_desktop_m5_qemu(
            "\n".join(
                reversed((*DESKTOP_M5_QEMU_MILESTONES, *DESKTOP_M4_MILESTONES))
            ).encode(),
            expected_debian_release="13.6",
        )
        self.assertEqual(reversed_result.reason, "desktop milestones out of order")
        self.assertIn("asterinas.debian_network=qemu-slirp", DESKTOP_M5_QEMU_BOOTARGS)
        self.assertEqual(DesktopM5QemuOperations.SCHEMA_VERSION, 5)
        self.assertEqual(DesktopM5QemuOperations.PROFILE_NAME, "desktop-m5-network")

    def test_qemu_adapter_adds_only_one_slirp_virtio_net_device(self) -> None:
        for name in ("u-boot", "boot.ext4", "root.ext2"):
            (self.directory / name).write_bytes(name.encode())

        arguments = desktop_m5_qemu_argv(
            uboot=self.directory / "u-boot",
            boot_disk=self.directory / "boot.ext4",
            root_disk=self.directory / "root.ext2",
            monitor_socket=self.directory / "monitor.sock",
        )

        self.assertNotIn("-nic", arguments)
        self.assertEqual(arguments.count("user,id=net0"), 1)
        self.assertEqual(arguments.count("virtio-net-device,netdev=net0"), 1)
        for device in (
            "bochs-display",
            "virtio-keyboard-device",
            "virtio-tablet-device",
        ):
            self.assertIn(device, arguments)

    def test_qemu_make_gate_allows_the_cold_desktop_to_finish(self) -> None:
        target = (
            MAKEFILE.read_text(encoding="utf-8")
            .split(".PHONY: test_riscv_debian_desktop_m5_qemu_gate", 1)[1]
            .split(".PHONY:", 1)[0]
        )

        self.assertIn("--boot-timeout 300", target)

    def test_classifier_requires_order_and_scans_complete_transcript(self) -> None:
        self.assertEqual(DESKTOP_M5_NETWORK_MILESTONES, EXPECTED_MEGREZ_MILESTONES)
        transcript = "\n".join(EXPECTED_MEGREZ_MILESTONES).encode()
        self.assertTrue(
            classify_desktop_m5_network(
                transcript,
                expected_debian_release="13.6",
            ).passed
        )
        reordered = "\n".join(reversed(EXPECTED_MEGREZ_MILESTONES)).encode()
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
        for marker in EXPECTED_MEGREZ_MILESTONES[2:5]:
            with self.subTest(missing=marker):
                missing = transcript.replace(marker.encode(), b"")
                self.assertEqual(
                    classify_desktop_m5_network(
                        missing,
                        expected_debian_release="13.6",
                    ).reason,
                    f"missing desktop milestone: {marker}",
                )
            with self.subTest(duplicate=marker):
                duplicate = transcript + b"\n" + marker.encode()
                self.assertEqual(
                    classify_desktop_m5_network(
                        duplicate,
                        expected_debian_release="13.6",
                    ).reason,
                    "duplicate desktop milestone",
                )


if __name__ == "__main__":
    unittest.main()
