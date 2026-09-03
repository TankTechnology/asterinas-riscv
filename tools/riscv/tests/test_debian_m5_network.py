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

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DESKTOP_M4_CORE_MILESTONES,
    DESKTOP_M4_MILESTONES,
)
from tools.riscv.debian.rootfs.desktop_m3_gate import _UBOOT_COMMAND_SAFE_LIMIT
from tools.riscv.debian.rootfs import desktop_m5_network_gate as network_gate
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_NETWORK_MILESTONES,
    DESKTOP_M5_QEMU_MILESTONES,
    classify_desktop_m5_network,
    classify_desktop_m5_qemu,
    classify_network_m5_qemu,
)
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import (
    DESKTOP_M5_QEMU_BOOTARGS,
    DesktopM5QemuOperations,
    NetworkM5QemuOperations,
    QemuExpectedFailure,
    QemuGateTarget,
    _parse_target,
    classify_qemu_web_network,
    desktop_m5_qemu_argv,
    qemu_web_network_bootargs,
)
from tools.riscv.debian.rootfs.contract import ContractError, load_manifest
from tools.riscv.debian.rootfs.profiles import get_profile
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig as RootfsGateConfig
from tools.riscv.megrez_network_fixture import (
    BROWSER_DOWNLOAD,
    BROWSER_DOWNLOAD_PATH,
    BROWSER_DOWNLOAD_SHA256,
    FIXTURE_PATH,
    PAYLOAD,
    PAYLOAD_SHA256,
    PAYLOAD_SIZE,
    FixtureConfig,
    FixtureServer,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BUILD_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
MAKEFILE = REPOSITORY_ROOT / "Makefile"
EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh"
)
DESKTOP_M4_SESSION_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m4_session.sh"
)
DESKTOP_M4_EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m4_evidence.sh"
)
SAFE_REBOOT_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/megrez_safe_reboot.sh"
MEGREZ_TCP_PROBE_SOURCE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/megrez_tcp_probe_init.c"
)
EXPECTED_MEGREZ_MILESTONES = (
    "DEBIAN_NETWORK_M5_LINK interface=eth0 address=10.100.19.200/21 state=lower-up",
    "DEBIAN_NETWORK_M5_MEGREZ_PROXY endpoint=10.100.19.216:17893",
    "DEBIAN_NETWORK_M5_STRESS requests=20 bytes=1310720 "
    f"sha256={PAYLOAD_SHA256} endpoint=10.100.19.216:17894",
    "DEBIAN_NETWORK_M5_CLOCK source=http-date proxy=10.100.19.216:17893",
    "DEBIAN_NETWORK_M5_MEGREZ_HTTPS host=www.baidu.com status=200 address=10.100.19.200 proxy=10.100.19.216:17893",
    "DEBIAN_NETWORK_M5_MEGREZ_ASSET host=www.baidu.com resource=logo-png proxy=10.100.19.216:17893",
    "DEBIAN_NETWORK_M5_MEGREZ_READY mode=static-rj45-host-proxy",
)
EXPECTED_MEGREZ_CONSOLE = (
    EXPECTED_MEGREZ_MILESTONES[:2]
    + ("DEBIAN_NETWORK_M5_STRESS_START requests=20 endpoint=10.100.19.216:17894",)
    + EXPECTED_MEGREZ_MILESTONES[2:]
)
EXPECTED_QEMU_STRESS_MILESTONE = (
    "DEBIAN_NETWORK_M5_STRESS requests=20 bytes=1310720 "
    f"sha256={PAYLOAD_SHA256} endpoint=10.0.2.2:17894"
)


class DebianDesktopM5NetworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def _web_network_transcript(self, mode: object) -> bytes:
        layers = getattr(network_gate, "NETWORK_LAYERS", ())
        records = [
            f"DEBIAN_WEB_NETWORK_LAYER mode={mode.value} "
            f"layer={layer} status=pass"
            for layer in layers
        ]
        records.append(
            f"DEBIAN_WEB_NETWORK_READY mode={mode.value} layers={len(layers)}"
        )
        return ("\n".join(records) + "\n").encode()

    def test_web_network_modes_are_isolated(self) -> None:
        self.assertTrue(hasattr(network_gate, "NetworkMode"))
        self.assertTrue(hasattr(network_gate, "classify_web_network"))
        proxy = network_gate.NetworkMode.PROXY
        direct = network_gate.NetworkMode.DIRECT
        proxy_transcript = self._web_network_transcript(proxy)
        direct_transcript = self._web_network_transcript(direct)

        self.assertTrue(
            network_gate.classify_web_network(
                proxy_transcript, mode=proxy
            ).passed
        )
        self.assertTrue(
            network_gate.classify_web_network(
                direct_transcript, mode=direct
            ).passed
        )
        self.assertFalse(
            network_gate.classify_web_network(
                proxy_transcript, mode=direct
            ).passed
        )
        self.assertFalse(
            network_gate.classify_web_network(
                direct_transcript, mode=proxy
            ).passed
        )

        mixed = proxy_transcript + (
            "DEBIAN_WEB_NETWORK_READY mode=direct layers=10\n"
        ).encode()
        self.assertEqual(
            network_gate.classify_web_network(mixed, mode=proxy).reason,
            "mixed web network modes",
        )

    def test_web_network_layers_are_unique_and_ordered(self) -> None:
        self.assertTrue(hasattr(network_gate, "NetworkMode"))
        self.assertTrue(hasattr(network_gate, "NETWORK_LAYERS"))
        mode = network_gate.NetworkMode.PROXY
        transcript = self._web_network_transcript(mode)
        layers = network_gate.NETWORK_LAYERS
        self.assertEqual(
            layers,
            (
                "link",
                "address",
                "neighbor",
                "reachability",
                "dns",
                "http",
                "https",
                "baidu-asset",
                "repeat",
                "medium",
            ),
        )

        marker = (
            "DEBIAN_WEB_NETWORK_LAYER mode=proxy layer=neighbor status=pass"
        ).encode()
        self.assertEqual(
            network_gate.classify_web_network(
                transcript.replace(marker, b""), mode=mode
            ).reason,
            "missing or duplicate neighbor layer",
        )
        self.assertEqual(
            network_gate.classify_web_network(
                transcript + marker + b"\n", mode=mode
            ).reason,
            "missing or duplicate neighbor layer",
        )
        first = (
            "DEBIAN_WEB_NETWORK_LAYER mode=proxy layer=link status=pass"
        ).encode()
        second = (
            "DEBIAN_WEB_NETWORK_LAYER mode=proxy layer=address status=pass"
        ).encode()
        reordered = transcript.replace(first, b"__FIRST__").replace(
            second, first
        ).replace(b"__FIRST__", second)
        self.assertEqual(
            network_gate.classify_web_network(reordered, mode=mode).reason,
            "web network layers out of order",
        )

    def test_web_network_failure_is_layer_qualified(self) -> None:
        self.assertTrue(hasattr(network_gate, "NetworkMode"))
        mode = network_gate.NetworkMode.DIRECT
        transcript = (
            b"DEBIAN_WEB_NETWORK_FAIL mode=direct layer=https "
            b"reason=certificate-verify\n"
        )
        result = network_gate.classify_web_network(transcript, mode=mode)
        self.assertFalse(result.passed)
        self.assertEqual(
            result.reason,
            "web network https failure: certificate-verify",
        )

        unknown_layer = (
            b"DEBIAN_WEB_NETWORK_FAIL mode=direct layer=magic reason=broken\n"
        )
        self.assertEqual(
            network_gate.classify_web_network(
                unknown_layer, mode=mode
            ).reason,
            "web network failure has unknown layer",
        )
        config_failure = (
            b"DEBIAN_WEB_NETWORK_FAIL mode=direct layer=config "
            b"reason=proxy-present\n"
        )
        self.assertEqual(
            network_gate.classify_web_network(
                config_failure, mode=mode
            ).reason,
            "web network config failure: proxy-present",
        )
        wrong_mode = (
            b"DEBIAN_WEB_NETWORK_FAIL mode=proxy layer=https reason=broken\n"
        )
        self.assertEqual(
            network_gate.classify_web_network(wrong_mode, mode=mode).reason,
            "mixed web network modes",
        )

    def test_native_megrez_tcp_probe_validates_exact_http_response(self) -> None:
        executable = self.directory / "megrez-tcp-probe-self-test"
        compile_result = subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-DMEGREZ_TCP_PROBE_SELF_TEST",
                str(MEGREZ_TCP_PROBE_SOURCE),
                "-o",
                str(executable),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr)

        result = subprocess.run(
            [str(executable)], check=False, capture_output=True, text=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            (
                "ASTERINAS_GMAC_TCP_PROBE_FAIL "
                "reason=receive-poll errno=110 attempts=1 "
                "current_bytes=14600 completed_bytes=0\n"
                "MEGREZ_TCP_PROBE_SELF_TEST PASS\n"
                "MEGREZ_TCP_STRESS_SELF_TEST PASS "
                "sizes=16384,65536,1048576,16777216 pattern=mod251\n"
                "MEGREZ_TCP_TIMING_SELF_TEST PASS "
                "deadline_ms=45000 recovery_margin_ms=15000\n"
            ),
        )

    def test_guest_network_deadline_matches_the_qemu_contract(self) -> None:
        self.assertIn(
            'readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS:-120}"',
            EVIDENCE_SCRIPT.read_text(encoding="utf-8"),
        )

    def test_desktop_core_contract_excludes_browser(self) -> None:
        self.assertEqual(
            DESKTOP_M4_CORE_MILESTONES,
            (
                "DEBIAN_DESKTOP_M4_UDEV state=active",
                "DEBIAN_DESKTOP_M4_LOGIND state=active",
                "DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1",
                "DEBIAN_DESKTOP_M4_INPUT keyboard=evdev pointer=evdev",
                "DEBIAN_DESKTOP_M4_XORG framebuffer=fbdev display=:0",
                "DEBIAN_DESKTOP_M4_SHELL wallpaper=asterinas "
                "desktop=pcmanfm panel=lxpanel launchers=3",
                "DEBIAN_DESKTOP_M4_CORE_CLIENTS window-manager=openbox "
                "file-manager=pcmanfm panel=lxpanel terminal=xterm",
                "DEBIAN_DESKTOP_M4_CORE_READY user=asterinas display=:0",
            ),
        )
        self.assertNotIn("browser", " ".join(DESKTOP_M4_CORE_MILESTONES).lower())
        self.assertNotIn("netsurf", " ".join(DESKTOP_M4_CORE_MILESTONES).lower())

    def test_desktop_session_validates_browser_mode_before_launch(self) -> None:
        session = DESKTOP_M4_SESSION_SCRIPT.read_text(encoding="utf-8")

        validation = (
            '[[ "$BROWSER_ENABLED" == 0 || "$BROWSER_ENABLED" == 1 ]] || {\n'
            "    printf '%s\\n' 'invalid ASTERINAS_DESKTOP_BROWSER_ENABLED' >&2\n"
            "    exit 64\n"
            "}"
        )
        self.assertIn(validation, session)
        self.assertLess(
            session.index(validation), session.index('if [[ "${1-}" == --xsession ]]')
        )

    def test_desktop_core_evidence_does_not_probe_netsurf(self) -> None:
        fake_bin = self.directory / "desktop-core-bin"
        fake_bin.mkdir()
        actions = self.directory / "desktop-core-actions"
        console = self.directory / "desktop-core-console"
        input_directory = self.directory / "input"
        input_directory.mkdir()
        (input_directory / "event0").touch()
        (input_directory / "event1").touch()
        xorg_log = self.directory / "Xorg.0.log"
        xorg_log.write_text(
            "FBDEV(0)\n"
            "Adding extended input device Asterinas keyboard\n"
            "Adding extended input device Asterinas pointer\n",
            encoding="utf-8",
        )
        session_log = self.directory / "desktop-m4-session.log"
        session_log.touch()

        commands = {
            "systemctl": "exit 0\n",
            "loginctl": "printf '1 1000 asterinas seat0 tty1\\n'\n",
            "pgrep": (
                'printf \'pgrep:%s\\n\' "$*" >>"$ASTERINAS_TEST_ACTIONS"\n'
                'case "$*" in *netsurf*) exit 23;; esac\n'
                "exit 0\n"
            ),
            "xdotool": (
                'printf \'xdotool:%s\\n\' "$*" >>"$ASTERINAS_TEST_ACTIONS"\n'
                "printf '42\\n'\n"
            ),
            "xwininfo": (
                'printf \'xwininfo:%s\\n\' "$*" >>"$ASTERINAS_TEST_ACTIONS"\n'
                'printf \'"Asterinas Terminal" ("xterm" "XTerm")\\n\'\n'
            ),
        }
        for name, body in commands.items():
            command = fake_bin / name
            command.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
            command.chmod(0o755)

        result = subprocess.run(
            ["/bin/bash", str(DESKTOP_M4_EVIDENCE_SCRIPT)],
            env={
                **os.environ,
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "ASTERINAS_TEST_ACTIONS": str(actions),
                "ASTERINAS_DESKTOP_BROWSER_ENABLED": "0",
                "ASTERINAS_DESKTOP_M4_CONSOLE": str(console),
                "ASTERINAS_DESKTOP_M4_INPUT_DIRECTORY": str(input_directory),
                "ASTERINAS_DESKTOP_M4_XORG_LOG": str(xorg_log),
                "ASTERINAS_DESKTOP_M4_SESSION_LOG": str(session_log),
                "ASTERINAS_DESKTOP_M4_TIMEOUT_SECONDS": "1",
                "ASTERINAS_DESKTOP_M4_PROBE_TIMEOUT_SECONDS": "1",
            },
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            console.read_text(encoding="utf-8").splitlines()[-2:],
            list(DESKTOP_M4_CORE_MILESTONES[-2:]),
        )
        self.assertNotIn("netsurf", actions.read_text(encoding="utf-8").lower())

    def test_native_megrez_tcp_probe_validates_streamed_stress_response(self) -> None:
        executable = self.directory / "megrez-tcp-stress-self-test"
        compile_result = subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-DMEGREZ_TCP_PROBE_SELF_TEST",
                "-DMEGREZ_TCP_STRESS_BYTES=2097152",
                str(MEGREZ_TCP_PROBE_SOURCE),
                "-o",
                str(executable),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr)

        result = subprocess.run(
            [str(executable)], check=False, capture_output=True, text=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "MEGREZ_TCP_STRESS_SELF_TEST PASS "
            "sizes=16384,65536,1048576,2097152 pattern=mod251\n",
            result.stdout,
        )

    def test_native_megrez_tcp_probe_production_branch_compiles_strictly(self) -> None:
        executable = self.directory / "megrez-tcp-stress-init"
        result = subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-static",
                "-no-pie",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(MEGREZ_TCP_PROBE_SOURCE),
                "-o",
                str(executable),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

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
                    + (
                        "curl",
                        "fonts-wqy-microhei",
                        "iproute2",
                        "iputils-ping",
                        "x11-apps",
                        "xdotool",
                    )
                )
            ),
        )
        self.assertEqual(
            m5.identity_packages,
            m4.identity_packages
            + ("curl", "iproute2", "iputils-ping", "xdotool", "x11-apps"),
        )
        self.assertIn("fonts-wqy-microhei", m5.requested_packages)
        self.assertNotIn("fonts-wqy-microhei", m5.identity_packages)

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
finalize_browser_startup_caches() {
    mkdir -p "$1/usr/share/asterinas"
    printf 'called\n' >"$1/usr/share/asterinas/startup-cache-fixture"
}
python3() {
    printf '%s\n' 'DESKTOP_STARTUP_CACHE_PASS profile=desktop-m5-network sysusers=static ldconfig=riscv64 journal=catalog fontconfig=cached stamps=current'
}
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
        self.assertEqual(
            (stage / "usr/share/asterinas/startup-cache-fixture").read_text(),
            "called\n",
        )
        self.assertTrue((stage / "etc/.updated").is_file())
        self.assertTrue((stage / "var/.updated").is_file())
        self.assertTrue((stage / "usr/lib/asterinas/desktop-m4-session").is_file())
        core_evidence_unit = (
            stage / "etc/systemd/system/asterinas-desktop-core-evidence.service"
        )
        self.assertIn(
            "ConditionEnvironment=ASTERINAS_DESKTOP_BROWSER_ENABLED=0",
            core_evidence_unit.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "ExecStart=/usr/lib/asterinas/desktop-m4-evidence",
            core_evidence_unit.read_text(encoding="utf-8"),
        )
        self.assertTrue(
            (
                stage
                / "etc/systemd/system/graphical.target.wants"
                / core_evidence_unit.name
            ).is_symlink()
        )
        installed = stage / "usr/lib/asterinas/desktop-m5-network-evidence"
        self.assertEqual(installed.read_bytes(), EVIDENCE_SCRIPT.read_bytes())
        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o755)
        safe_reboot = stage / "usr/lib/asterinas/megrez-safe-reboot"
        self.assertEqual(safe_reboot.read_bytes(), SAFE_REBOOT_SCRIPT.read_bytes())
        self.assertEqual(stat.S_IMODE(safe_reboot.stat().st_mode), 0o755)
        safe_reboot_unit = stage / "etc/systemd/system/asterinas-safe-reboot.service"
        self.assertIn(
            "ExecStart=/usr/lib/asterinas/megrez-safe-reboot",
            safe_reboot_unit.read_text(encoding="utf-8"),
        )
        self.assertIn("Type=simple", safe_reboot_unit.read_text(encoding="utf-8"))
        self.assertNotIn("Type=oneshot", safe_reboot_unit.read_text(encoding="utf-8"))
        self.assertTrue(
            (
                stage / "etc/systemd/system/basic.target.wants" / safe_reboot_unit.name
            ).is_symlink()
        )
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
        self.assertNotIn("ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS", unit.read_text())
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
        desktop_drop_in = (
            stage
            / "etc/systemd/system/asterinas-desktop-m4.service.d"
            / "m7-browser-diagnostics.conf"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "Environment=ASTERINAS_DESKTOP_BROWSER_VERBOSE=1", desktop_drop_in
        )
        self.assertNotIn("ASTERINAS_DESKTOP_SHOW_OVERVIEW", desktop_drop_in)
        evidence_drop_in = (
            stage
            / "etc/systemd/system/asterinas-desktop-m4-evidence.service.d"
            / "m5-overview.conf"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            evidence_drop_in,
            "# SPDX-License-Identifier: MPL-2.0\n"
            "[Unit]\n"
            "Requires=asterinas-desktop-m5-network.service\n"
            "After=asterinas-desktop-m5-network.service\n"
            "\n"
            "[Service]\n"
            "Environment=ASTERINAS_DESKTOP_SHOW_OVERVIEW=1\n",
        )

    def test_safe_reboot_uses_uptime_and_syncs_before_reboot(self) -> None:
        fake_bin = self.directory / "safe-reboot-bin"
        fake_bin.mkdir()
        actions = self.directory / "safe-reboot-actions"
        console = self.directory / "safe-reboot-console"
        sleep_count = self.directory / "safe-reboot-sleep-count"
        uptime = self.directory / "uptime"
        uptime.write_text("100.25 80.00\n", encoding="utf-8")
        sleep = fake_bin / "sleep"
        sleep.write_text(
            "#!/bin/sh\n"
            "printf 'sleep:%s\\n' \"$*\" "
            '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n'
            'if [ ! -e "$ASTERINAS_SAFE_REBOOT_SLEEP_COUNT" ]; then\n'
            '    : >"$ASTERINAS_SAFE_REBOOT_SLEEP_COUNT"\n'
            "else\n"
            "    printf '130.00 100.00\\n' "
            '>"$ASTERINAS_SAFE_REBOOT_UPTIME_FILE"\n'
            "fi\n",
            encoding="utf-8",
        )
        sleep.chmod(0o755)
        for name in ("sync", "reboot"):
            command = fake_bin / name
            command.write_text(
                "#!/bin/sh\n"
                'printf \'%s:%s\\n\' "$(basename "$0")" "$*" '
                '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n',
                encoding="utf-8",
            )
            command.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_SAFE_REBOOT_AFTER="130",
            ASTERINAS_SAFE_REBOOT_UPTIME_FILE=str(uptime),
            ASTERINAS_SAFE_REBOOT_CONSOLE=str(console),
            ASTERINAS_SAFE_REBOOT_ACTIONS=str(actions),
            ASTERINAS_SAFE_REBOOT_SLEEP_COUNT=str(sleep_count),
        )

        result = subprocess.run(
            ["/bin/bash", str(SAFE_REBOOT_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            actions.read_text(encoding="utf-8").splitlines(),
            ["sleep:5", "sleep:5", "sync:", "reboot:-f"],
        )
        self.assertEqual(
            console.read_text(encoding="utf-8").splitlines(),
            [
                "ASTERINAS_USERSPACE_REBOOT_ARMED uptime=100 deadline=130",
                "ASTERINAS_USERSPACE_REBOOT_SYNC deadline=130",
            ],
        )

    def _fake_network_tools(
        self, *, address: str, directory: Path | None = None
    ) -> tuple[Path, Path]:
        directory = self.directory if directory is None else directory
        bin_directory = directory / "bin"
        bin_directory.mkdir(exist_ok=True)
        ping_log = directory / "ping.log"
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

    def _physical_evidence_environment(
        self, directory: Path, *, cmdline: str
    ) -> tuple[dict[str, str], Path, Path, Path, Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        console = directory / "console"
        console.write_text("", encoding="utf-8")
        cmdline_path = directory / "cmdline"
        cmdline_path.write_text(cmdline + "\n", encoding="utf-8")
        resolv_conf = directory / "resolv.conf"
        resolv_conf.write_text("nameserver 192.0.2.53\n", encoding="utf-8")
        url_file = directory / "desktop-url"
        url_file.write_text("https://old.invalid/\n", encoding="utf-8")
        fake_bin, ping_log = self._fake_network_tools(
            address="10.100.19.200/21", directory=directory
        )
        getent_log = directory / "getent.log"
        curl_log = directory / "curl.log"
        date_log = directory / "date.log"
        fixture_payload = directory / "fixture.bin"
        fixture_payload.write_bytes(PAYLOAD)
        getent = fake_bin / "getent"
        getent.write_text(
            """#!/bin/sh
printf '%s\n' "$*" >>"$ASTERINAS_M5_GETENT_LOG"
exit "${ASTERINAS_M5_GETENT_STATUS:-0}"
""",
            encoding="utf-8",
        )
        curl = fake_bin / "curl"
        curl.write_text(
            """#!/bin/sh
printf '%s\n' "$*" >>"$ASTERINAS_M5_CURL_LOG"
case "$*" in
    *asterinas-network-probe.bin*)
        previous=
        output_count=0
        for argument in "$@"; do
            if [ "$previous" = --output ]; then
                output_count=$((output_count + 1))
                if [ "${ASTERINAS_M5_FIXTURE_SHORT:-0}" = 1 ]; then
                    head -c 65535 "$ASTERINAS_M5_FIXTURE_PAYLOAD" >"$argument"
                elif [ "${ASTERINAS_M5_FIXTURE_CORRUPT:-0}" = 1 ]; then
                    printf X >"$argument"
                    tail -c +2 "$ASTERINAS_M5_FIXTURE_PAYLOAD" >>"$argument"
                else
                    cp "$ASTERINAS_M5_FIXTURE_PAYLOAD" "$argument"
                fi
            fi
            previous="$argument"
        done
        [ "$output_count" -gt 0 ] || exit 95
        [ "${ASTERINAS_M5_FIXTURE_STATUS:-0}" = 0 ] || exit "$ASTERINAS_M5_FIXTURE_STATUS"
        ;;
    *http://www.baidu.com/*)
        [ "${ASTERINAS_M5_CLOCK_STATUS:-0}" = 0 ] || exit "$ASTERINAS_M5_CLOCK_STATUS"
        printf 'HTTP/1.1 200 OK\r\n'
        if [ "${ASTERINAS_M5_CLOCK_DATE:-set}" != missing ]; then
            printf 'Date: %s\r\n' "${ASTERINAS_M5_CLOCK_DATE:-Sat, 29 Aug 2026 02:02:25 GMT}"
        fi
        printf '\r\n'
        ;;
    *result.png*)
        output=
        previous=
        for argument in "$@"; do
            if [ "$previous" = --output ]; then output="$argument"; break; fi
            previous="$argument"
        done
        [ -n "$output" ] || exit 96
        if [ "${ASTERINAS_M5_EMPTY_ASSET:-0}" = 1 ]; then : >"$output"; else printf PNG >"$output"; fi
        exit "${ASTERINAS_M5_ASSET_STATUS:-0}"
        ;;
    *)
        if [ "${ASTERINAS_M5_HTTPS_STATUS:-0}" != 0 ]; then
            printf '%s\n' "${ASTERINAS_M5_HTTPS_ERROR:-curl failed}" >&2
            exit "$ASTERINAS_M5_HTTPS_STATUS"
        fi
        printf '%s\t%s' "${ASTERINAS_M5_HTTP_CODE:-200}" "${ASTERINAS_M5_LOCAL_IP:-10.100.19.200}"
        ;;
esac
""",
            encoding="utf-8",
        )
        date = fake_bin / "date"
        date.write_text(
            """#!/bin/sh
printf '%s\n' "$*" >>"$ASTERINAS_M5_DATE_LOG"
exit "${ASTERINAS_M5_DATE_STATUS:-0}"
""",
            encoding="utf-8",
        )
        getent.chmod(0o755)
        curl.chmod(0o755)
        date.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_DESKTOP_M5_CONSOLE=str(console),
            ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS="5",
            ASTERINAS_DESKTOP_M5_CMDLINE_PATH=str(cmdline_path),
            ASTERINAS_DESKTOP_M5_RESOLV_CONF=str(resolv_conf),
            ASTERINAS_DESKTOP_M5_URL_FILE=str(url_file),
            ASTERINAS_M5_PING_LOG=str(ping_log),
            ASTERINAS_M5_GETENT_LOG=str(getent_log),
            ASTERINAS_M5_CURL_LOG=str(curl_log),
            ASTERINAS_M5_DATE_LOG=str(date_log),
            ASTERINAS_M5_FIXTURE_PAYLOAD=str(fixture_payload),
            ASTERINAS_DESKTOP_PROXY_URL="http://10.100.19.216:17893",
            ASTERINAS_DESKTOP_PROXY_HOST="10.100.19.216",
            ASTERINAS_DESKTOP_PROXY_PORT="17893",
            ASTERINAS_DESKTOP_FIXTURE_URL=(f"http://10.100.19.216:17894{FIXTURE_PATH}"),
            ASTERINAS_DESKTOP_FIXTURE_SIZE=str(PAYLOAD_SIZE),
            ASTERINAS_DESKTOP_FIXTURE_SHA256=PAYLOAD_SHA256,
            ASTERINAS_DESKTOP_FIXTURE_REQUESTS="20",
        )
        return environment, console, resolv_conf, url_file, ping_log, curl_log

    def _web_network_environment(
        self,
        directory: Path,
        *,
        mode: str,
        fail_stage: str = "",
        explicit_medium: bool = True,
    ) -> tuple[dict[str, str], Path, Path, Path]:
        directory.mkdir(parents=True)
        fake_bin = directory / "bin"
        fake_bin.mkdir()
        console = directory / "console"
        console.write_text("", encoding="utf-8")
        resolv_conf = directory / "resolv.conf"
        resolv_conf.write_text("nameserver 192.0.2.53\n", encoding="utf-8")
        command_log = directory / "commands.log"
        fixture_payload = directory / "fixture.bin"
        fixture_payload.write_bytes(PAYLOAD)
        medium_payload = directory / "medium.bin"
        medium_payload.write_bytes(BROWSER_DOWNLOAD)
        fixture_count = directory / "fixture-count"

        tools = {
            "ip": r'''#!/bin/sh
printf 'ip %s\n' "$*" >>"$ASTERINAS_WEB_COMMAND_LOG"
case "$*" in
    "-o link show dev eth0")
        [ "$ASTERINAS_WEB_FAIL_STAGE" = link ] && exit 1
        printf '%s\n' '2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP'
        ;;
    "-o -4 addr show dev eth0 scope global")
        [ "$ASTERINAS_WEB_FAIL_STAGE" = address ] && exit 1
        printf '%s\n' '2: eth0 inet 10.100.19.200/21 brd 10.100.23.255 scope global eth0'
        ;;
    neigh*)
        if [ "$ASTERINAS_WEB_FAIL_STAGE" = neighbor ]; then
            printf '%s\n' '10.100.16.1 dev eth0 INCOMPLETE'
        elif [ "$ASTERINAS_WEB_FAIL_STAGE" = neighbor-unobservable ]; then
            printf '%s\n' 'RTNETLINK answers: Operation not supported' >&2
            exit 1
        elif [ "$ASTERINAS_WEB_FAIL_STAGE" = neighbor-command ]; then
            exit 2
        else
            printf '%s\n' "$4 dev eth0 lladdr 4c:d6:29:18:93:43 REACHABLE"
        fi
        ;;
    *) exit 2 ;;
esac
''',
            "ping": r'''#!/bin/sh
printf 'ping %s\n' "$*" >>"$ASTERINAS_WEB_COMMAND_LOG"
[ "$ASTERINAS_WEB_FAIL_STAGE" = reachability ] && exit 1
exit 0
''',
            "getent": r'''#!/bin/sh
printf 'getent %s\n' "$*" >>"$ASTERINAS_WEB_COMMAND_LOG"
[ "$ASTERINAS_WEB_FAIL_STAGE" = dns ] && exit 2
printf '%s\n' '110.242.68.3 STREAM www.baidu.com'
''',
            "date": r'''#!/bin/sh
printf 'date %s\n' "$*" >>"$ASTERINAS_WEB_COMMAND_LOG"
exit 0
''',
            "curl": r'''#!/bin/sh
printf 'curl %s\n' "$*" >>"$ASTERINAS_WEB_COMMAND_LOG"
output=
headers=
previous=
for argument in "$@"; do
    if [ "$previous" = --output ]; then output="$argument"; fi
    if [ "$previous" = --dump-header ]; then headers="$argument"; fi
    previous="$argument"
done
[ -z "$headers" ] || printf 'HTTP/1.1 200 OK\r\nDate: Sat, 29 Aug 2026 02:02:25 GMT\r\n\r\n' >"$headers"
case "$*" in
    *http://www.baidu.com/*)
        [ "$ASTERINAS_WEB_FAIL_STAGE" = http ] && exit 7
        printf 'HTTP/1.1 200 OK\r\nDate: Sat, 29 Aug 2026 02:02:25 GMT\r\n\r\n'
        ;;
    *result.png*)
        [ "$ASTERINAS_WEB_FAIL_STAGE" = baidu-asset-curl ] && exit 22
        [ -n "$output" ] || exit 96
        if [ "$ASTERINAS_WEB_FAIL_STAGE" = baidu-asset ]; then
            printf 'not-png' >"$output"
        else
            printf '\211PNG\r\n\032\nfixture' >"$output"
        fi
        ;;
    *https://www.baidu.com/*|*https://10.0.2.2:8446/*)
        case "$ASTERINAS_WEB_FAIL_STAGE" in
            https-connect) exit 7 ;;
            https-tls) exit 60 ;;
        esac
        printf '200\t10.100.19.200\t0.010\t0.020'
        ;;
    *medium.bin*|*browser-quality/download.bin*)
        [ -n "$output" ] || exit 97
        if [ "$ASTERINAS_WEB_FAIL_STAGE" = medium ]; then
            head -c 262143 "$ASTERINAS_WEB_MEDIUM_PAYLOAD" >"$output"
        else
            cp "$ASTERINAS_WEB_MEDIUM_PAYLOAD" "$output"
        fi
        ;;
    *asterinas-network-probe.bin*)
        [ -n "$output" ] || exit 98
        count=0
        [ ! -f "$ASTERINAS_WEB_FIXTURE_COUNT" ] || count="$(cat "$ASTERINAS_WEB_FIXTURE_COUNT")"
        count=$((count + 1))
        printf '%s\n' "$count" >"$ASTERINAS_WEB_FIXTURE_COUNT"
        if [ "$ASTERINAS_WEB_FAIL_STAGE" = http ] && [ "$count" = 1 ]; then
            exit 7
        elif [ "$ASTERINAS_WEB_FAIL_STAGE" = repeat ] && [ "$count" -gt 1 ]; then
            head -c 65535 "$ASTERINAS_WEB_FIXTURE_PAYLOAD" >"$output"
        else
            cp "$ASTERINAS_WEB_FIXTURE_PAYLOAD" "$output"
        fi
        ;;
    *) exit 99 ;;
esac
''',
        }
        for name, source in tools.items():
            executable = fake_bin / name
            executable.write_text(source, encoding="utf-8")
            executable.chmod(0o755)

        proxy = {
            "ASTERINAS_DESKTOP_PROXY_URL": "http://10.100.19.216:17893",
            "ASTERINAS_DESKTOP_PROXY_HOST": "10.100.19.216",
            "ASTERINAS_DESKTOP_PROXY_PORT": "17893",
        }
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "ASTERINAS_DESKTOP_M5_CONSOLE": str(console),
            "ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS": "5",
            "ASTERINAS_DESKTOP_M5_COMMAND_TIMEOUT_SECONDS": "2",
            "ASTERINAS_DESKTOP_M5_RESOLV_CONF": str(resolv_conf),
            "ASTERINAS_DESKTOP_M5_URL_FILE": str(directory / "desktop-url"),
            "ASTERINAS_WEB_NETWORK_MODE": mode,
            "ASTERINAS_WEB_NETWORK_NEIGHBOR_QUERY": "1",
            "ASTERINAS_WEB_NETWORK_ADDRESS": "10.100.19.200/21",
            "ASTERINAS_WEB_NETWORK_GATEWAY": "10.100.16.1",
            "ASTERINAS_WEB_NETWORK_RESOLVER": "10.100.16.1",
            "ASTERINAS_DESKTOP_FIXTURE_URL": (
                f"http://10.100.19.216:17894{FIXTURE_PATH}"
            ),
            "ASTERINAS_DESKTOP_FIXTURE_SIZE": str(PAYLOAD_SIZE),
            "ASTERINAS_DESKTOP_FIXTURE_SHA256": PAYLOAD_SHA256,
            "ASTERINAS_DESKTOP_FIXTURE_REQUESTS": "20",
            "ASTERINAS_WEB_NETWORK_MEDIUM_URL": (
                "http://10.100.19.216:17894/medium.bin"
            ),
            "ASTERINAS_WEB_NETWORK_MEDIUM_SIZE": str(len(BROWSER_DOWNLOAD)),
            "ASTERINAS_WEB_NETWORK_MEDIUM_SHA256": BROWSER_DOWNLOAD_SHA256,
            "ASTERINAS_WEB_COMMAND_LOG": str(command_log),
            "ASTERINAS_WEB_FIXTURE_PAYLOAD": str(fixture_payload),
            "ASTERINAS_WEB_MEDIUM_PAYLOAD": str(medium_payload),
            "ASTERINAS_WEB_FIXTURE_COUNT": str(fixture_count),
            "ASTERINAS_WEB_FAIL_STAGE": fail_stage,
        }
        if mode == "proxy":
            environment.update(proxy)
        if not explicit_medium:
            for name in (
                "ASTERINAS_WEB_NETWORK_MEDIUM_URL",
                "ASTERINAS_WEB_NETWORK_MEDIUM_SIZE",
                "ASTERINAS_WEB_NETWORK_MEDIUM_SHA256",
            ):
                environment.pop(name)
        return environment, console, resolv_conf, command_log

    def test_web_network_derives_medium_fixture_from_probe_url(self) -> None:
        environment, console, _, command_log = self._web_network_environment(
            self.directory / "web-derived-medium",
            mode="proxy",
            explicit_medium=False,
        )

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, console.read_text())
        commands = command_log.read_text(encoding="utf-8")
        self.assertIn(BROWSER_DOWNLOAD_PATH, commands)
        self.assertEqual(
            console.read_text(encoding="utf-8").splitlines()[-1],
            "DEBIAN_WEB_NETWORK_READY mode=proxy layers=10",
        )

    def test_proxy_web_network_evidence(self) -> None:
        environment, console, resolv_conf, command_log = (
            self._web_network_environment(
                self.directory / "web-proxy", mode="proxy"
            )
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
            console.read_text(encoding="utf-8").splitlines(),
            [
                *(
                    f"DEBIAN_WEB_NETWORK_LAYER mode=proxy layer={layer} status=pass"
                    for layer in network_gate.NETWORK_LAYERS
                ),
                "DEBIAN_WEB_NETWORK_READY mode=proxy layers=10",
            ],
        )
        commands = command_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            sum(FIXTURE_PATH in line for line in commands),
            20,
        )
        external = [
            line
            for line in commands
            if "www.baidu.com" in line or "result.png" in line
        ]
        self.assertTrue(external)
        self.assertTrue(
            all("--proxy http://10.100.19.216:17893" in line for line in external)
        )
        self.assertFalse(any("http://www.baidu.com/" in line for line in commands))
        fixture_http = [line for line in commands if FIXTURE_PATH in line][0]
        self.assertIn("--dump-header", fixture_http)
        self.assertFalse(any(line.startswith("getent ") for line in commands))
        self.assertEqual(resolv_conf.read_text(), "nameserver 192.0.2.53\n")

    def test_direct_web_network_has_no_proxy_configuration(self) -> None:
        environment, console, resolv_conf, command_log = (
            self._web_network_environment(
                self.directory / "web-direct", mode="direct"
            )
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
            console.read_text(encoding="utf-8").splitlines()[-1],
            "DEBIAN_WEB_NETWORK_READY mode=direct layers=10",
        )
        commands = command_log.read_text(encoding="utf-8")
        self.assertNotIn("--proxy", commands)
        self.assertIn("getent ahostsv4 www.baidu.com", commands)
        self.assertEqual(resolv_conf.read_text(), "nameserver 10.100.16.1\n")

    def test_web_network_uses_owned_fixture_when_neighbor_dump_is_unobservable(
        self,
    ) -> None:
        environment, console, _, command_log = self._web_network_environment(
            self.directory / "web-neighbor-unobservable",
            mode="proxy",
            fail_stage="neighbor-unobservable",
        )

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = console.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            "DEBIAN_WEB_NETWORK_DIAGNOSTIC mode=proxy "
            "neighbor=unobservable proof=owned-fixture-http",
            lines,
        )
        self.assertEqual(lines[-1], "DEBIAN_WEB_NETWORK_READY mode=proxy layers=10")
        self.assertFalse(
            any(line.startswith("ping ") for line in command_log.read_text().splitlines())
        )

    def test_web_network_skips_unsupported_neighbor_query(self) -> None:
        environment, console, _, command_log = self._web_network_environment(
            self.directory / "web-neighbor-query-disabled",
            mode="proxy",
            fail_stage="neighbor-command",
        )
        environment["ASTERINAS_WEB_NETWORK_NEIGHBOR_QUERY"] = "0"

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = console.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            "DEBIAN_WEB_NETWORK_DIAGNOSTIC mode=proxy "
            "neighbor=unobservable proof=owned-fixture-http",
            lines,
        )
        self.assertEqual(lines[-1], "DEBIAN_WEB_NETWORK_READY mode=proxy layers=10")
        self.assertFalse(
            any(line.startswith("ip neigh ") for line in command_log.read_text().splitlines())
        )

    def test_web_network_failure_taxonomy(self) -> None:
        cases = {
            "link": ("link", "carrier"),
            "address": ("address", "static-address"),
            "neighbor": ("neighbor", "neighbor-unusable"),
            "neighbor-command": ("neighbor", "neighbor-unusable"),
            "reachability": ("reachability", "icmp-timeout"),
            "dns": ("dns", "resolve"),
            "http": ("http", "tcp-connect"),
            "https-connect": ("https", "tcp-connect"),
            "https-tls": ("https", "tls"),
            "baidu-asset": ("baidu-asset", "content"),
            "repeat": ("repeat", "length"),
            "medium": ("medium", "length"),
        }
        for fail_stage, (layer, reason) in cases.items():
            with self.subTest(fail_stage=fail_stage):
                environment, console, _, _ = self._web_network_environment(
                    self.directory / f"web-fail-{fail_stage}",
                    mode="direct",
                    fail_stage=fail_stage,
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
                    console.read_text(encoding="utf-8").splitlines()[-1],
                    f"DEBIAN_WEB_NETWORK_FAIL mode=direct "
                    f"layer={layer} reason={reason}",
                )

    def test_web_network_tls_override_remains_strict(self) -> None:
        environment, console, _, command_log = self._web_network_environment(
            self.directory / "web-local-tls",
            mode="direct",
            fail_stage="https-tls",
        )
        environment["ASTERINAS_WEB_NETWORK_HTTPS_URL"] = (
            "https://10.0.2.2:8446/"
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
            console.read_text(encoding="utf-8").splitlines()[-1],
            "DEBIAN_WEB_NETWORK_FAIL mode=direct layer=https reason=tls",
        )
        self.assertIn("https://10.0.2.2:8446/", command_log.read_text())

    def _qemu_fixture_environment(self, directory: Path) -> dict[str, str]:
        payload = directory / "qemu-fixture.bin"
        payload.write_bytes(PAYLOAD)
        return {
            "ASTERINAS_M5_FIXTURE_PAYLOAD": str(payload),
            "ASTERINAS_DESKTOP_FIXTURE_URL": (f"http://10.0.2.2:17894{FIXTURE_PATH}"),
            "ASTERINAS_DESKTOP_FIXTURE_SIZE": str(PAYLOAD_SIZE),
            "ASTERINAS_DESKTOP_FIXTURE_SHA256": PAYLOAD_SHA256,
            "ASTERINAS_DESKTOP_FIXTURE_REQUESTS": "20",
        }

    def test_guest_evidence_requires_link_and_proxied_https_without_dns(self) -> None:
        environment, console, resolv_conf, url_file, ping_log, curl_log = (
            self._physical_evidence_environment(
                self.directory / "physical-success",
                cmdline=(
                    "console=tty0 init=/init "
                    "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1 "
                    "-- --root-init=systemd"
                ),
            )
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
            console.read_text().splitlines(), list(EXPECTED_MEGREZ_CONSOLE)
        )
        self.assertFalse(ping_log.exists())
        self.assertEqual(resolv_conf.read_text(), "nameserver 192.0.2.53\n")
        self.assertFalse((self.directory / "physical-success/getent.log").exists())
        self.assertEqual(
            url_file.read_text(),
            "https://www.baidu.com/img/flexible/logo/pc/result.png\n",
        )
        curl_calls = curl_log.read_text().splitlines()
        self.assertEqual(len(curl_calls), 4)
        self.assertEqual(curl_calls[0].count(FIXTURE_PATH), 20)
        self.assertEqual(curl_calls[0].count("--output"), 20)
        self.assertNotIn("--proxy", curl_calls[0])
        self.assertIn("--head", curl_calls[1])
        self.assertIn("http://www.baidu.com/", curl_calls[1])
        self.assertIn("https://www.baidu.com/", curl_calls[2])
        self.assertIn("result.png", curl_calls[3])
        self.assertTrue(
            all("--proxy http://10.100.19.216:17893" in call for call in curl_calls[1:])
        )
        self.assertNotIn(" -k", f" {' '.join(curl_calls)}")
        self.assertEqual(
            (self.directory / "physical-success/date.log").read_text(),
            "--utc --set Sat, 29 Aug 2026 02:02:25 GMT\n",
        )

    def test_guest_evidence_rejects_wrong_megrez_bootarg_before_network(self) -> None:
        environment, console, resolv_conf, url_file, ping_log, curl_log = (
            self._physical_evidence_environment(
                self.directory / "wrong-bootarg",
                cmdline=(
                    "console=tty0 init=/init "
                    "asterinas.net=eic7700-rj45,10.100.19.200/21,192.0.2.1"
                ),
            )
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
            ["DEBIAN_NETWORK_M5_FAIL reason=megrez-bootarg"],
        )
        self.assertEqual(resolv_conf.read_text(), "nameserver 192.0.2.53\n")
        self.assertEqual(url_file.read_text(), "https://old.invalid/\n")
        self.assertFalse(ping_log.exists())
        self.assertFalse(curl_log.exists())

    def test_guest_evidence_reports_proxy_https_and_asset_failures(self) -> None:
        cases = (
            (
                "proxy",
                {"ASTERINAS_DESKTOP_PROXY_PORT": "17894"},
                "megrez-proxy-config",
            ),
            (
                "clock-date",
                {"ASTERINAS_M5_CLOCK_DATE": "missing"},
                "megrez-clock-date",
            ),
            (
                "clock-set",
                {"ASTERINAS_M5_DATE_STATUS": "1"},
                "megrez-clock-set",
            ),
            (
                "fixture-config",
                {"ASTERINAS_DESKTOP_FIXTURE_REQUESTS": "19"},
                "megrez-fixture-config",
            ),
            (
                "fixture-download",
                {"ASTERINAS_M5_FIXTURE_STATUS": "28"},
                "megrez-fixture-download",
            ),
            (
                "fixture-short",
                {"ASTERINAS_M5_FIXTURE_SHORT": "1"},
                "megrez-fixture-size",
            ),
            (
                "fixture-corrupt",
                {"ASTERINAS_M5_FIXTURE_CORRUPT": "1"},
                "megrez-fixture-sha256",
            ),
            (
                "https",
                {
                    "ASTERINAS_M5_HTTPS_STATUS": "42",
                    "ASTERINAS_M5_HTTPS_ERROR": "curl: recv failure",
                },
                "megrez-https",
            ),
            (
                "http-status",
                {"ASTERINAS_M5_HTTP_CODE": "503"},
                "megrez-http-status",
            ),
            (
                "local-address",
                {"ASTERINAS_M5_LOCAL_IP": "10.100.19.201"},
                "megrez-local-address",
            ),
            ("empty-asset", {"ASTERINAS_M5_EMPTY_ASSET": "1"}, "megrez-asset"),
        )
        for name, overrides, expected_reason in cases:
            with self.subTest(name=name):
                environment, console, _, url_file, _, _ = (
                    self._physical_evidence_environment(
                        self.directory / name,
                        cmdline=(
                            "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1"
                        ),
                    )
                )
                environment.update(overrides)

                result = subprocess.run(
                    ["/bin/bash", str(EVIDENCE_SCRIPT)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    console.read_text().splitlines()[-1],
                    f"DEBIAN_NETWORK_M5_FAIL reason={expected_reason}",
                )
                if name == "https":
                    self.assertEqual(
                        console.read_text().splitlines()[-2],
                        "DEBIAN_NETWORK_M5_DIAGNOSTIC "
                        "phase=megrez-https attempt=3 status=42 "
                        "stderr_hex=6375726c3a2072656376206661696c7572650a",
                    )
                self.assertEqual(url_file.read_text(), "https://old.invalid/\n")
                self.assertEqual(
                    tuple((self.directory / name).glob("desktop-url.fixture.*")),
                    (),
                )

    def test_guest_evidence_preserves_url_when_proxy_config_is_missing(self) -> None:
        environment, console, resolv_conf, url_file, _, _ = (
            self._physical_evidence_environment(
                self.directory / "resolver-rename",
                cmdline=("asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1"),
            )
        )
        environment.pop("ASTERINAS_DESKTOP_PROXY_URL")

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            console.read_text().splitlines()[-1],
            "DEBIAN_NETWORK_M5_FAIL reason=megrez-proxy-config",
        )
        self.assertEqual(resolv_conf.read_text(), "nameserver 192.0.2.53\n")
        self.assertEqual(url_file.read_text(), "https://old.invalid/\n")

    def test_guest_evidence_fails_once_for_wrong_address(self) -> None:
        console = self.directory / "bad-console"
        console.write_text("", encoding="utf-8")
        cmdline = self.directory / "bad-address-cmdline"
        cmdline.write_text(
            "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1\n",
            encoding="utf-8",
        )
        fake_bin, ping_log = self._fake_network_tools(address="10.100.19.201/21")
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_DESKTOP_M5_CONSOLE=str(console),
            ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS="0",
            ASTERINAS_DESKTOP_M5_CMDLINE_PATH=str(cmdline),
            ASTERINAS_M5_PING_LOG=str(ping_log),
            ASTERINAS_DESKTOP_PROXY_URL="http://10.100.19.216:17893",
            ASTERINAS_DESKTOP_PROXY_HOST="10.100.19.216",
            ASTERINAS_DESKTOP_PROXY_PORT="17893",
            ASTERINAS_DESKTOP_FIXTURE_URL=(f"http://10.100.19.216:17894{FIXTURE_PATH}"),
            ASTERINAS_DESKTOP_FIXTURE_SIZE=str(PAYLOAD_SIZE),
            ASTERINAS_DESKTOP_FIXTURE_SHA256=PAYLOAD_SHA256,
            ASTERINAS_DESKTOP_FIXTURE_REQUESTS="20",
        )

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        lines = console.read_text().splitlines()
        self.assertEqual(
            lines[-1], "DEBIAN_NETWORK_M5_FAIL reason=link-or-address-timeout"
        )
        self.assertEqual(len(lines), 3)
        self.assertIn("field=link status=0 value_hex=", lines[0])
        self.assertIn("field=address status=0 value_hex=", lines[1])
        self.assertFalse(ping_log.exists())

    def test_guest_evidence_keeps_link_diagnostics_bounded_to_cached_probes(
        self,
    ) -> None:
        source = EVIDENCE_SCRIPT.read_text(encoding="utf-8")
        service_source = (
            REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("LAST_LINK_OUTPUT", source)
        self.assertIn("LAST_ADDRESS_OUTPUT", source)
        self.assertIn("TimeoutStartSec=180s", service_source)
        self.assertNotIn('timeout "$LINK_PROBE_TIMEOUT_SECONDS" ip', source)

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
            "curl": """#!/bin/sh
printf '%s\n' "$*" >>"$ASTERINAS_M5_CURL_LOG"
case "$*" in
    *asterinas-network-probe.bin*)
        previous=
        output_count=0
        for argument in "$@"; do
            if [ "$previous" = --output ]; then
                cp "$ASTERINAS_M5_FIXTURE_PAYLOAD" "$argument"
                output_count=$((output_count + 1))
            fi
            previous="$argument"
        done
        [ "$output_count" -gt 0 ] || exit 95
        ;;
    *) printf '200\t10.0.2.15' ;;
esac
""",
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
        environment.update(self._qemu_fixture_environment(self.directory))

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
        curl_calls = curl_log.read_text().splitlines()
        self.assertEqual(len(curl_calls), 2)
        self.assertEqual(curl_calls[0].count(FIXTURE_PATH), 20)
        self.assertEqual(curl_calls[0].count("--output"), 20)
        self.assertIn("https://www.baidu.com/", curl_calls[-1])

    def test_qemu_evidence_retries_a_transient_dns_failure(self) -> None:
        console = self.directory / "qemu-retry-console"
        console.write_text("", encoding="utf-8")
        cmdline = self.directory / "qemu-retry-cmdline"
        cmdline.write_text(
            "console=ttyS0 asterinas.debian_network=qemu-slirp\n",
            encoding="utf-8",
        )
        resolv_conf = self.directory / "qemu-retry-resolv.conf"
        url_file = self.directory / "qemu-retry-desktop-url"
        fake_bin = self.directory / "qemu-retry-bin"
        fake_bin.mkdir()
        getent_log = self.directory / "qemu-retry-getent.log"
        curl_log = self.directory / "qemu-retry-curl.log"
        for name, body in {
            "getent": """#!/bin/sh
attempt=0
[ ! -f "$ASTERINAS_M5_GETENT_LOG" ] || attempt=$(cat "$ASTERINAS_M5_GETENT_LOG")
attempt=$((attempt + 1))
printf '%s\n' "$attempt" >"$ASTERINAS_M5_GETENT_LOG"
[ "$attempt" -ne 1 ] || exit 75
printf '%s\n' '110.242.68.66 STREAM www.baidu.com'
""",
            "curl": """#!/bin/sh
printf '%s\n' "$*" >>"$ASTERINAS_M5_CURL_LOG"
case "$*" in
    *asterinas-network-probe.bin*)
        previous=
        for argument in "$@"; do
            if [ "$previous" = --output ]; then
                cp "$ASTERINAS_M5_FIXTURE_PAYLOAD" "$argument"
            fi
            previous="$argument"
        done
        ;;
    *) printf '200\t10.0.2.15' ;;
esac
""",
            "sleep": "#!/bin/sh\nexit 0\n",
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
            ASTERINAS_M5_GETENT_LOG=str(getent_log),
            ASTERINAS_M5_CURL_LOG=str(curl_log),
        )
        environment.update(self._qemu_fixture_environment(self.directory))

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(getent_log.read_text(encoding="utf-8"), "2\n")
        self.assertEqual(
            console.read_text(encoding="utf-8").splitlines(),
            list(DESKTOP_M5_QEMU_MILESTONES),
        )

    def test_qemu_evidence_retries_https_and_bounds_final_diagnostic(self) -> None:
        def run_case(
            name: str, failures: int
        ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
            directory = self.directory / name
            directory.mkdir()
            console = directory / "console"
            console.write_text("", encoding="utf-8")
            cmdline = directory / "cmdline"
            cmdline.write_text(
                "console=ttyS0 asterinas.debian_network=qemu-slirp\n",
                encoding="utf-8",
            )
            fake_bin = directory / "bin"
            fake_bin.mkdir()
            curl_count = directory / "curl-count"
            for tool, body in {
                "getent": "#!/bin/sh\nprintf '%s\\n' '110.242.68.66 STREAM www.baidu.com'\n",
                "curl": """#!/bin/sh
case "$*" in
    *asterinas-network-probe.bin*)
        previous=
        for argument in "$@"; do
            if [ "$previous" = --output ]; then
                cp "$ASTERINAS_M5_FIXTURE_PAYLOAD" "$argument"
            fi
            previous="$argument"
        done
        exit 0
        ;;
esac
count=0
[ ! -f "$ASTERINAS_M5_CURL_COUNT" ] || count=$(cat "$ASTERINAS_M5_CURL_COUNT")
count=$((count + 1))
printf '%s\n' "$count" >"$ASTERINAS_M5_CURL_COUNT"
if [ "$count" -le "$ASTERINAS_M5_CURL_FAILURES" ]; then
    head -c 3000 /dev/zero | tr '\\000' E >&2
    exit 28
fi
printf '200\t10.0.2.15'
""",
                "sleep": "#!/bin/sh\nexit 0\n",
            }.items():
                executable = fake_bin / tool
                executable.write_text(body, encoding="utf-8")
                executable.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                PATH=f"{fake_bin}:/usr/bin:/bin",
                ASTERINAS_DESKTOP_M5_CONSOLE=str(console),
                ASTERINAS_DESKTOP_M5_CMDLINE_PATH=str(cmdline),
                ASTERINAS_DESKTOP_M5_RESOLV_CONF=str(directory / "resolv.conf"),
                ASTERINAS_DESKTOP_M5_URL_FILE=str(directory / "desktop-url"),
                ASTERINAS_M5_CURL_COUNT=str(curl_count),
                ASTERINAS_M5_CURL_FAILURES=str(failures),
            )
            environment.update(self._qemu_fixture_environment(directory))
            result = subprocess.run(
                ["/bin/bash", str(EVIDENCE_SCRIPT)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            return result, console, curl_count

        recovered, recovered_console, recovered_count = run_case("recovered", 1)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(recovered_count.read_text(encoding="utf-8"), "2\n")
        self.assertEqual(
            recovered_console.read_text(encoding="utf-8").splitlines(),
            list(DESKTOP_M5_QEMU_MILESTONES),
        )

        failed, failed_console, failed_count = run_case("failed", 3)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed_count.read_text(encoding="utf-8"), "3\n")
        failed_lines = failed_console.read_text(encoding="utf-8").splitlines()
        self.assertEqual(failed_lines[:2], list(DESKTOP_M5_QEMU_MILESTONES[:2]))
        self.assertEqual(
            failed_lines[2],
            "DEBIAN_NETWORK_M5_DIAGNOSTIC phase=qemu-https attempt=3 "
            f"stderr_hex={'45' * 2048}",
        )
        self.assertEqual(failed_lines[3], "DEBIAN_NETWORK_M5_FAIL reason=qemu-https")

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
        self.assertIn(EXPECTED_QEMU_STRESS_MILESTONE, DESKTOP_M5_QEMU_MILESTONES)
        for variable, value in (
            (
                "ASTERINAS_DESKTOP_FIXTURE_URL",
                f"http://10.0.2.2:17894{FIXTURE_PATH}",
            ),
            ("ASTERINAS_DESKTOP_FIXTURE_SIZE", str(PAYLOAD_SIZE)),
            ("ASTERINAS_DESKTOP_FIXTURE_SHA256", PAYLOAD_SHA256),
            ("ASTERINAS_DESKTOP_FIXTURE_REQUESTS", "20"),
            ("ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS", "120"),
        ):
            self.assertIn(
                f"systemd.setenv={variable}={value}",
                DESKTOP_M5_QEMU_BOOTARGS.split(),
            )
        self.assertEqual(DesktopM5QemuOperations.SCHEMA_VERSION, 5)
        self.assertEqual(DesktopM5QemuOperations.PROFILE_NAME, "desktop-m5-network")

    def test_qemu_network_target_ignores_desktop_readiness(self) -> None:
        transcript = (
            "\n".join(DESKTOP_M5_QEMU_MILESTONES)
            + "\nDEBIAN_DESKTOP_M4_FAIL reason=netsurf-window-probe-timeout\n"
        ).encode()

        result = classify_network_m5_qemu(
            transcript,
            expected_debian_release="13.6",
        )

        self.assertTrue(result.passed, result.reason)
        self.assertEqual(
            classify_network_m5_qemu(
                transcript + b"Kernel panic - not syncing\n",
                expected_debian_release="13.6",
            ).reason,
            "kernel panic",
        )
        selection, remaining = _parse_target([])
        self.assertEqual(selection.target, QemuGateTarget.BROWSER)
        self.assertIsNone(selection.network_mode)
        self.assertEqual(selection.expected_failure, QemuExpectedFailure.NONE)
        self.assertEqual(remaining, [])
        selection, remaining = _parse_target(
            [
                "--target",
                "network",
                "--network-mode",
                "proxy",
                "--expect-failure",
                "none",
                "--smp",
                "4",
            ]
        )
        self.assertEqual(
            selection.target,
            QemuGateTarget.NETWORK,
        )
        self.assertEqual(selection.network_mode, network_gate.NetworkMode.PROXY)
        self.assertEqual(remaining, ["--smp", "4"])
        with self.assertRaises(SystemExit):
            _parse_target(["--target", "invalid"])
        with self.assertRaises(SystemExit):
            _parse_target(["--target", "network"])
        self.assertFalse(NetworkM5QemuOperations.CAPTURE_SCREENSHOT)
        self.assertEqual(
            NetworkM5QemuOperations.MILESTONES,
            DESKTOP_M5_QEMU_MILESTONES,
        )
        self.assertEqual(
            DesktopM5QemuOperations._accepted_profile_identities(),
            ((5, "desktop-m5-network"),),
        )
        self.assertEqual(
            NetworkM5QemuOperations._accepted_profile_identities(),
            ((5, "desktop-m5-network"), (7, "browser-web")),
        )

    def test_qemu_web_network_mode_argv(self) -> None:
        proxy = qemu_web_network_bootargs(network_gate.NetworkMode.PROXY)
        direct = qemu_web_network_bootargs(network_gate.NetworkMode.DIRECT)

        self.assertIn("ASTERINAS_WEB_NETWORK_MODE=proxy", proxy)
        self.assertIn("ASTERINAS_DESKTOP_PROXY_URL=http://10.0.2.2:17893", proxy)
        self.assertIn("ASTERINAS_WEB_NETWORK_MODE=direct", direct)
        self.assertIn("ASTERINAS_WEB_NETWORK_RESOLVER=10.0.2.3", direct)
        self.assertNotIn("PROXY", direct)
        self.assertNotIn("--proxy", direct)
        tls = qemu_web_network_bootargs(
            network_gate.NetworkMode.DIRECT,
            expected_failure=QemuExpectedFailure.TLS,
        )
        self.assertIn(
            "ASTERINAS_WEB_NETWORK_HTTPS_URL=https://10.0.2.2:8446/",
            tls,
        )
        makefile = MAKEFILE.read_text(encoding="utf-8")
        underlying = makefile.split(
            ".PHONY: test_riscv_debian_desktop_m5_qemu_gate", 1
        )[1].split(".PHONY:", 1)[0]
        self.assertIn("--smp 4", underlying)
        self.assertIn('--network-mode "$(DEBIAN_WEB_NETWORK_MODE)"', underlying)
        for target in (
            "test_riscv_debian_web_network_proxy_qemu",
            "test_riscv_debian_web_network_direct_qemu",
        ):
            block = makefile.split(f".PHONY: {target}", 1)[1].split(
                ".PHONY:", 1
            )[0]
            self.assertIn("test_riscv_debian_desktop_m5_qemu_gate", block)
            self.assertIn("DEBIAN_DESKTOP_M5_QEMU_GATE_TARGET=network", block)

    def test_qemu_web_bootargs_are_split_below_uboot_console_limit(self) -> None:
        operations = object.__new__(NetworkM5QemuOperations)
        operations.BOOTARGS = qemu_web_network_bootargs(
            network_gate.NetworkMode.PROXY
        )
        commands = operations._boot_commands(0x40000000)
        bootarg_commands = tuple(
            command
            for command in commands
            if command.startswith("setenv ast_bootargs_")
            or command.startswith("setenv bootargs ")
        )

        self.assertGreaterEqual(len(bootarg_commands), 3)
        self.assertTrue(
            all(
                len(command.encode()) <= _UBOOT_COMMAND_SAFE_LIMIT
                for command in commands
            )
        )
        chunks = tuple(
            command.split('"', 2)[1]
            for command in bootarg_commands[:-1]
        )
        self.assertEqual(" ".join(chunks), operations.BOOTARGS)
        expansion = " ".join(
            f"${{ast_bootargs_{index}}}" for index in range(len(chunks))
        )
        self.assertEqual(
            bootarg_commands[-1],
            f'setenv bootargs "{expansion}"',
        )

    def test_qemu_web_network_negative_cases(self) -> None:
        cases = (
            (
                network_gate.NetworkMode.PROXY,
                QemuExpectedFailure.PROXY_UNAVAILABLE,
                b"DEBIAN_WEB_NETWORK_FAIL mode=proxy layer=https "
                b"reason=proxy-unavailable\n",
            ),
            (
                network_gate.NetworkMode.DIRECT,
                QemuExpectedFailure.DNS,
                b"DEBIAN_WEB_NETWORK_FAIL mode=direct layer=dns reason=resolve\n",
            ),
            (
                network_gate.NetworkMode.DIRECT,
                QemuExpectedFailure.TLS,
                b"DEBIAN_WEB_NETWORK_FAIL mode=direct layer=https reason=tls\n",
            ),
        )
        for mode, expected_failure, transcript in cases:
            with self.subTest(expected_failure=expected_failure):
                result = classify_qemu_web_network(
                    transcript,
                    mode=mode,
                    expected_failure=expected_failure,
                )
                self.assertTrue(result.passed, result.reason)
                self.assertFalse(
                    classify_qemu_web_network(
                        transcript
                        + f"DEBIAN_WEB_NETWORK_READY mode={mode.value} layers=10\n".encode(),
                        mode=mode,
                        expected_failure=expected_failure,
                    ).passed
                )
                self.assertFalse(
                    classify_qemu_web_network(
                        transcript,
                        mode=mode,
                        expected_failure=QemuExpectedFailure.NONE,
                    ).passed
                )

    def test_qemu_tls_negative_owns_repository_fixture_lifecycle(self) -> None:
        class FakeTlsFixture:
            def __init__(self) -> None:
                self.events: list[str] = []

            def start(self) -> None:
                self.events.append("start")

            def close(self) -> None:
                if not self.events or self.events[-1] != "close":
                    self.events.append("close")

            def summary(self) -> dict[str, object]:
                return {"endpoint": "https://10.0.2.2:8446/", "ready": True}

        inputs = []
        for index in range(8):
            path = self.directory / f"tls-input-{index}"
            path.write_bytes(str(index).encode())
            inputs.append(path)
        output = self.directory / "qemu-tls-evidence"
        output.mkdir()
        config = RootfsGateConfig(*inputs, output)
        fixture = FixtureServer(FixtureConfig("127.0.0.1", 0))
        tls_fixture = FakeTlsFixture()
        operations = NetworkM5QemuOperations(
            config,
            network_mode=network_gate.NetworkMode.DIRECT,
            expected_failure=QemuExpectedFailure.TLS,
            fixture=fixture,
            tls_fixture=tls_fixture,
        )
        expected = operations.MILESTONES[-1].encode()
        generic_failure = b"DEBIAN_WEB_NETWORK_FAIL mode="
        self.assertFalse(
            operations._completion_is_failure(expected, (generic_failure,))
        )
        self.assertTrue(
            operations._completion_is_failure(
                b"DEBIAN_WEB_NETWORK_FAIL mode=direct layer=dns reason=resolve",
                (generic_failure,),
            )
        )

        with operations:
            self.assertTrue(fixture.running)
            self.assertEqual(tls_fixture.events, ["start"])
        self.assertFalse(fixture.running)
        self.assertEqual(tls_fixture.events, ["start", "close"])

    def test_qemu_proxy_lifecycle_skips_bridge_only_for_expected_outage(self) -> None:
        class FakeProxy:
            def __init__(self) -> None:
                self.events: list[str] = []

            def start(self) -> None:
                self.events.append("start")

            def close(self) -> None:
                if not self.events or self.events[-1] != "close":
                    self.events.append("close")

            def summary(self) -> dict[str, object]:
                return {"listen": "0.0.0.0:17893", "ready": True}

        inputs = []
        for index in range(8):
            path = self.directory / f"proxy-input-{index}"
            path.write_bytes(str(index).encode())
            inputs.append(path)
        config = RootfsGateConfig(
            *inputs,
            self.directory / "qemu-proxy-evidence",
        )
        config.output_directory.mkdir()

        for expected_failure, expected_events in (
            (QemuExpectedFailure.NONE, ["start", "close"]),
            (QemuExpectedFailure.PROXY_UNAVAILABLE, ["close"]),
        ):
            with self.subTest(expected_failure=expected_failure):
                fixture = FixtureServer(FixtureConfig("127.0.0.1", 0))
                proxy = FakeProxy()
                operations = NetworkM5QemuOperations(
                    config,
                    network_mode=network_gate.NetworkMode.PROXY,
                    expected_failure=expected_failure,
                    fixture=fixture,
                    proxy_bridge=proxy,
                )
                with operations:
                    self.assertTrue(fixture.running)
                self.assertEqual(proxy.events, expected_events)

    def test_qemu_expected_dns_failure_does_not_require_fixture_requests(self) -> None:
        inputs = []
        for index in range(8):
            path = self.directory / f"dns-input-{index}"
            path.write_bytes(str(index).encode())
            inputs.append(path)
        output = self.directory / "qemu-dns-evidence"
        output.mkdir()
        config = RootfsGateConfig(*inputs, output)
        fixture = FixtureServer(FixtureConfig("127.0.0.1", 0))
        operations = NetworkM5QemuOperations(
            config,
            network_mode=network_gate.NetworkMode.DIRECT,
            expected_failure=QemuExpectedFailure.DNS,
            fixture=fixture,
        )
        transcript = (
            b"DEBIAN_WEB_NETWORK_FAIL mode=direct layer=dns reason=resolve\n"
        )

        with operations:
            operations.invalidate(config)
            result: dict[str, object] = {"passed": True, "reason": "pass"}
            operations.publish(config, None, transcript, result)

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["network_fixture"]["request_count"], 0)
        self.assertEqual(
            result["classified_guest_failure"],
            {"mode": "direct", "layer": "dns", "reason": "resolve"},
        )

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

    def test_qemu_operations_owns_and_publishes_fixture_lifecycle(self) -> None:
        inputs = []
        for index in range(8):
            path = self.directory / f"input-{index}"
            path.write_bytes(str(index).encode())
            inputs.append(path)
        output = self.directory / "qemu-fixture-evidence"
        output.mkdir()
        config = RootfsGateConfig(*inputs, output)
        fixture = FixtureServer(FixtureConfig("127.0.0.1", 0))
        operations = DesktopM5QemuOperations(config, fixture=fixture)

        with operations:
            self.assertTrue(fixture.running)
            operations.invalidate(config)
            result: dict[str, object] = {"passed": False, "reason": "test"}
            operations.publish(config, None, b"serial\n", result)
            self.assertIn("network_fixture", result)
        self.assertFalse(fixture.running)

        summary = json.loads(
            (output / "network-fixture.json").read_text(encoding="utf-8")
        )
        self.assertEqual(summary["request_count"], 0)
        published = json.loads((output / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(published["network_fixture"]["request_count"], 0)
        self.assertEqual(published["target"], "browser")

    def test_qemu_make_gate_allows_the_cold_desktop_to_finish(self) -> None:
        target = (
            MAKEFILE.read_text(encoding="utf-8")
            .split(".PHONY: test_riscv_debian_desktop_m5_qemu_gate", 1)[1]
            .split(".PHONY:", 1)[0]
        )

        self.assertIn('--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"', target)
        self.assertIn('--target "$(DEBIAN_DESKTOP_M5_QEMU_GATE_TARGET)"', target)
        self.assertIn("DEBIAN_DESKTOP_BOOT_TIMEOUT ?= 420", MAKEFILE.read_text())
        self.assertIn(
            "DEBIAN_DESKTOP_M5_QEMU_GATE_TARGET ?= browser",
            MAKEFILE.read_text(),
        )

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
        for marker in EXPECTED_MEGREZ_MILESTONES[1:4]:
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
