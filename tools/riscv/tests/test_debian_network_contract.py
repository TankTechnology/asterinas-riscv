#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from tools.riscv.tests.support.debian_network import *


class DebianNetworkContractTests(_DebianNetworkFixtures, unittest.TestCase):
    def test_web_network_modes_are_isolated(self) -> None:
        self.assertTrue(hasattr(network_gate, "NetworkMode"))
        self.assertTrue(hasattr(network_gate, "classify_web_network"))
        proxy = network_gate.NetworkMode.PROXY
        direct = network_gate.NetworkMode.DIRECT
        proxy_transcript = self._web_network_transcript(proxy)
        direct_transcript = self._web_network_transcript(direct)

        self.assertTrue(
            network_gate.classify_web_network(proxy_transcript, mode=proxy).passed
        )
        self.assertTrue(
            network_gate.classify_web_network(direct_transcript, mode=direct).passed
        )
        self.assertFalse(
            network_gate.classify_web_network(proxy_transcript, mode=direct).passed
        )
        self.assertFalse(
            network_gate.classify_web_network(direct_transcript, mode=proxy).passed
        )

        mixed = (
            proxy_transcript
            + ("DEBIAN_WEB_NETWORK_READY mode=direct layers=10\n").encode()
        )
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
        first = ("DEBIAN_WEB_NETWORK_LAYER mode=proxy layer=link status=pass").encode()
        second = (
            "DEBIAN_WEB_NETWORK_LAYER mode=proxy layer=address status=pass"
        ).encode()
        reordered = (
            transcript.replace(first, b"__FIRST__")
            .replace(second, first)
            .replace(b"__FIRST__", second)
        )
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
            network_gate.classify_web_network(unknown_layer, mode=mode).reason,
            "web network failure has unknown layer",
        )
        config_failure = (
            b"DEBIAN_WEB_NETWORK_FAIL mode=direct layer=config reason=proxy-present\n"
        )
        self.assertEqual(
            network_gate.classify_web_network(config_failure, mode=mode).reason,
            "web network config failure: proxy-present",
        )
        wrong_mode = b"DEBIAN_WEB_NETWORK_FAIL mode=proxy layer=https reason=broken\n"
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
            'readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_NETWORK_TIMEOUT_SECONDS:-30}"',
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
                "ASTERINAS_DESKTOP_M4_TIMEOUT_SECONDS": "0",
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

    def test_browser_web_profile_carries_daily_tools(self) -> None:
        profile = get_profile("browser-web")
        for package in ("vim", "build-essential"):
            self.assertIn(package, profile.requested_packages)
            self.assertIn(package, profile.identity_packages)
        self.assertEqual(profile.root_size_bytes, 3 * 1024 * 1024 * 1024)
        self.assertEqual(profile.root_label, "ASTER_BROWSERWEB")

    def test_builder_prints_browser_web_packages_without_build_tools(self) -> None:
        result = subprocess.run(
            [
                "/bin/bash",
                str(BUILD_SCRIPT),
                "--profile",
                "browser-web",
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
            get_profile("browser-web").requested_packages,
        )

    def test_safe_reboot_uses_uptime_and_syncs_before_reboot(self) -> None:
        fake_bin = self.directory / "safe-reboot-bin"
        fake_bin.mkdir()
        unit_dir = self.directory / "safe-reboot-units"
        unit_dir.mkdir()
        for name in (
            "asterinas-browser-web-evidence.service",
            "asterinas-browser-web.service",
            "asterinas-desktop-network.service",
            "asterinas-desktop.service",
        ):
            (unit_dir / name).touch()
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
        for name in ("systemctl", "sync", "reboot"):
            command = fake_bin / name
            command.write_text(
                "#!/bin/sh\n"
                'printf \'%s:%s\\n\' "$(basename "$0")" "$*" '
                '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n',
                encoding="utf-8",
            )
            command.chmod(0o755)
        pgrep = fake_bin / "pgrep"
        pgrep.write_text(
            "#!/bin/sh\n"
            'printf \'pgrep:%s\\n\' "$*" '
            '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n'
            "exit 1\n",
            encoding="utf-8",
        )
        pgrep.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_SAFE_REBOOT_AFTER="130",
            ASTERINAS_SAFE_REBOOT_UPTIME_FILE=str(uptime),
            ASTERINAS_SAFE_REBOOT_CONSOLE=str(console),
            ASTERINAS_SAFE_REBOOT_ACTIONS=str(actions),
            ASTERINAS_SAFE_REBOOT_SLEEP_COUNT=str(sleep_count),
            ASTERINAS_SAFE_REBOOT_UNIT_DIR=str(unit_dir),
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
            [
                "sleep:5",
                "sleep:5",
                "systemctl:stop asterinas-browser-web-evidence.service asterinas-browser-web.service asterinas-desktop-network.service asterinas-desktop.service",
                "pgrep:-u 1000",
                "sync:",
                "reboot:-f",
            ],
        )
        self.assertEqual(
            console.read_text(encoding="utf-8").splitlines(),
            [
                "ASTERINAS_USERSPACE_REBOOT_ARMED uptime=100 deadline=130",
                "ASTERINAS_USERSPACE_REBOOT_QUIESCE_START deadline=130 units=4",
                "ASTERINAS_USERSPACE_REBOOT_PROCESS state=observed count=0",
                "ASTERINAS_USERSPACE_REBOOT_QUIESCE_DONE processes=0",
                "ASTERINAS_USERSPACE_REBOOT_SYNC_START deadline=130",
                "ASTERINAS_USERSPACE_REBOOT_SYNC_DONE deadline=130",
            ],
        )

    def test_safe_reboot_derives_userspace_deadline_from_kernel_cmdline(
        self,
    ) -> None:
        fake_bin = self.directory / "safe-reboot-cmdline-bin"
        fake_bin.mkdir()
        unit_dir = self.directory / "safe-reboot-cmdline-units"
        unit_dir.mkdir()
        for name in (
            "asterinas-browser-web-evidence.service",
            "asterinas-browser-web.service",
            "asterinas-desktop-network.service",
            "asterinas-desktop.service",
        ):
            (unit_dir / name).touch()
        actions = self.directory / "safe-reboot-cmdline-actions"
        actions.write_text("", encoding="utf-8")
        console = self.directory / "safe-reboot-cmdline-console"
        console.write_text("", encoding="utf-8")
        uptime = self.directory / "safe-reboot-cmdline-uptime"
        uptime.write_text("120.25 80.00\n", encoding="utf-8")
        cmdline = self.directory / "safe-reboot-cmdline"
        cmdline.write_text(
            "console=ttyS0 asterinas.reboot_after=300 init=/init\n",
            encoding="utf-8",
        )
        for name in ("systemctl", "sync", "reboot"):
            command = fake_bin / name
            command.write_text(
                "#!/bin/sh\n"
                'printf \'%s:%s\\n\' "$(basename "$0")" "$*" '
                '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n',
                encoding="utf-8",
            )
            command.chmod(0o755)
        pgrep = fake_bin / "pgrep"
        pgrep.write_text(
            "#!/bin/sh\n"
            'printf \'pgrep:%s\\n\' "$*" '
            '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n'
            "exit 1\n",
            encoding="utf-8",
        )
        pgrep.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_SAFE_REBOOT_UPTIME_FILE=str(uptime),
            ASTERINAS_SAFE_REBOOT_CMDLINE_PATH=str(cmdline),
            ASTERINAS_SAFE_REBOOT_CONSOLE=str(console),
            ASTERINAS_SAFE_REBOOT_ACTIONS=str(actions),
            ASTERINAS_SAFE_REBOOT_UNIT_DIR=str(unit_dir),
        )
        environment.pop("ASTERINAS_SAFE_REBOOT_AFTER", None)

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
            [
                "systemctl:stop asterinas-browser-web-evidence.service asterinas-browser-web.service asterinas-desktop-network.service asterinas-desktop.service",
                "pgrep:-u 1000",
                "sync:",
                "reboot:-f",
            ],
        )
        self.assertEqual(
            console.read_text(encoding="utf-8").splitlines(),
            [
                "ASTERINAS_USERSPACE_REBOOT_ARMED uptime=120 deadline=120",
                "ASTERINAS_USERSPACE_REBOOT_QUIESCE_START deadline=120 units=4",
                "ASTERINAS_USERSPACE_REBOOT_PROCESS state=observed count=0",
                "ASTERINAS_USERSPACE_REBOOT_QUIESCE_DONE processes=0",
                "ASTERINAS_USERSPACE_REBOOT_SYNC_START deadline=120",
                "ASTERINAS_USERSPACE_REBOOT_SYNC_DONE deadline=120",
            ],
        )

    def test_safe_reboot_hands_off_only_to_ready_current_boot(self) -> None:
        fake_bin = self.directory / "desktop-handoff-bin"
        fake_bin.mkdir()
        units = self.directory / "desktop-handoff-units"
        units.mkdir()
        actions = self.directory / "desktop-handoff-actions"
        console = self.directory / "desktop-handoff-console"
        uptime = self.directory / "desktop-handoff-uptime"
        cmdline = self.directory / "desktop-handoff-cmdline"
        cmdline.write_text("asterinas.reboot_after=300\n", encoding="utf-8")
        ready = self.directory / "desktop-handoff-ready"
        boot_id = self.directory / "desktop-handoff-boot-id"
        boot_id.write_text("11111111-2222-3333-4444-555555555555\n", encoding="utf-8")
        watchdog = self.directory / "desktop-handoff-watchdog"
        sleep = fake_bin / "sleep"
        sleep.write_text(
            "#!/bin/sh\n"
            'printf "sleep:%s\\n" "$*" >>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n'
            'printf "110.00 100.00\\n" >"$ASTERINAS_SAFE_REBOOT_UPTIME_FILE"\n'
            'printf "%s\\n" "$READY_BOOT" >"$ASTERINAS_SAFE_REBOOT_DESKTOP_READY_FILE"\n'
            'printf "%s\\n" "$WATCHDOG_VALUE" >"$ASTERINAS_SAFE_REBOOT_WATCHDOG_FILE"\n',
            encoding="utf-8",
        )
        sleep.chmod(0o755)
        for name in ("systemctl", "sync", "reboot"):
            command = fake_bin / name
            command.write_text(
                "#!/bin/sh\n"
                'printf \'%s:%s\\n\' "$(basename "$0")" "$*" '
                '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n',
                encoding="utf-8",
            )
            command.chmod(0o755)
        pgrep = fake_bin / "pgrep"
        pgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        pgrep.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_SAFE_REBOOT_AFTER="110",
            ASTERINAS_SAFE_REBOOT_CMDLINE_PATH=str(cmdline),
            ASTERINAS_SAFE_REBOOT_UPTIME_FILE=str(uptime),
            ASTERINAS_SAFE_REBOOT_CONSOLE=str(console),
            ASTERINAS_SAFE_REBOOT_UNIT_DIR=str(units),
            ASTERINAS_SAFE_REBOOT_ACTIONS=str(actions),
            ASTERINAS_SAFE_REBOOT_DESKTOP_READY_FILE=str(ready),
            ASTERINAS_SAFE_REBOOT_BOOT_ID_FILE=str(boot_id),
            ASTERINAS_SAFE_REBOOT_WATCHDOG_FILE=str(watchdog),
        )
        for marker_boot, watchdog_value, should_disarm in (
            ("11111111-2222-3333-4444-555555555555", "0", True),
            ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "0", False),
            ("11111111-2222-3333-4444-555555555555", "1", False),
        ):
            with self.subTest(marker_boot=marker_boot, watchdog=watchdog_value):
                ready.unlink(missing_ok=True)
                actions.write_text("", encoding="utf-8")
                console.write_text("", encoding="utf-8")
                uptime.write_text("100.00 90.00\n", encoding="utf-8")
                watchdog.write_text("1\n", encoding="utf-8")
                environment["READY_BOOT"] = marker_boot
                environment["WATCHDOG_VALUE"] = watchdog_value
                result = subprocess.run(
                    ["/bin/bash", str(SAFE_REBOOT_SCRIPT)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                entries = actions.read_text(encoding="utf-8").splitlines()
                notices = console.read_text(encoding="utf-8").splitlines()
                self.assertEqual("reboot:-f" in entries, not should_disarm)
                self.assertEqual(
                    any("ASTERINAS_USERSPACE_REBOOT_DISARMED" in line for line in notices),
                    should_disarm,
                )

    def test_safe_reboot_refuses_work_past_kernel_deadline_budget(self) -> None:
        fake_bin = self.directory / "safe-reboot-deadline-bin"
        fake_bin.mkdir()
        unit_dir = self.directory / "safe-reboot-deadline-units"
        unit_dir.mkdir()
        (unit_dir / "asterinas-desktop.service").touch()
        actions = self.directory / "safe-reboot-deadline-actions"
        console = self.directory / "safe-reboot-deadline-console"
        uptime = self.directory / "safe-reboot-deadline-uptime"
        uptime.write_text("80.00 70.00\n", encoding="utf-8")
        cmdline = self.directory / "safe-reboot-deadline-cmdline"
        cmdline.write_text("asterinas.reboot_after=100\n", encoding="utf-8")
        for name in ("systemctl", "pgrep", "sync", "reboot"):
            command = fake_bin / name
            command.write_text(
                "#!/bin/sh\n"
                'printf \'%s:%s\\n\' "$(basename "$0")" "$*" '
                '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n'
                + ("exit 1\n" if name == "pgrep" else ""),
                encoding="utf-8",
            )
            command.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_SAFE_REBOOT_UPTIME_FILE=str(uptime),
            ASTERINAS_SAFE_REBOOT_CMDLINE_PATH=str(cmdline),
            ASTERINAS_SAFE_REBOOT_CONSOLE=str(console),
            ASTERINAS_SAFE_REBOOT_ACTIONS=str(actions),
            ASTERINAS_SAFE_REBOOT_UNIT_DIR=str(unit_dir),
        )
        environment.pop("ASTERINAS_SAFE_REBOOT_AFTER", None)

        for explicit_deadline in (None, "1"):
            with self.subTest(explicit_deadline=explicit_deadline):
                actions.write_text("", encoding="utf-8")
                console.write_text("", encoding="utf-8")
                if explicit_deadline is None:
                    environment.pop("ASTERINAS_SAFE_REBOOT_AFTER", None)
                else:
                    environment["ASTERINAS_SAFE_REBOOT_AFTER"] = explicit_deadline
                result = subprocess.run(
                    ["/bin/bash", str(SAFE_REBOOT_SCRIPT)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("sync:", actions.read_text(encoding="utf-8"))
                self.assertNotIn("reboot:", actions.read_text(encoding="utf-8"))
                self.assertIn(
                    "ASTERINAS_USERSPACE_REBOOT_FAIL reason=kernel-deadline-exhausted",
                    console.read_text(encoding="utf-8").splitlines(),
                )

    def test_safe_reboot_manual_after_verified_desktop_handoff(self) -> None:
        fake_bin = self.directory / "manual-reboot-bin"
        fake_bin.mkdir()
        units = self.directory / "manual-reboot-units"
        units.mkdir()
        (units / "asterinas-desktop.service").touch()
        actions = self.directory / "manual-reboot-actions"
        console = self.directory / "manual-reboot-console"
        uptime = self.directory / "manual-reboot-uptime"
        uptime.write_text("31380.00 100.00\n", encoding="utf-8")
        cmdline = self.directory / "manual-reboot-cmdline"
        cmdline.write_text("asterinas.reboot_after=300\n", encoding="utf-8")
        ready = self.directory / "manual-reboot-ready"
        boot_id = self.directory / "manual-reboot-boot-id"
        current_boot = "11111111-2222-3333-4444-555555555555"
        boot_id.write_text(current_boot + "\n", encoding="utf-8")
        watchdog = self.directory / "manual-reboot-watchdog"
        watchdog.write_text("0\n", encoding="utf-8")
        for name in ("systemctl", "pgrep", "sync", "reboot"):
            command = fake_bin / name
            command.write_text(
                "#!/bin/sh\n"
                'printf \'%s:%s\\n\' "$(basename "$0")" "$*" '
                '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n'
                + ("exit 1\n" if name == "pgrep" else ""),
                encoding="utf-8",
            )
            command.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_SAFE_REBOOT_MANUAL="1",
            ASTERINAS_SAFE_REBOOT_AFTER="1",
            ASTERINAS_SAFE_REBOOT_CMDLINE_PATH=str(cmdline),
            ASTERINAS_SAFE_REBOOT_UPTIME_FILE=str(uptime),
            ASTERINAS_SAFE_REBOOT_CONSOLE=str(console),
            ASTERINAS_SAFE_REBOOT_UNIT_DIR=str(units),
            ASTERINAS_SAFE_REBOOT_ACTIONS=str(actions),
            ASTERINAS_SAFE_REBOOT_DESKTOP_READY_FILE=str(ready),
            ASTERINAS_SAFE_REBOOT_BOOT_ID_FILE=str(boot_id),
            ASTERINAS_SAFE_REBOOT_WATCHDOG_FILE=str(watchdog),
        )
        cases = (
            (current_boot, "0", True),
            ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "0", False),
            (current_boot, "1", False),
        )
        for marker_boot, watchdog_value, should_reboot in cases:
            with self.subTest(marker_boot=marker_boot, watchdog=watchdog_value):
                ready.write_text(marker_boot + "\n", encoding="utf-8")
                watchdog.write_text(watchdog_value + "\n", encoding="utf-8")
                actions.write_text("", encoding="utf-8")
                console.write_text("", encoding="utf-8")
                result = subprocess.run(
                    ["/bin/bash", str(SAFE_REBOOT_SCRIPT)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                self.assertEqual(result.returncode == 0, should_reboot, result.stderr)
                entries = actions.read_text(encoding="utf-8").splitlines()
                self.assertEqual("reboot:-f" in entries, should_reboot)
                if not should_reboot:
                    self.assertIn(
                        "ASTERINAS_USERSPACE_REBOOT_FAIL reason=manual-reboot-not-ready",
                        console.read_text(encoding="utf-8").splitlines(),
                    )

    def test_safe_reboot_stops_root_evidence_and_skips_absent_browser_units(self) -> None:
        fake_bin = self.directory / "safe-reboot-network-bin"
        fake_bin.mkdir()
        unit_dir = self.directory / "safe-reboot-network-units"
        unit_dir.mkdir()
        (unit_dir / "asterinas-desktop-evidence.service").touch()
        (unit_dir / "asterinas-desktop-network.service").touch()
        (unit_dir / "asterinas-desktop.service").touch()
        actions = self.directory / "safe-reboot-network-actions"
        console = self.directory / "safe-reboot-network-console"
        uptime = self.directory / "safe-reboot-network-uptime"
        uptime.write_text("100.00 80.00\n", encoding="utf-8")
        for name in ("systemctl", "pgrep", "sync", "reboot"):
            command = fake_bin / name
            command.write_text(
                "#!/bin/sh\n"
                'printf \'%s:%s\\n\' "$(basename "$0")" "$*" '
                '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n'
                + (
                    'for unit in "$@"; do\n'
                    '  case "$unit" in *.service) '
                    '[ -f "$ASTERINAS_SAFE_REBOOT_UNIT_DIR/$unit" ] || exit 1;; esac\n'
                    'done\n'
                    if name == "systemctl"
                    else "exit 1\n" if name == "pgrep" else ""
                ),
                encoding="utf-8",
            )
            command.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_SAFE_REBOOT_AFTER="100",
            ASTERINAS_SAFE_REBOOT_UPTIME_FILE=str(uptime),
            ASTERINAS_SAFE_REBOOT_CONSOLE=str(console),
            ASTERINAS_SAFE_REBOOT_ACTIONS=str(actions),
            ASTERINAS_SAFE_REBOOT_UNIT_DIR=str(unit_dir),
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
        action_lines = actions.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            "systemctl:stop asterinas-desktop-evidence.service asterinas-desktop-network.service asterinas-desktop.service",
            action_lines,
        )
        self.assertNotIn("browser-web", "\n".join(action_lines))
        self.assertEqual(action_lines[-2:], ["sync:", "reboot:-f"])

    def test_safe_reboot_requires_units_stopped_after_kill_escalation(self) -> None:
        fake_bin = self.directory / "safe-reboot-unit-stuck-bin"
        fake_bin.mkdir()
        unit_dir = self.directory / "safe-reboot-unit-stuck-units"
        unit_dir.mkdir()
        (unit_dir / "asterinas-desktop-evidence.service").touch()
        actions = self.directory / "safe-reboot-unit-stuck-actions"
        console = self.directory / "safe-reboot-unit-stuck-console"
        uptime = self.directory / "safe-reboot-unit-stuck-uptime"
        uptime.write_text("100.00 80.00\n", encoding="utf-8")
        for name in ("systemctl", "pgrep", "sync", "reboot"):
            command = fake_bin / name
            command.write_text(
                "#!/bin/sh\n"
                'printf \'%s:%s\\n\' "$(basename "$0")" "$*" '
                '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n'
                + (
                    'if [ "$1" = stop ]; then exit 1; fi\n'
                    if name == "systemctl"
                    else "exit 1\n" if name == "pgrep" else ""
                ),
                encoding="utf-8",
            )
            command.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_SAFE_REBOOT_AFTER="100",
            ASTERINAS_SAFE_REBOOT_UPTIME_FILE=str(uptime),
            ASTERINAS_SAFE_REBOOT_CONSOLE=str(console),
            ASTERINAS_SAFE_REBOOT_ACTIONS=str(actions),
            ASTERINAS_SAFE_REBOOT_UNIT_DIR=str(unit_dir),
        )

        result = subprocess.run(
            ["/bin/bash", str(SAFE_REBOOT_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

        self.assertNotEqual(result.returncode, 0)
        action_lines = actions.read_text(encoding="utf-8").splitlines()
        self.assertIn("systemctl:kill --kill-who=all --signal=KILL asterinas-desktop-evidence.service", action_lines)
        self.assertEqual(
            action_lines.count("systemctl:stop asterinas-desktop-evidence.service"),
            2,
        )
        self.assertFalse(any(line.startswith("sync:") for line in action_lines))
        self.assertIn(
            "ASTERINAS_USERSPACE_REBOOT_FAIL reason=unit-quiesce",
            console.read_text(encoding="utf-8").splitlines(),
        )

    def test_safe_reboot_refuses_sync_when_process_scan_fails(self) -> None:
        fake_bin = self.directory / "safe-reboot-pgrep-error-bin"
        fake_bin.mkdir()
        unit_dir = self.directory / "safe-reboot-pgrep-error-units"
        unit_dir.mkdir()
        (unit_dir / "asterinas-desktop.service").touch()
        actions = self.directory / "safe-reboot-pgrep-error-actions"
        console = self.directory / "safe-reboot-pgrep-error-console"
        uptime = self.directory / "safe-reboot-pgrep-error-uptime"
        uptime.write_text("100.00 80.00\n", encoding="utf-8")
        for name in ("systemctl", "pgrep", "sync", "reboot"):
            command = fake_bin / name
            command.write_text(
                "#!/bin/sh\n"
                'printf \'%s:%s\\n\' "$(basename "$0")" "$*" '
                '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n'
                + ("exit 2\n" if name == "pgrep" else ""),
                encoding="utf-8",
            )
            command.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_SAFE_REBOOT_AFTER="100",
            ASTERINAS_SAFE_REBOOT_UPTIME_FILE=str(uptime),
            ASTERINAS_SAFE_REBOOT_CONSOLE=str(console),
            ASTERINAS_SAFE_REBOOT_ACTIONS=str(actions),
            ASTERINAS_SAFE_REBOOT_UNIT_DIR=str(unit_dir),
        )

        result = subprocess.run(
            ["/bin/bash", str(SAFE_REBOOT_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

        self.assertNotEqual(result.returncode, 0)
        action_lines = actions.read_text(encoding="utf-8").splitlines()
        self.assertIn("pgrep:-u 1000", action_lines)
        self.assertFalse(any(line.startswith("sync:") for line in action_lines))
        self.assertFalse(any(line.startswith("reboot:") for line in action_lines))
        self.assertIn(
            "ASTERINAS_USERSPACE_REBOOT_FAIL reason=invalid-process-list",
            console.read_text(encoding="utf-8").splitlines(),
        )

    def test_safe_reboot_refuses_sync_while_user_writers_survive(self) -> None:
        fake_bin = self.directory / "safe-reboot-stuck-bin"
        fake_bin.mkdir()
        unit_dir = self.directory / "safe-reboot-stuck-units"
        unit_dir.mkdir()
        (unit_dir / "asterinas-desktop.service").touch()
        actions = self.directory / "safe-reboot-stuck-actions"
        actions.write_text("", encoding="utf-8")
        console = self.directory / "safe-reboot-stuck-console"
        console.write_text("", encoding="utf-8")
        uptime = self.directory / "safe-reboot-stuck-uptime"
        uptime.write_text("100.00 80.00\n", encoding="utf-8")
        commands = {
            "systemctl": "exit 0\n",
            "pgrep": "printf '41\\n'\nexit 0\n",
            "pkill": "exit 0\n",
            "sleep": "exit 0\n",
            "sync": "exit 0\n",
            "reboot": "exit 0\n",
        }
        for name, body in commands.items():
            command = fake_bin / name
            command.write_text(
                "#!/bin/sh\n"
                'printf \'%s:%s\\n\' "$(basename "$0")" "$*" '
                '>>"$ASTERINAS_SAFE_REBOOT_ACTIONS"\n'
                f"{body}",
                encoding="utf-8",
            )
            command.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_SAFE_REBOOT_AFTER="100",
            ASTERINAS_SAFE_REBOOT_UPTIME_FILE=str(uptime),
            ASTERINAS_SAFE_REBOOT_CONSOLE=str(console),
            ASTERINAS_SAFE_REBOOT_ACTIONS=str(actions),
            ASTERINAS_SAFE_REBOOT_UNIT_DIR=str(unit_dir),
            ASTERINAS_SAFE_REBOOT_TERM_WAIT_SECONDS="1",
            ASTERINAS_SAFE_REBOOT_KILL_WAIT_SECONDS="1",
        )

        result = subprocess.run(
            ["/bin/bash", str(SAFE_REBOOT_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

        self.assertNotEqual(result.returncode, 0)
        action_lines = actions.read_text(encoding="utf-8").splitlines()
        self.assertIn("pkill:-TERM -u 1000", action_lines)
        self.assertIn("pkill:-KILL -u 1000", action_lines)
        self.assertFalse(any(line.startswith("sync:") for line in action_lines))
        self.assertFalse(any(line.startswith("reboot:") for line in action_lines))
        console_lines = console.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            "ASTERINAS_USERSPACE_REBOOT_PROCESS state=signal signal=TERM count=1",
            console_lines,
        )
        self.assertIn(
            "ASTERINAS_USERSPACE_REBOOT_PROCESS state=signal signal=KILL count=1",
            console_lines,
        )
        self.assertIn(
            "ASTERINAS_USERSPACE_REBOOT_FAIL reason=writers-remain-1",
            console_lines,
        )
        self.assertFalse(
            any("ASTERINAS_USERSPACE_REBOOT_QUIESCE_DONE" in line for line in console_lines)
        )
        self.assertFalse(
            any("ASTERINAS_USERSPACE_REBOOT_SYNC_DONE" in line for line in console_lines)
        )

    def test_safe_reboot_defaults_off_and_treats_zero_as_off(self) -> None:
        cmdline = self.directory / "safe-reboot-default-cmdline"
        cmdline.write_text("console=ttyS0\n", encoding="utf-8")
        for value in (None, "0"):
            environment = os.environ.copy()
            environment.pop("ASTERINAS_SAFE_REBOOT_AFTER", None)
            environment["ASTERINAS_SAFE_REBOOT_CMDLINE_PATH"] = str(cmdline)
            if value is not None:
                environment["ASTERINAS_SAFE_REBOOT_AFTER"] = value
            result = subprocess.run(
                ["/bin/bash", str(SAFE_REBOOT_SCRIPT)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")

    def test_web_network_rejects_an_invalid_monotonic_clock(self) -> None:
        environment, console, _, _ = self._web_network_environment(
            self.directory / "web-invalid-uptime",
            mode="proxy",
        )
        Path(environment["ASTERINAS_DESKTOP_NETWORK_UPTIME_PATH"]).write_text(
            "not-an-uptime\n",
            encoding="utf-8",
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
            "DEBIAN_WEB_NETWORK_FAIL mode=proxy layer=config reason=monotonic-clock",
        )

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
        environment, console, resolv_conf, command_log = self._web_network_environment(
            self.directory / "web-proxy", mode="proxy"
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
            sum(line.count(FIXTURE_PATH) for line in commands),
            20,
        )
        external = [
            line for line in commands if "www.baidu.com" in line or "result.png" in line
        ]
        self.assertTrue(external)
        self.assertTrue(
            all("--proxy http://10.100.19.216:17893" in line for line in external)
        )
        self.assertFalse(any("http://www.baidu.com/" in line for line in commands))
        fixture_http = [line for line in commands if FIXTURE_PATH in line][0]
        self.assertIn("--dump-header", fixture_http)
        fixture_calls = [line for line in commands if FIXTURE_PATH in line]
        self.assertEqual(len(fixture_calls), 2)
        self.assertEqual(fixture_calls[1].count(FIXTURE_PATH), 19)
        self.assertEqual(fixture_calls[1].count("--output"), 19)
        self.assertFalse(any(line.startswith("getent ") for line in commands))
        self.assertEqual(resolv_conf.read_text(), "nameserver 192.0.2.53\n")

    def test_direct_web_network_has_no_proxy_configuration(self) -> None:
        environment, console, resolv_conf, command_log = self._web_network_environment(
            self.directory / "web-direct", mode="direct"
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
            any(
                line.startswith("ping ")
                for line in command_log.read_text().splitlines()
            )
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
            any(
                line.startswith("ip neigh ")
                for line in command_log.read_text().splitlines()
            )
        )


if __name__ == "__main__":
    unittest.main()
