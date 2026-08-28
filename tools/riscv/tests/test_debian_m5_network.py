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
MEGREZ_TCP_PROBE_SOURCE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/megrez_tcp_probe_init.c"
)
EXPECTED_MEGREZ_MILESTONES = (
    "DEBIAN_NETWORK_M5_LINK interface=eth0 address=10.100.19.200/21 state=lower-up",
    "DEBIAN_NETWORK_M5_MEGREZ_DNS resolver=10.2.0.5 fallback=10.2.0.6 host=www.baidu.com",
    "DEBIAN_NETWORK_M5_MEGREZ_HTTPS host=www.baidu.com status=200 address=10.100.19.200",
    "DEBIAN_NETWORK_M5_MEGREZ_ASSET host=www.baidu.com resource=logo-png",
    "DEBIAN_NETWORK_M5_MEGREZ_READY mode=static-rj45",
)


class DebianDesktopM5NetworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

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
                        "xdotool",
                    )
                )
            ),
        )
        self.assertEqual(
            m5.identity_packages,
            m4.identity_packages + ("curl", "iproute2", "iputils-ping", "xdotool"),
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
            "[Service]\n"
            "Environment=ASTERINAS_DESKTOP_SHOW_OVERVIEW=1\n",
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
        [ "${ASTERINAS_M5_HTTPS_STATUS:-0}" = 0 ] || exit "$ASTERINAS_M5_HTTPS_STATUS"
        printf '%s\t%s' "${ASTERINAS_M5_HTTP_CODE:-200}" "${ASTERINAS_M5_LOCAL_IP:-10.100.19.200}"
        ;;
esac
""",
            encoding="utf-8",
        )
        getent.chmod(0o755)
        curl.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_DESKTOP_M5_CONSOLE=str(console),
            ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS="0",
            ASTERINAS_DESKTOP_M5_CMDLINE_PATH=str(cmdline_path),
            ASTERINAS_DESKTOP_M5_RESOLV_CONF=str(resolv_conf),
            ASTERINAS_DESKTOP_M5_URL_FILE=str(url_file),
            ASTERINAS_M5_PING_LOG=str(ping_log),
            ASTERINAS_M5_GETENT_LOG=str(getent_log),
            ASTERINAS_M5_CURL_LOG=str(curl_log),
        )
        return environment, console, resolv_conf, url_file, ping_log, curl_log

    def test_guest_evidence_requires_link_dns_https_without_ping(self) -> None:
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
            console.read_text().splitlines(), list(EXPECTED_MEGREZ_MILESTONES)
        )
        self.assertFalse(ping_log.exists())
        self.assertEqual(
            resolv_conf.read_text(), "nameserver 10.2.0.5\nnameserver 10.2.0.6\n"
        )
        self.assertEqual(
            url_file.read_text(),
            "https://www.baidu.com/img/flexible/logo/pc/result.png\n",
        )
        curl_calls = curl_log.read_text().splitlines()
        self.assertEqual(len(curl_calls), 2)
        self.assertIn("https://www.baidu.com/", curl_calls[0])
        self.assertIn("result.png", curl_calls[1])
        self.assertNotIn(" -k", f" {' '.join(curl_calls)}")

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

    def test_guest_evidence_reports_dns_https_and_asset_failures(self) -> None:
        cases = (
            ("dns", {"ASTERINAS_M5_GETENT_STATUS": "41"}, "megrez-dns"),
            ("https", {"ASTERINAS_M5_HTTPS_STATUS": "42"}, "megrez-https"),
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
                self.assertEqual(url_file.read_text(), "https://old.invalid/\n")

    def test_guest_evidence_preserves_resolver_when_atomic_rename_fails(self) -> None:
        environment, console, resolv_conf, url_file, _, _ = (
            self._physical_evidence_environment(
                self.directory / "resolver-rename",
                cmdline=("asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1"),
            )
        )
        fake_mv = Path(environment["PATH"].split(":", 1)[0]) / "mv"
        fake_mv.write_text("#!/bin/sh\nexit 89\n", encoding="utf-8")
        fake_mv.chmod(0o755)

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
            "DEBIAN_NETWORK_M5_FAIL reason=resolver-publish",
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
printf '%s\n' "$*" >"$ASTERINAS_M5_CURL_LOG"
printf '200\t10.0.2.15'
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
        self.assertEqual(failed_lines[0], DESKTOP_M5_QEMU_MILESTONES[0])
        self.assertEqual(
            failed_lines[1],
            "DEBIAN_NETWORK_M5_DIAGNOSTIC phase=qemu-https attempt=3 "
            f"stderr_hex={'45' * 2048}",
        )
        self.assertEqual(failed_lines[2], "DEBIAN_NETWORK_M5_FAIL reason=qemu-https")

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

        self.assertIn('--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"', target)
        self.assertIn("DEBIAN_DESKTOP_BOOT_TIMEOUT ?= 420", MAKEFILE.read_text())

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
