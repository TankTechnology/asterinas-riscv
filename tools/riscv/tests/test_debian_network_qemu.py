#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from tools.riscv.tests.support.debian_network import *


class DebianNetworkQemuTests(_DebianNetworkFixtures, unittest.TestCase):
    def test_web_network_retries_one_transient_https_timeout(self) -> None:
        environment, console, _, _ = self._web_network_environment(
            self.directory / "web-https-once",
            mode="proxy",
            fail_stage="https-once",
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
            "DEBIAN_WEB_NETWORK_RETRY mode=proxy layer=https attempt=1 reason=timeout",
            lines,
        )
        self.assertEqual(lines[-1], "DEBIAN_WEB_NETWORK_READY mode=proxy layers=10")

    def test_web_network_failure_taxonomy(self) -> None:
        cases = {
            "link": ("link", "carrier", 75),
            "address": ("address", "static-address", 75),
            "neighbor": ("neighbor", "neighbor-unusable", 75),
            "neighbor-command": ("neighbor", "neighbor-unusable", 75),
            "reachability": ("reachability", "icmp-timeout", 75),
            "dns": ("dns", "resolve", 75),
            "http": ("http", "tcp-connect", 75),
            "https-connect": ("https", "tcp-connect", 75),
            "https-tls": ("https", "tls", 75),
            "baidu-asset": ("baidu-asset", "content", 1),
            "repeat": ("repeat", "length", 1),
            "repeat-timeout": ("repeat", "timeout", 75),
            "medium": ("medium", "length", 1),
        }
        for fail_stage, (layer, reason, status) in cases.items():
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

                self.assertEqual(result.returncode, status)
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
        environment["ASTERINAS_WEB_NETWORK_HTTPS_URL"] = "https://10.0.2.2:8446/"

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
            ["DEBIAN_NETWORK_FAIL reason=megrez-bootarg"],
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
                {"ASTERINAS_NETWORK_CLOCK_DATE": "missing"},
                "megrez-clock-date",
            ),
            (
                "clock-set",
                {"ASTERINAS_NETWORK_DATE_STATUS": "1"},
                "megrez-clock-set",
            ),
            (
                "fixture-config",
                {"ASTERINAS_DESKTOP_FIXTURE_REQUESTS": "19"},
                "megrez-fixture-config",
            ),
            (
                "fixture-download",
                {"ASTERINAS_NETWORK_FIXTURE_STATUS": "28"},
                "megrez-fixture-download",
            ),
            (
                "fixture-short",
                {"ASTERINAS_NETWORK_FIXTURE_SHORT": "1"},
                "megrez-fixture-size",
            ),
            (
                "fixture-corrupt",
                {"ASTERINAS_NETWORK_FIXTURE_CORRUPT": "1"},
                "megrez-fixture-sha256",
            ),
            (
                "https",
                {
                    "ASTERINAS_NETWORK_HTTPS_STATUS": "42",
                    "ASTERINAS_NETWORK_HTTPS_ERROR": "curl: recv failure",
                },
                "megrez-https",
            ),
            (
                "http-status",
                {"ASTERINAS_NETWORK_HTTP_CODE": "503"},
                "megrez-http-status",
            ),
            (
                "local-address",
                {"ASTERINAS_NETWORK_LOCAL_IP": "10.100.19.201"},
                "megrez-local-address",
            ),
            ("empty-asset", {"ASTERINAS_NETWORK_EMPTY_ASSET": "1"}, "megrez-asset"),
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
                    f"DEBIAN_NETWORK_FAIL reason={expected_reason}",
                )
                if name == "https":
                    console_lines = console.read_text().splitlines()
                    self.assertRegex(
                        console_lines[-2],
                        r"^DEBIAN_NETWORK_DIAGNOSTIC "
                        r"phase=megrez-https attempt=3 status=42 elapsed=[0-9]+ "
                        r"stderr_hex=6375726c3a2072656376206661696c7572650a "
                        r"trace_hex=none$",
                    )
                    self.assertEqual(
                        [
                            line
                            for line in console_lines
                            if line.endswith("stage=begin")
                        ],
                        [
                            "DEBIAN_NETWORK_DIAGNOSTIC "
                            f"phase=megrez-https attempt={attempt} stage=begin"
                            for attempt in (1, 2, 3)
                        ],
                    )
                    self.assertEqual(
                        len(
                            [
                                line
                                for line in console_lines
                                if "phase=megrez-https" in line and "status=" in line
                            ]
                        ),
                        3,
                    )
                self.assertEqual(url_file.read_text(), "https://old.invalid/\n")
                self.assertEqual(
                    tuple((self.directory / name).glob("desktop-url.fixture.*")),
                    (),
                )

    def test_guest_evidence_bounds_megrez_https_by_the_global_deadline(self) -> None:
        environment, console, _, _, _, _ = (
            self._physical_evidence_environment(
                self.directory / "https-deadline",
                cmdline=("asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1"),
            )
        )
        environment["ASTERINAS_NETWORK_HTTPS_SLEEP"] = "5"
        environment["ASTERINAS_NETWORK_HTTPS_STATUS"] = "7"

        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        console_lines = console.read_text().splitlines()
        self.assertEqual(
            console_lines[-1], "DEBIAN_NETWORK_FAIL reason=megrez-https"
        )
        self.assertIn(
            "DEBIAN_NETWORK_DIAGNOSTIC "
            "phase=megrez-https attempt=2 stage=deadline",
            console_lines,
        )
        self.assertEqual(
            [
                line
                for line in console_lines
                if "phase=megrez-https" in line and line.endswith("stage=begin")
            ],
            [
                "DEBIAN_NETWORK_DIAGNOSTIC "
                "phase=megrez-https attempt=1 stage=begin"
            ],
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
            "DEBIAN_NETWORK_FAIL reason=megrez-proxy-config",
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
            ASTERINAS_DESKTOP_NETWORK_CONSOLE=str(console),
            ASTERINAS_DESKTOP_NETWORK_TIMEOUT_SECONDS="0",
            ASTERINAS_DESKTOP_NETWORK_CMDLINE_PATH=str(cmdline),
            ASTERINAS_NETWORK_PING_LOG=str(ping_log),
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
            lines[-1], "DEBIAN_NETWORK_FAIL reason=link-or-address-timeout"
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
        self.assertIn("TimeoutStartSec=45s", service_source)
        self.assertNotIn('timeout "$LINK_PROBE_TIMEOUT_SECONDS" ip', source)
        self.assertIn(
            'FIXTURE_CONNECT_TIMEOUT_SECONDS:-1',
            source,
        )
        self.assertIn(
            '--connect-timeout "$FIXTURE_CONNECT_TIMEOUT_SECONDS"',
            source,
        )
        self.assertIn('FIXTURE_MAX_TIME_SECONDS:-2', source)
        self.assertIn('--max-time "$FIXTURE_MAX_TIME_SECONDS"', source)

    def test_dns_shim_unit_does_not_loop_or_fail_without_a_forwarder(self) -> None:
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        block = source[source.index("asterinas-dns-shim.service"):]
        block = block[: block.index("[Install]")]
        # The healthy shim serves forever; oneshot would wait for a process
        # that never exits and TimeoutStartSec would kill it.  The
        # unconfigured case is skipped by ExecCondition; the tunnel-down case
        # is a successful exit with Restart=no, so neither loops or fails.
        self.assertIn("Type=simple", block)
        self.assertNotIn("Type=oneshot", block)
        self.assertIn("Restart=no", block)
        self.assertIn('ExecCondition=/bin/sh -c', block)
        self.assertIn('test -n "${ASTERINAS_DESKTOP_DNS_HOST:-}"', block)
        self.assertIn("SuccessExitStatus=1 3", block)
        self.assertNotIn("Restart=always", block)
        self.assertNotIn("RestartPreventExitStatus", block)

    def test_builder_drops_unsupported_sysctl_files(self) -> None:
        stage = self.directory / "sysctl-stage"
        lib_directory = stage / "usr/lib/sysctl.d"
        etc_directory = stage / "etc/sysctl.d"
        lib_directory.mkdir(parents=True)
        etc_directory.mkdir(parents=True)
        pid_max = lib_directory / "50-pid-max.conf"
        pid_max.write_text(
            "# Debian default\nkernel.pid_max = 4194304\n",
            encoding="utf-8",
        )
        coredump = lib_directory / "10-coredump-debian.conf"
        coredump.write_text("kernel.core_pattern=core\n", encoding="utf-8")
        unrelated = lib_directory / "99-keep.conf"
        unrelated.write_text("fs.file-max=1\n", encoding="utf-8")

        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                """source "$1"
drop_unsupported_sysctl_files "$2"
""",
                "builder-sysctl-test",
                str(BUILD_SCRIPT),
                str(stage),
            ],
            cwd=self.directory,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(pid_max.exists())
        self.assertFalse(coredump.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "fs.file-max=1\n")
        builder = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'drop_unsupported_sysctl_files "$stage"',
            builder,
        )
        self.assertIn("10-coredump-debian.conf", builder)
        self.assertIn("50-pid-max.conf", builder)

    def test_dns_shim_unit_fails_fast_without_a_forwarder(self) -> None:
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        block = source[source.index("asterinas-dns-shim.service"):]
        block = block[: block.index("[Install]")]
        # Main keeps the shim long-lived, but skips it when no forwarder is
        # configured and treats exit 1/3 as explicit inactive states.  There is
        # no Restart=always loop.
        self.assertIn("Type=simple", block)
        self.assertIn("Restart=no", block)
        self.assertIn(
            "ExecCondition=/bin/sh -c 'test -n \"${ASTERINAS_DESKTOP_DNS_HOST:-}\"'",
            block,
        )
        self.assertIn("SuccessExitStatus=1 3", block)
        self.assertNotIn("Type=oneshot", block)
        self.assertNotIn("Restart=always", block)
        self.assertNotIn("RestartPreventExitStatus", block)

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
printf '%s\n' "$*" >>"$ASTERINAS_NETWORK_CURL_LOG"
case "$*" in
    *asterinas-network-probe.bin*)
        previous=
        output_count=0
        for argument in "$@"; do
            if [ "$previous" = --output ]; then
                cp "$ASTERINAS_NETWORK_FIXTURE_PAYLOAD" "$argument"
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
            ASTERINAS_DESKTOP_NETWORK_CONSOLE=str(console),
            ASTERINAS_DESKTOP_NETWORK_CMDLINE_PATH=str(cmdline),
            ASTERINAS_DESKTOP_NETWORK_RESOLV_CONF=str(resolv_conf),
            ASTERINAS_DESKTOP_NETWORK_URL_FILE=str(url_file),
            ASTERINAS_NETWORK_CURL_LOG=str(curl_log),
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
            console.read_text().splitlines(), list(DESKTOP_NETWORK_QEMU_MILESTONES)
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
[ ! -f "$ASTERINAS_NETWORK_GETENT_LOG" ] || attempt=$(cat "$ASTERINAS_NETWORK_GETENT_LOG")
attempt=$((attempt + 1))
printf '%s\n' "$attempt" >"$ASTERINAS_NETWORK_GETENT_LOG"
[ "$attempt" -ne 1 ] || exit 75
printf '%s\n' '110.242.68.66 STREAM www.baidu.com'
""",
            "curl": """#!/bin/sh
printf '%s\n' "$*" >>"$ASTERINAS_NETWORK_CURL_LOG"
case "$*" in
    *asterinas-network-probe.bin*)
        previous=
        for argument in "$@"; do
            if [ "$previous" = --output ]; then
                cp "$ASTERINAS_NETWORK_FIXTURE_PAYLOAD" "$argument"
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
            ASTERINAS_DESKTOP_NETWORK_CONSOLE=str(console),
            ASTERINAS_DESKTOP_NETWORK_CMDLINE_PATH=str(cmdline),
            ASTERINAS_DESKTOP_NETWORK_RESOLV_CONF=str(resolv_conf),
            ASTERINAS_DESKTOP_NETWORK_URL_FILE=str(url_file),
            ASTERINAS_NETWORK_GETENT_LOG=str(getent_log),
            ASTERINAS_NETWORK_CURL_LOG=str(curl_log),
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
            list(DESKTOP_NETWORK_QEMU_MILESTONES),
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
                cp "$ASTERINAS_NETWORK_FIXTURE_PAYLOAD" "$argument"
            fi
            previous="$argument"
        done
        exit 0
        ;;
esac
count=0
[ ! -f "$ASTERINAS_NETWORK_CURL_COUNT" ] || count=$(cat "$ASTERINAS_NETWORK_CURL_COUNT")
count=$((count + 1))
printf '%s\n' "$count" >"$ASTERINAS_NETWORK_CURL_COUNT"
if [ "$count" -le "$ASTERINAS_NETWORK_CURL_FAILURES" ]; then
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
                ASTERINAS_DESKTOP_NETWORK_CONSOLE=str(console),
                ASTERINAS_DESKTOP_NETWORK_CMDLINE_PATH=str(cmdline),
                ASTERINAS_DESKTOP_NETWORK_RESOLV_CONF=str(directory / "resolv.conf"),
                ASTERINAS_DESKTOP_NETWORK_URL_FILE=str(directory / "desktop-url"),
                ASTERINAS_NETWORK_CURL_COUNT=str(curl_count),
                ASTERINAS_NETWORK_CURL_FAILURES=str(failures),
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
            list(DESKTOP_NETWORK_QEMU_MILESTONES),
        )

        failed, failed_console, failed_count = run_case("failed", 3)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed_count.read_text(encoding="utf-8"), "3\n")
        failed_lines = failed_console.read_text(encoding="utf-8").splitlines()
        self.assertEqual(failed_lines[:2], list(DESKTOP_NETWORK_QEMU_MILESTONES[:2]))
        self.assertEqual(
            failed_lines[2],
            "DEBIAN_NETWORK_DIAGNOSTIC phase=qemu-https attempt=3 "
            f"stderr_hex={'45' * 2048}",
        )
        self.assertEqual(failed_lines[3], "DEBIAN_NETWORK_FAIL reason=qemu-https")

    def test_qemu_classifier_and_adapter_bind_network_before_desktop(self) -> None:
        transcript = (
            "\n".join((*DESKTOP_NETWORK_QEMU_MILESTONES, *DESKTOP_M4_MILESTONES)) + "\n"
        ).encode()

        result = classify_desktop_network_qemu(transcript, expected_debian_release="13.6")

        self.assertTrue(result.passed, result.reason)
        reversed_result = classify_desktop_network_qemu(
            "\n".join(
                reversed((*DESKTOP_NETWORK_QEMU_MILESTONES, *DESKTOP_M4_MILESTONES))
            ).encode(),
            expected_debian_release="13.6",
        )
        self.assertEqual(reversed_result.reason, "desktop milestones out of order")
        self.assertIn("asterinas.debian_network=qemu-slirp", DESKTOP_NETWORK_QEMU_BOOTARGS)
        self.assertIn(EXPECTED_QEMU_STRESS_MILESTONE, DESKTOP_NETWORK_QEMU_MILESTONES)
        for variable, value in (
            (
                "ASTERINAS_DESKTOP_FIXTURE_URL",
                f"http://10.0.2.2:17894{FIXTURE_PATH}",
            ),
            ("ASTERINAS_DESKTOP_FIXTURE_SIZE", str(PAYLOAD_SIZE)),
            ("ASTERINAS_DESKTOP_FIXTURE_SHA256", PAYLOAD_SHA256),
            ("ASTERINAS_DESKTOP_FIXTURE_REQUESTS", "20"),
            ("ASTERINAS_DESKTOP_NETWORK_TIMEOUT_SECONDS", "120"),
        ):
            self.assertIn(
                f"systemd.setenv={variable}={value}",
                DESKTOP_NETWORK_QEMU_BOOTARGS.split(),
            )
        self.assertEqual(DesktopNetworkQemuOperations.SCHEMA_VERSION, 5)
        self.assertEqual(DesktopNetworkQemuOperations.PROFILE_NAME, "desktop-network")
        for mode in (network_gate.NetworkMode.PROXY, network_gate.NetworkMode.DIRECT):
            self.assertIn(
                "systemd.setenv=ASTERINAS_DESKTOP_NETWORK_TIMEOUT_SECONDS=300",
                qemu_web_network_bootargs(mode).split(),
            )

    def test_qemu_network_target_ignores_desktop_readiness(self) -> None:
        transcript = (
            "\n".join(DESKTOP_NETWORK_QEMU_MILESTONES)
            + "\nDEBIAN_DESKTOP_M4_FAIL reason=netsurf-window-probe-timeout\n"
        ).encode()

        result = classify_network_qemu(
            transcript,
            expected_debian_release="13.6",
        )

        self.assertTrue(result.passed, result.reason)
        self.assertEqual(
            classify_network_qemu(
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
        self.assertFalse(NetworkQemuOperations.CAPTURE_SCREENSHOT)
        self.assertEqual(
            NetworkQemuOperations.MILESTONES,
            DESKTOP_NETWORK_QEMU_MILESTONES,
        )
        self.assertEqual(
            DesktopNetworkQemuOperations._accepted_profile_identities(),
            ((5, "desktop-network"),),
        )
        self.assertEqual(
            NetworkQemuOperations._accepted_profile_identities(),
            ((5, "desktop-network"), (7, "browser-web")),
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
            ".PHONY: test_riscv_debian_desktop_network_qemu_gate", 1
        )[1].split(".PHONY:", 1)[0]
        self.assertIn("--smp 4", underlying)
        self.assertIn('--network-mode "$(DEBIAN_WEB_NETWORK_MODE)"', underlying)
        for target in (
            "test_riscv_debian_web_network_proxy_qemu",
            "test_riscv_debian_web_network_direct_qemu",
        ):
            block = makefile.split(f".PHONY: {target}", 1)[1].split(".PHONY:", 1)[0]
            self.assertIn("test_riscv_debian_desktop_network_qemu_gate", block)
            self.assertIn("DEBIAN_DESKTOP_NETWORK_QEMU_GATE_TARGET=network", block)

    def test_qemu_web_bootargs_are_split_below_uboot_console_limit(self) -> None:
        operations = object.__new__(NetworkQemuOperations)
        operations.BOOTARGS = qemu_web_network_bootargs(network_gate.NetworkMode.PROXY)
        operations.config = SimpleNamespace(root_fs="ext2")
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
        chunks = tuple(command.split('"', 2)[1] for command in bootarg_commands[:-1])
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
        operations = NetworkQemuOperations(
            config,
            network_mode=network_gate.NetworkMode.DIRECT,
            expected_failure=QemuExpectedFailure.TLS,
            fixture=fixture,
            tls_fixture=tls_fixture,
        )
        expected = operations.MILESTONES[-1].encode()
        generic_failure = b"DEBIAN_WEB_NETWORK_FAIL mode="
        self.assertEqual(operations.FAILURE_MARKER, expected)
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
                operations = NetworkQemuOperations(
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
        operations = NetworkQemuOperations(
            config,
            network_mode=network_gate.NetworkMode.DIRECT,
            expected_failure=QemuExpectedFailure.DNS,
            fixture=fixture,
        )
        transcript = b"DEBIAN_WEB_NETWORK_FAIL mode=direct layer=dns reason=resolve\n"

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

        arguments = desktop_network_qemu_argv(
            uboot=self.directory / "u-boot",
            boot_disk=self.directory / "boot.ext4",
            root_disk=self.directory / "root.ext2",
            monitor_socket=self.directory / "monitor.sock",
        )

        self.assertNotIn("-nic", arguments)
        self.assertEqual(arguments.count("user,id=net0"), 1)
        self.assertEqual(arguments.count("virtio-net-device,netdev=net0"), 1)
        self.assertTrue(
            any(argument.startswith("bochs-display") for argument in arguments)
        )
        for device in ("virtio-keyboard-device", "virtio-tablet-device"):
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
        operations = DesktopNetworkQemuOperations(config, fixture=fixture)

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
            .split(".PHONY: test_riscv_debian_desktop_network_qemu_gate", 1)[1]
            .split(".PHONY:", 1)[0]
        )

        self.assertIn('--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"', target)
        self.assertIn('--target "$(DEBIAN_DESKTOP_NETWORK_QEMU_GATE_TARGET)"', target)
        self.assertIn("DEBIAN_DESKTOP_BOOT_TIMEOUT ?= 420", MAKEFILE.read_text())
        self.assertIn(
            "DEBIAN_DESKTOP_NETWORK_QEMU_GATE_TARGET ?= browser",
            MAKEFILE.read_text(),
        )

    def test_classifier_requires_order_and_scans_complete_transcript(self) -> None:
        self.assertEqual(DESKTOP_NETWORK_MILESTONES, EXPECTED_MEGREZ_MILESTONES)
        transcript = "\n".join(EXPECTED_MEGREZ_MILESTONES).encode()
        self.assertTrue(
            classify_desktop_network(
                transcript,
                expected_debian_release="13.6",
            ).passed
        )
        reordered = "\n".join(reversed(EXPECTED_MEGREZ_MILESTONES)).encode()
        self.assertEqual(
            classify_desktop_network(
                reordered,
                expected_debian_release="13.6",
            ).reason,
            "desktop milestones out of order",
        )
        fatal_after_ready = transcript + b"\nKernel panic - not syncing"
        self.assertEqual(
            classify_desktop_network(
                fatal_after_ready,
                expected_debian_release="13.6",
            ).reason,
            "kernel panic",
        )
        for marker in EXPECTED_MEGREZ_MILESTONES[1:4]:
            with self.subTest(missing=marker):
                missing = transcript.replace(marker.encode(), b"")
                self.assertEqual(
                    classify_desktop_network(
                        missing,
                        expected_debian_release="13.6",
                    ).reason,
                    f"missing desktop milestone: {marker}",
                )
            with self.subTest(duplicate=marker):
                duplicate = transcript + b"\n" + marker.encode()
                self.assertEqual(
                    classify_desktop_network(
                        duplicate,
                        expected_debian_release="13.6",
                    ).reason,
                    "duplicate desktop milestone",
                )


if __name__ == "__main__":
    unittest.main()
