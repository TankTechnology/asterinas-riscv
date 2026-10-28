#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from tools.riscv.tests.support.browser_web import *


class BrowserWebRuntimeTests(_BrowserWebFixtures, unittest.TestCase):
    def test_desktop_network_profile_requires_prebuilt_startup_caches(self) -> None:
        builder = ROOTFS / "build_rootfs.sh"
        tools = subprocess.run(
            [str(builder), "--profile", "browser-web", "--print-tools"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertNotIn("systemd-sysusers", tools)
        self.assertNotIn("journalctl", tools)
        for profile, expected in (
            ("browser-web", 0),
            ("minimal", 1),
        ):
            with self.subTest(profile=profile):
                result = subprocess.run(
                    [
                        "/bin/bash",
                        "-c",
                        'source "$1"; profile_uses_startup_caches "$2"',
                        "startup-cache-profile-test",
                        str(builder),
                        profile,
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_qemu_runner_has_one_slirp_virtio_nic_and_fail_closed_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("u-boot", "boot.ext4", "root.ext2"):
                (root / name).write_bytes(name.encode())
            argv = browser_web_qemu_argv(
                uboot=root / "u-boot",
                boot_disk=root / "boot.ext4",
                root_disk=root / "root.ext2",
                monitor_socket=root / "monitor.sock",
            )
        self.assertEqual(BrowserWebQemuOperations.SCHEMA_VERSION, 7)
        self.assertEqual(BrowserWebQemuOperations.PROFILE_NAME, "browser-web")
        self.assertNotIn("-nic", argv)
        self.assertEqual(argv.count("user,id=net0"), 1)
        self.assertEqual(argv.count("virtio-net-device,netdev=net0"), 1)
        root_drives = [value for value in argv if ",id=rootdisk," in value]
        self.assertEqual(len(root_drives), 1)
        self.assertIn("cache=writeback", root_drives[0])
        self.assertNotIn("cache=directsync", root_drives[0])
        self.assertNotIn("cache=unsafe", root_drives[0])
        self.assertNotIn("-gdb", argv)
        passing = ("\n".join(BROWSER_WEB_MILESTONES) + "\n").encode()
        self.assertTrue(
            classify_browser_web_qemu(passing, expected_debian_release="13.6").passed
        )
        self.assertLess(
            BROWSER_WEB_MILESTONES.index(
                "DEBIAN_BROWSER_WEB_TRUST_STATIC xul_ckbi=audited ca_bundle=audited package_closure=verified"
            ),
            BROWSER_WEB_MILESTONES.index(
                "DEBIAN_BROWSER_WEB_NETWORK mode=direct nic=virtio-slirp dns=10.0.2.3 https=curl-verified"
            ),
        )
        for marker in (
            b"DEBIAN_BROWSER_WEB_FAIL reason=challenge",
            b"DEBIAN_NETWORK_FAIL reason=qemu-https",
            *KERNEL_FATAL_MARKERS,
        ):
            with self.subTest(marker=marker):
                result = classify_browser_web_qemu(
                    passing + marker + b"\n", expected_debian_release="13.6"
                )
                self.assertFalse(result.passed)

    def test_qemu_progress_tracks_split_structured_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = web_qemu_gate.PinnedOutputDirectory(directory)
            self.addCleanup(output.close)
            progress = web_qemu_gate.BrowserWebProgress(output, boot_number=2)
            stderr = io.StringIO()
            with mock.patch.object(sys, "stderr", stderr):
                progress(
                    b"unstructured page text\n"
                    b"DEBIAN_WEB_NETWORK_READY mode=direct layers=10"
                )
                self.assertFalse(
                    (Path(directory) / "browser-web-progress.json").exists()
                )
                progress(b"\nA_WEB_PHASE phase=navigate-baidu-home state=sta")
                first = json.loads(
                    (Path(directory) / "browser-web-progress.json").read_text()
                )
                self.assertEqual(first["sequence"], 1)
                progress(
                    b"rt firefox_pid=70\n"
                    b"A_WEB_PHASE phase=navigate-baidu-home state=done firefox_pid=70\n"
                )

            payload = json.loads(
                (Path(directory) / "browser-web-progress.json").read_text()
            )
            self.assertEqual(
                payload,
                {
                    "active_phase": None,
                    "boot_number": 2,
                    "last_marker": (
                        "A_WEB_PHASE phase=navigate-baidu-home state=done "
                        "firefox_pid=70"
                    ),
                    "sequence": 3,
                },
            )
            self.assertNotIn("unstructured page text", stderr.getvalue())
            self.assertEqual(
                stderr.getvalue().count("browser-web-progress:"),
                3,
            )

    def test_qemu_timeout_reason_identifies_last_browser_phase(self) -> None:
        network = b"DEBIAN_WEB_NETWORK_READY mode=direct layers=10\n"
        cases = (
            (b"U-Boot only\n", "browser-timeout:network-direct"),
            (network, "browser-timeout:firefox-launch"),
            (
                network + b"A_WEB_PHASE phase=navigate-baidu-home state=start "
                b"firefox_pid=70\n",
                "browser-timeout:phase-navigate-baidu-home",
            ),
            (
                network + b"A_WEB_PHASE phase=navigate-baidu-home state=start "
                b"firefox_pid=70\n"
                + b"DEBIAN_BROWSER_WEB_PLATFORM_READY baidu_home=pass "
                b"bilibili_home=pass bilibili_detail=pass "
                b"bv=BV1Ab411c7De tls=verified\n",
                "browser-timeout:after-platform-ready",
            ),
        )
        for transcript, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    web_qemu_gate.browser_timeout_reason(
                        transcript, network_mode=NetworkMode.DIRECT
                    ),
                    expected,
                )

        operations = object.__new__(BrowserWebQemuOperations)
        config = mock.Mock(boot_timeout=7200.0)
        with mock.patch.object(web_qemu_gate.time, "monotonic", return_value=100.0):
            self.assertEqual(operations._protocol_deadline(config), 1000.0)

        operations.network_mode = NetworkMode.DIRECT
        session = {
            "serial": mock.Mock(
                transcript=(
                    network + b"A_WEB_PHASE phase=navigate-baidu-home state=start "
                    b"firefox_pid=70\n"
                )
            )
        }
        with mock.patch(
            "tools.riscv.debian.rootfs.desktop_m3_gate."
            "DesktopM3Operations.run_protocol",
            side_effect=TimeoutError("serial deadline"),
        ):
            with self.assertRaisesRegex(
                GateFailure, "browser-timeout:phase-navigate-baidu-home"
            ):
                operations.run_protocol(session, config)

        makefile = (ROOT / "Makefile").read_text()
        target = makefile.split(".PHONY: test_riscv_debian_browser_web_qemu_gate", 1)[
            1
        ].split(".PHONY:", 1)[0]
        self.assertIn("--boot-timeout 900", target)
        self.assertNotIn("--boot-timeout 7200", target)

    def test_qemu_gdb_stub_is_loopback_only_and_explicitly_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("u-boot", "boot.ext4", "root.ext2"):
                (root / name).write_bytes(name.encode())
            arguments = {
                "uboot": root / "u-boot",
                "boot_disk": root / "boot.ext4",
                "root_disk": root / "root.ext2",
                "monitor_socket": root / "monitor.sock",
            }
            argv = browser_web_qemu_argv(gdb_port=23456, **arguments)
            self.assertEqual(argv[-2:], ("-gdb", "tcp:127.0.0.1:23456"))
            for invalid in (True, 0, 1023, 65536, "23456"):
                with self.subTest(invalid=invalid):
                    with self.assertRaisesRegex(ValueError, "GDB port"):
                        browser_web_qemu_argv(gdb_port=invalid, **arguments)

    def test_qemu_gdb_environment_is_absent_by_default_and_strict(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_diagnostic_gdb_port())
        for value in ("", "abc", "1023", "65536", "+2345"):
            with self.subTest(value=value):
                with mock.patch.dict(
                    os.environ, {"ASTERINAS_QEMU_GDB_PORT": value}, clear=True
                ):
                    with self.assertRaisesRegex(ValueError, "ASTERINAS_QEMU_GDB_PORT"):
                        _diagnostic_gdb_port()
        with mock.patch.dict(
            os.environ, {"ASTERINAS_QEMU_GDB_PORT": "23456"}, clear=True
        ):
            self.assertEqual(_diagnostic_gdb_port(), 23456)

    def test_external_baidu_marker_does_not_mask_a_later_guest_failure(self) -> None:
        operations = object.__new__(BrowserWebQemuOperations)
        session = {"serial": mock.Mock(transcript=b"prefix\n" + _EXTERNAL_BLOCK)}
        with mock.patch(
            "tools.riscv.debian.rootfs.desktop_m3_gate.DesktopM3Operations.run_protocol",
            side_effect=GateFailure("guest reported desktop failure"),
        ):
            with self.assertRaisesRegex(GateFailure, "guest reported desktop failure"):
                operations.run_protocol(session, mock.sentinel.config)

    def test_qemu_gate_stops_immediately_on_stage1_failure(self) -> None:
        self.assertIn(
            b"DEBIAN_ROOTFS_FAIL reason=",
            BrowserWebQemuOperations.ADDITIONAL_FAILURE_MARKERS,
        )

        operations = object.__new__(BrowserWebQemuOperations)
        session = {
            "serial": mock.Mock(transcript=b"DEBIAN_ROOTFS_FAIL reason=dev-bind\r\n")
        }
        with mock.patch(
            "tools.riscv.debian.rootfs.desktop_m3_gate."
            "DesktopM3Operations.run_protocol",
            side_effect=GateFailure("guest reported desktop failure"),
        ):
            with self.assertRaisesRegex(GateFailure, "stage1 rootfs failure: dev-bind"):
                operations.run_protocol(session, mock.sentinel.config)

    def test_kernel_fatal_drains_a_bounded_serial_tail(self) -> None:
        operations = object.__new__(BrowserWebQemuOperations)
        serial = mock.Mock(transcript=b"prefix\n" + KERNEL_FATAL_MARKERS[0])
        session = {"serial": serial}
        config = mock.Mock(cleanup_timeout=3.0)
        with (
            mock.patch(
                "tools.riscv.debian.rootfs.desktop_m3_gate.DesktopM3Operations.run_protocol",
                side_effect=GateFailure("guest reported desktop failure"),
            ),
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_qemu_gate.time.monotonic",
                return_value=100.0,
            ),
        ):
            with self.assertRaisesRegex(GateFailure, "guest reported desktop failure"):
                operations.run_protocol(session, config)
        serial.drain.assert_called_once_with(103.0)

    def test_baidu_search_is_submitted_from_live_homepage(self) -> None:
        client = mock.Mock()
        client.command.return_value = {"value": "search-click-scheduled"}
        _submit_baidu_search(client)
        command, arguments = client.command.call_args.args
        self.assertEqual(command, "WebDriver:ExecuteScript")
        self.assertIn("document.querySelector('#kw')", arguments["script"])
        self.assertIn("document.querySelector('#su')", arguments["script"])
        self.assertIn("submit.click()", arguments["script"])
        run_source = inspect.getsource(
            __import__(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate",
                fromlist=["run_gate"],
            ).run_gate
        )
        self.assertIn("_submit_baidu_search(client)", run_source)
        self.assertNotIn("_navigate(client, BAIDU_SEARCH)", run_source)
        client.command.return_value = {"value": "missing-controls"}
        with self.assertRaisesRegex(GateError, "could not be submitted"):
            _submit_baidu_search(client)

    def test_baidu_home_scope_uses_one_session_and_stops_after_evidence(self) -> None:
        ready = snapshot("https://www.baidu.com/")
        ready["title"] = "百度一下，你就知道"
        ready["bodyText"] = "百度一下，你就知道 新闻 地图 视频 学术 更多产品"
        ready["dom"]["baiduLogo"] = True
        ready["dom"]["baiduKeyword"] = True
        ready["dom"]["baiduSubmit"] = True
        client = mock.Mock()

        def command(name: str, _parameters: object | None = None) -> object:
            if name == "WebDriver:NewSession":
                return {
                    "value": {
                        "sessionId": "session-1",
                        "capabilities": {"acceptInsecureCerts": False},
                    }
                }
            if name == "WebDriver:GetWindowHandles":
                return {"value": ["window-1"]}
            if name in {"WebDriver:Navigate", "WebDriver:GetTitle"}:
                return {"value": None}
            if name == "WebDriver:ExecuteScript":
                return {
                    "value": json.dumps(
                        {
                            "url": "https://www.baidu.com/",
                            "readyState": "interactive",
                        }
                    )
                }
            self.fail(f"unexpected Marionette command: {name}")

        client.command.side_effect = command
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate._connect",
                return_value=client,
            ) as connect,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate._wait_for_probe",
                return_value=(ready, None),
            ) as wait_probe,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate._snapshot",
                return_value=ready,
            ) as take_snapshot,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate._write_evidence"
            ) as write_evidence,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate."
                "fixture_index_url_from_environment"
            ) as fixture_url,
        ):
            result = run_baidu_home_gate(
                "127.0.0.1", 2828, 30, Path(directory), firefox_pid=116
            )

        self.assertEqual(result, ready)
        connect.assert_called_once()
        wait_probe.assert_called_once()
        command_names = [call.args[0] for call in client.command.call_args_list]
        self.assertLess(
            command_names.index("WebDriver:GetTitle"),
            command_names.index("WebDriver:ExecuteScript"),
        )
        ping_script = next(
            call.args[1]["script"]
            for call in client.command.call_args_list
            if call.args[0] == "WebDriver:ExecuteScript"
        )
        self.assertNotIn("document.body", ping_script)
        self.assertNotIn("querySelector", ping_script)
        take_snapshot.assert_called_once_with(client)
        write_evidence.assert_called_once_with(
            client, Path(directory), "baidu-home", ready, mock.ANY
        )
        fixture_url.assert_not_called()
        self.assertNotIn(
            "WebDriver:DeleteSession",
            [call.args[0] for call in client.command.call_args_list],
        )
        client.close.assert_called_once_with()

    def test_baidu_home_cli_selects_only_the_lightweight_scope(self) -> None:
        result = snapshot("https://www.baidu.com/")
        result["title"] = "百度一下，你就知道"
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate."
                "validate_network_namespace"
            ) as validate_namespace,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate."
                "run_baidu_home_gate",
                return_value=result,
            ) as run_home,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate.run_gate"
            ) as run_full,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            evidence = Path(directory)
            status = marionette_gate_main(
                [
                    "--scope",
                    "baidu-home",
                    "--firefox-pid",
                    "116",
                    "--timeout",
                    "30",
                    "--evidence-dir",
                    str(evidence),
                ]
            )

        self.assertEqual(status, 0)
        validate_namespace.assert_called_once_with(116)
        run_home.assert_called_once_with("127.0.0.1", 2828, 30.0, evidence, 116)
        run_full.assert_not_called()
        marker = json.loads(stdout.getvalue())
        self.assertEqual(marker["scope"], "baidu-home")
        self.assertEqual(marker["url"], "https://www.baidu.com/")
        self.assertRegex(marker["title_sha256"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual(marker["tls"], "verified")

    def test_bilibili_playback_scope_runs_without_the_qemu_fixture(self) -> None:
        selected = "https://www.bilibili.com/video/BV1Ab411c7De/"
        home = snapshot("https://www.bilibili.com/")
        home["dom"]["bilibiliHome"] = True
        home["links"] = [selected]
        detail = snapshot(selected, tls=0)
        detail["dom"]["bilibiliDetail"] = True
        playback = json.loads(web_evidence()["bilibili-detail.json"])["playback"]
        client = mock.Mock()

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate._connect",
                return_value=client,
            ),
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate."
                "_start_webdriver_session"
            ),
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate._navigate"
            ) as navigate,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate."
                "_wait_for_probe"
            ) as wait_probe,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate._snapshot",
                side_effect=[home, detail],
            ),
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate."
                "_wait_for_bilibili_playback",
                return_value=playback,
            ) as wait_playback,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate."
                "_write_evidence"
            ) as write_evidence,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate."
                "fixture_index_url_from_environment"
            ) as fixture_url,
        ):
            observed_bv, observed_playback = run_bilibili_playback_gate(
                "127.0.0.1", 2828, 30, Path(directory), firefox_pid=116
            )

        self.assertEqual(observed_bv, "BV1Ab411c7De")
        self.assertEqual(observed_playback, playback)
        self.assertEqual(
            [call.args[1] for call in navigate.call_args_list],
            ["https://www.bilibili.com/", selected],
        )
        self.assertEqual(wait_probe.call_count, 2)
        wait_playback.assert_called_once_with(client, selected, mock.ANY)
        self.assertEqual(write_evidence.call_count, 2)
        fixture_url.assert_not_called()
        client.close.assert_called_once_with()

    def test_bilibili_playback_cli_selects_the_physical_scope(self) -> None:
        playback = json.loads(web_evidence()["bilibili-detail.json"])["playback"]
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate."
                "validate_network_namespace"
            ) as validate_namespace,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate."
                "run_bilibili_playback_gate",
                return_value=("BV1Ab411c7De", playback),
            ) as run_playback,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate.run_gate"
            ) as run_full,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            evidence = Path(directory)
            status = marionette_gate_main(
                [
                    "--scope",
                    "bilibili-playback",
                    "--firefox-pid",
                    "116",
                    "--timeout",
                    "30",
                    "--evidence-dir",
                    str(evidence),
                ]
            )

        self.assertEqual(status, 0)
        validate_namespace.assert_called_once_with(116)
        run_playback.assert_called_once_with(
            "127.0.0.1", 2828, 30.0, evidence, 116
        )
        run_full.assert_not_called()
        marker = json.loads(stdout.getvalue())
        self.assertEqual(marker["marker"], "DEBIAN_BROWSER_WEB_BILIBILI_PLAYBACK")
        self.assertEqual(marker["scope"], "bilibili-playback")
        self.assertEqual(marker["bv"], "BV1Ab411c7De")
        self.assertEqual(marker["status"], "pass")
        self.assertEqual(marker["muted"], True)

    def test_controlled_fixture_search_is_submitted_from_the_real_form(self) -> None:
        client = mock.Mock()
        client.command.return_value = {"value": "fixture-search-scheduled"}
        _submit_fixture_search(client)
        command, arguments = client.command.call_args.args
        self.assertEqual(command, "WebDriver:ExecuteScript")
        self.assertIn('input[name="q"]', arguments["script"])
        self.assertIn("requestSubmit", arguments["script"])
        self.assertIn("query.value = 'asterinas'", arguments["script"])
        client.command.return_value = {"value": "missing-controls"}
        with self.assertRaisesRegex(GateError, "fixture search form"):
            _submit_fixture_search(client)

    def test_controlled_fixture_download_is_activated_and_hashed(self) -> None:
        client = mock.Mock()
        client.command.return_value = {"value": "fixture-download-scheduled"}
        with mock.patch(
            "tools.riscv.debian.rootfs.browser_web_marionette_gate."
            "FIXTURE_DOWNLOAD_FILE",
            Path("/definitely-absent/asterinas-browser-quality.bin"),
        ):
            _trigger_fixture_download(client)
        command, arguments = client.command.call_args.args
        self.assertEqual(command, "WebDriver:ExecuteScript")
        self.assertIn("#quality-download", arguments["script"])
        self.assertIn("fixture-download-scheduled", arguments["script"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            download = root / "asterinas-browser-quality.bin"
            evidence = root / "evidence"
            download.write_bytes(bytes(range(256)) * 1024)
            result = _wait_for_fixture_download(
                download, time.monotonic() + 2, evidence, os.getuid()
            )
            self.assertEqual(result["bytes"], 256 * 1024)
            self.assertEqual(result["filename"], download.name)
            self.assertEqual(
                json.loads((evidence / "fixture-download.json").read_text()),
                result,
            )
            download.write_bytes(b"forged")
            with self.assertRaisesRegex(GateError, "did not complete"):
                _wait_for_fixture_download(
                    download, time.monotonic() + 0.01, evidence, os.getuid()
                )
            download.write_bytes(bytes(range(256)) * 1024)
            with self.assertRaisesRegex(GateError, "not a safe regular file"):
                _wait_for_fixture_download(
                    download, time.monotonic() + 0.01, evidence, os.getuid() + 1
                )

    def test_controlled_fixture_url_and_evidence_are_exact(self) -> None:
        environment = {
            "ASTERINAS_DESKTOP_FIXTURE_URL": (
                "http://10.0.2.2:17894/asterinas-network-probe.bin"
            )
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            fixture_url = fixture_index_url_from_environment()
        self.assertEqual(
            fixture_url,
            "http://10.0.2.2:17894/browser-quality/index.html",
        )
        for forged in (
            "https://10.0.2.2:17894/asterinas-network-probe.bin",
            "http://127.0.0.1:17894/asterinas-network-probe.bin",
            "http://10.0.2.2:17895/asterinas-network-probe.bin",
            "http://10.0.2.2:17894/browser-quality/index.html",
        ):
            with (
                self.subTest(forged=forged),
                mock.patch.dict(
                    os.environ, {"ASTERINAS_DESKTOP_FIXTURE_URL": forged}, clear=True
                ),
                self.assertRaisesRegex(GateError, "fixture URL"),
            ):
                fixture_index_url_from_environment()

        search_url = f"{fixture_url}?q=asterinas"
        fixture = snapshot(search_url, tls=0)
        fixture["title"] = "asterinas - Asterinas Browser Quality"
        fixture["bodyText"] = "Asterinas browser quality / 浏览器质量"
        for field in ("fixtureQuery", "fixtureImage", "fixtureSecond"):
            fixture["dom"][field] = True
        fixture["browserCapabilities"] = fixture_capabilities("search")
        fixture["resources"] = [
            {
                "name": "http://10.0.2.2:17894/browser-quality/pattern.png",
                "initiatorType": "img",
                "duration": 1,
                "transferSize": 123,
            }
        ]
        validate_fixture_search(fixture, search_url)
        probe = {
            key: value
            for key, value in fixture.items()
            if key
            in {
                "url",
                "title",
                "readyState",
                "bodyText",
                "jsComplete",
                "browserCapabilities",
                "dom",
            }
        }
        probe_fixture_search(probe, search_url)
        home = copy.deepcopy(probe)
        home["url"] = fixture_url
        home["title"] = "Asterinas Browser Quality"
        home["browserCapabilities"] = fixture_capabilities("home")
        probe_fixture_home(home, fixture_url)
        home["browserCapabilities"]["checks"]["wasm"] = False
        with self.assertRaisesRegex(GateError, "capability checks"):
            probe_fixture_capabilities(
                {**home, "url": fixture_url + "?capabilities=1"},
                fixture_url + "?capabilities=1",
            )
        fixture["resources"][0]["name"] = "http://127.0.0.1:17894/forged.png"
        with self.assertRaisesRegex(GateError, "outside its frozen origin"):
            validate_fixture_search(fixture, search_url)

    def test_marionette_script_value_accepts_wrapped_and_raw_results(self) -> None:
        self.assertEqual(_script_value({"value": "wrapped"}), "wrapped")
        self.assertEqual(_script_value("raw"), "raw")
        self.assertEqual(_script_value({"other": "field"}), {"other": "field"})

    def test_marionette_navigate_accepts_raw_and_wrapped_null(self) -> None:
        client = mock.Mock()
        client.command.side_effect = [None, {"value": None}, {"value": "bad"}]
        _navigate(client, "https://www.baidu.com/")
        _navigate(client, "https://www.bilibili.com/")
        with self.assertRaisesRegex(GateError, "invalid Navigate"):
            _navigate(client, "https://example.invalid/")

    def test_about_blank_unload_boundary_is_strict_and_bounded(self) -> None:
        blank = snapshot("about:blank", tls=0)
        probe = {
            key: value
            for key, value in blank.items()
            if key
            in {
                "url",
                "title",
                "readyState",
                "bodyText",
                "jsComplete",
                "browserCapabilities",
                "dom",
            }
        }
        probe_about_blank(probe)
        for forged in (
            {**probe, "url": "https://www.baidu.com/"},
            {**probe, "readyState": "loading"},
            {**probe, "jsComplete": False},
        ):
            with self.subTest(forged=forged):
                with self.assertRaisesRegex(GateError, "about:blank"):
                    probe_about_blank(forged)

        gate = (ROOTFS / "browser_web_marionette_gate.py").read_text()
        self.assertNotIn('"clear-baidu-document"', gate)
        self.assertIn('"navigate-fixture-home"', gate)

    def test_clear_document_stops_busy_public_script_before_replacement(self) -> None:
        client = mock.Mock()
        client.command.return_value = {"value": "document-stopped"}
        _clear_document(client, time.monotonic() + 5)
        command, arguments = client.command.call_args.args
        self.assertEqual(command, "WebDriver:ExecuteScript")
        self.assertIn("window.stop()", arguments["script"])
        self.assertIn("replaceChildren()", arguments["script"])

    def test_marionette_wait_retries_transient_null_probe(self) -> None:
        ready = snapshot("https://www.baidu.com/")
        ready["dom"]["baiduKeyword"] = True
        ready["dom"]["baiduSubmit"] = True
        ready["dom"]["baiduLogo"] = True
        ready = {
            key: value
            for key, value in ready.items()
            if key
            in {
                "url",
                "title",
                "readyState",
                "bodyText",
                "jsComplete",
                "browserCapabilities",
                "dom",
            }
        }
        client = mock.Mock()
        client.command.side_effect = [
            {"value": None},
            {"value": json.dumps(ready)},
        ]
        with mock.patch(
            "tools.riscv.debian.rootfs.browser_web_marionette_gate.time.sleep"
        ):
            observed, result = _wait_for_probe(
                client, probe_baidu_home, time.monotonic() + 5
            )
        self.assertEqual(observed, ready)
        self.assertIsNone(result)
        self.assertEqual(client.command.call_count, 2)

    def test_probe_diagnostic_callback_is_bounded_and_does_not_write_stderr(
        self,
    ) -> None:
        messages = []
        attempts = 0

        def validate(probe):
            nonlocal attempts
            attempts += 1
            if attempts <= 30:
                raise GateError("unavailable " * 500)
            return "ready"

        with (
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate._probe",
                return_value={},
            ),
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate.time.sleep"
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            probe, result = _wait_for_probe(
                mock.Mock(),
                validate,
                time.monotonic() + 5,
                diagnostic_fn=messages.append,
            )
        self.assertEqual((probe, result), ({}, "ready"))
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(messages), 16)
        self.assertTrue(all(len(message) <= 2048 for message in messages))

    def test_marionette_reports_running_fixture_capabilities_once(self) -> None:
        url = "http://10.0.2.2:17894/browser-quality/index.html"
        running = snapshot(url, tls=0)
        running["title"] = "Asterinas Browser Quality"
        running["bodyText"] = "Asterinas browser quality / 浏览器质量"
        for field in ("fixtureQuery", "fixtureImage", "fixtureSecond"):
            running["dom"][field] = True
        running["browserCapabilities"] = {
            "version": 1,
            "phase": "home",
            "state": "running",
            "checks": {"localStorage": True, "canvas": True},
            "error": None,
        }
        complete = copy.deepcopy(running)
        complete["browserCapabilities"] = fixture_capabilities("home")
        fields = {
            "url",
            "title",
            "readyState",
            "bodyText",
            "jsComplete",
            "browserCapabilities",
            "dom",
        }
        client = mock.Mock()
        client.command.side_effect = [
            {"value": json.dumps({key: running[key] for key in fields})},
            {"value": json.dumps({key: complete[key] for key in fields})},
        ]
        with (
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate.time.sleep"
            ),
            mock.patch("builtins.print") as printed,
        ):
            observed, result = _wait_for_probe(
                client,
                lambda probe: probe_fixture_home(probe, url),
                time.monotonic() + 5,
            )
        self.assertEqual(observed["browserCapabilities"]["state"], "complete")
        self.assertIsNone(result)
        capability_lines = [
            call.args[0]
            for call in printed.call_args_list
            if call.args and str(call.args[0]).startswith("A_WEB_PROBE_CAPABILITIES")
        ]
        self.assertEqual(len(capability_lines), 1)
        self.assertIn("state=running", capability_lines[0])
        self.assertIn('checks={"canvas":true,"localStorage":true}', capability_lines[0])

    def test_marionette_search_follows_a_new_window(self) -> None:
        old = snapshot("about:blank")
        search = snapshot("https://www.baidu.com/s?wd=Asterinas", tls=0)
        search["dom"]["baiduResults"] = 2
        old_probe = {
            key: value
            for key, value in old.items()
            if key
            in {
                "url",
                "title",
                "readyState",
                "bodyText",
                "jsComplete",
                "browserCapabilities",
                "dom",
            }
        }
        search_probe = {
            key: value
            for key, value in search.items()
            if key
            in {
                "url",
                "title",
                "readyState",
                "bodyText",
                "jsComplete",
                "browserCapabilities",
                "dom",
            }
        }
        client = mock.Mock()
        client.command.side_effect = [
            {"value": ["old-window", "search-window"]},
            {"value": None},
            {"value": json.dumps(old_probe)},
            None,
            {"value": json.dumps(search_probe)},
            {"value": json.dumps(search)},
        ]
        with mock.patch(
            "tools.riscv.debian.rootfs.browser_web_marionette_gate.time.sleep"
        ):
            observed, result = _wait_across_windows(
                client,
                probe_baidu_search,
                validate_baidu_search,
                time.monotonic() + 5,
            )
        self.assertEqual(observed, search)
        self.assertIsNone(result)
        self.assertEqual(
            client.command.call_args_list[3],
            mock.call(
                "WebDriver:SwitchToWindow",
                {"handle": "search-window", "focus": False},
            ),
        )

    def test_marionette_records_a_strict_external_captcha_outcome(self) -> None:
        challenge = snapshot(
            "https://wappass.baidu.com/static/captcha/tuxing_v2.html?"
            "backurl=https%3A%2F%2Fwww.baidu.com%2Fs%3Fwd%3DAsterinas"
        )
        probe = {
            key: value
            for key, value in challenge.items()
            if key
            in {
                "url",
                "title",
                "readyState",
                "bodyText",
                "jsComplete",
                "browserCapabilities",
                "dom",
            }
        }
        client = mock.Mock()
        client.command.side_effect = [
            {"value": ["challenge-window"]},
            {"value": None},
            {"value": json.dumps(probe)},
            {"value": json.dumps(challenge)},
        ]
        observed, outcome = _wait_baidu_search_outcome(client, time.monotonic() + 5)
        self.assertEqual(observed, challenge)
        self.assertEqual(outcome, "external-captcha")

    def test_online_readiness_probe_avoids_layout_and_full_enumeration(self) -> None:
        client = mock.Mock()
        probe = snapshot("https://www.baidu.com/")
        probe["dom"]["baiduKeyword"] = True
        probe["dom"]["baiduSubmit"] = True
        probe["dom"]["baiduLogo"] = True
        probe = {
            key: value
            for key, value in probe.items()
            if key
            in {
                "url",
                "title",
                "readyState",
                "bodyText",
                "jsComplete",
                "browserCapabilities",
                "dom",
            }
        }
        client.command.return_value = {"value": json.dumps(probe)}
        self.assertEqual(_probe(client), probe)
        script = client.command.call_args.args[1]["script"]
        self.assertNotIn("innerText", script)
        self.assertNotIn("getBoundingClientRect", script)
        self.assertNotIn("getEntriesByType", script)
        self.assertNotIn("querySelectorAll('a[href*=", script)

    def test_marionette_gate_reports_screenshot_and_search_phases(self) -> None:
        gate = (ROOTFS / "browser_web_marionette_gate.py").read_text()
        for phase in (
            "evidence-baidu-home",
            "submit-fixture-search",
            "snapshot-fixture-search",
            "submit-baidu-search",
            "snapshot-baidu-search",
            "navigate-bilibili-home",
            "probe-bilibili-detail",
            "snapshot-bilibili-detail",
        ):
            self.assertIn(f'"{phase}"', gate)
        self.assertIn('phase(name, "start")', gate)
        self.assertIn('phase(name, "exception", error)', gate)
        self.assertIn('phase(name, "done")', gate)

    def test_gecko_profiler_diagnostic_is_bounded_and_opt_in(self) -> None:
        gate = (ROOTFS / "browser_web_marionette_gate.py").read_text()
        evidence = (ROOTFS / "browser_web_evidence.sh").read_text()
        service = (ROOTFS / "browser_web_evidence.service").read_text()
        diagnostic = (ROOTFS / "browser_web_diagnostic.conf").read_text()
        self.assertIn("ASTERINAS_BROWSER_WEB_TIMEOUT_SECONDS=540", service)
        self.assertIn("ASTERINAS_BROWSER_WEB_FORMAL_TIMEOUT_SECONDS=480", service)
        self.assertIn("TimeoutStartSec=570s", service)
        self.assertIn('os.environ.get("ASTERINAS_FIREFOX_GECKO_PROFILE") == "1"', gate)
        self.assertIn('"gecko-profiler-verify"', gate)
        self.assertNotIn("SIGUSR1", gate)
        self.assertIn('"MOZ_PROFILER_STARTUP_ENTRIES": "262144"', gate)
        self.assertIn('"MOZ_PROFILER_STARTUP_INTERVAL": "10"', gate)
        self.assertIn("kill -USR2", evidence)
        self.assertIn("DEBIAN_BROWSER_WEB_GECKO_PROFILE state=ready", evidence)
        self.assertIn("DEBIAN_BROWSER_WEB_CHILD_DIAGNOSTIC", evidence)
        self.assertIn("DETAIL_DIAGNOSTIC_MARKER", gate)
        self.assertIn("DETAIL_DIAGNOSTIC_MARKER", evidence)
        self.assertIn("/usr/bin/timeout 5 /usr/bin/ps", evidence)
        self.assertIn("HOT_MAPS_DIAGNOSTIC_MARKER", evidence)
        self.assertIn("DEBIAN_BROWSER_WEB_HOT_PID", evidence)
        self.assertIn("DEBIAN_BROWSER_WEB_HOT_MAP pid=", evidence)
        self.assertIn('[[ "$candidate_stat" == R* ]]', evidence)
        self.assertIn("hot_seconds", evidence)
        self.assertIn('[[ "$cmdline" == *" -contentproc "*', evidence)
        self.assertNotIn('[[ "$process_line" == *" tab"* ]]', evidence)
        self.assertIn("HOT_MAP_MAX_LINES=128", evidence)
        self.assertIn('head -n "$HOT_MAP_MAX_LINES"', evidence)
        self.assertNotIn("head -n 4096", evidence)
        self.assertIn(
            '/usr/bin/timeout 10 /usr/bin/head -n "$HOT_MAP_MAX_LINES"', evidence
        )
        self.assertIn('"$PROC_ROOT/$hot_pid/maps"', evidence)
        sampler = evidence.split("start_gate_sampler()", 1)[1].split(
            "capture_gecko_profile()", 1
        )[0]
        self.assertNotIn('"$process/cmdline"', sampler)
        self.assertIn("/usr/bin/sync", evidence)
        self.assertNotIn("ASTERINAS_FIREFOX_GECKO_PROFILE", service)
        self.assertIn("Environment=ASTERINAS_FIREFOX_GECKO_PROFILE=1", diagnostic)
        self.assertIn("Environment=MOZ_PROFILER_STARTUP=1", diagnostic)
        self.assertIn("Environment=MOZ_PROFILER_STARTUP_INTERVAL=10", diagnostic)

    @mock.patch("pathlib.Path.read_bytes")

    def test_gecko_profiler_environment_is_exact(self, read_bytes: mock.Mock) -> None:
        read_bytes.return_value = (
            b"MOZ_PROFILER_STARTUP=1\0"
            b"MOZ_PROFILER_STARTUP_ENTRIES=262144\0"
            b"MOZ_PROFILER_STARTUP_INTERVAL=10\0"
            b"MOZ_PROFILER_STARTUP_FEATURES=js,leaf,ipcmessages,processcpu\0"
            b"MOZ_PROFILER_STARTUP_FILTERS=GeckoMain,DOM Worker,Compositor,Renderer,"
            b"Socket Thread,SwComposite,MediaDecoderStateMachine\0"
        )
        validate_gecko_profiler_environment(74)
        read_bytes.return_value = read_bytes.return_value.replace(
            b"MOZ_PROFILER_STARTUP_INTERVAL=10",
            b"MOZ_PROFILER_STARTUP_INTERVAL=1",
        )
        with self.assertRaisesRegex(GateError, "STARTUP_INTERVAL"):
            validate_gecko_profiler_environment(74)

    def test_post_stop_evidence_is_exact_and_rejects_combined_forgery(self) -> None:
        evidence = web_evidence()
        index = validate_web_evidence(evidence)
        self.assertEqual(set(index), set(WEB_EVIDENCE_PATHS))
        self.assertEqual(index["security.log"]["sandbox_outcome"], "enabled")
        self.assertEqual(index["timeline.log"]["clock_outcome"], "monotonic")
        forged = dict(evidence)
        forged["curl.log"] = (
            "DNS host=www.baidu.com address=1.2.3.4\n"
            "DNS host=www.bilibili.com address=5.6.7.8\n"
            "HTTPS requested=https://www.baidu.com/ status=200 effective=https://www.baidu.com/ verify=60\n"
            "HTTPS requested=https://www.bilibili.com/ status=200 effective=https://www.bilibili.com/ verify=60\n"
            "unrelated verify=0\n"
        ).encode()
        forged["security.log"] = (
            "BROWSER_WEB_SECURITY parent_pid=100 uid=1000 caps=zero nnp=1 sandbox_disable=absent\n"
        ).encode()
        forged["trust-static.log"] = b"FIREFOX_TRUST_PASS mode=garbage\n"
        for name in (
            "baidu-home",
            "baidu-search",
            "fixture-search",
            "bilibili-home",
            "bilibili-detail",
        ):
            forged[f"{name}.png"] = b"\x89PNG\r\n\x1a\n" + b"garbage" * 4
        with self.assertRaises(GateFailure):
            validate_web_evidence(forged)

        mutations = {
            "curl.log": forged["curl.log"],
            "security.log": forged["security.log"],
            "trust-static.log": forged["trust-static.log"],
            "baidu-home.png": forged["baidu-home.png"],
        }
        for name, contents in mutations.items():
            with self.subTest(name=name), self.assertRaises(GateFailure):
                validate_web_evidence({**evidence, name: contents})

        with self.assertRaisesRegex(GateFailure, "decoded size"):
            validate_web_evidence(
                {**evidence, "baidu-home.png": png_claiming_large_decode()}
            )
        with self.assertRaisesRegex(GateFailure, "pixel size"):
            validate_web_evidence(
                {**evidence, "baidu-home.png": png_with_second_zlib_stream()}
            )

        riscv_unsandboxed = {
            **evidence,
            "security.log": evidence["security.log"].replace(
                b"role=content caps=zero nnp=1 seccomp=2",
                b"role=content caps=zero nnp=1 seccomp=0",
            ),
        }
        index = validate_web_evidence(riscv_unsandboxed)
        self.assertEqual(
            index["security.log"]["sandbox_outcome"],
            "unavailable-firefox-riscv64-build",
        )
        for invalid in (
            b"seccomp=1",
            b"seccomp=0\nBROWSER_WEB_SECURITY child_pid=103 role=content caps=zero nnp=1 seccomp=2",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(GateFailure):
                validate_web_evidence(
                    {
                        **riscv_unsandboxed,
                        "security.log": riscv_unsandboxed["security.log"].replace(
                            b"seccomp=0", invalid, 1
                        ),
                    }
                )

    def test_post_stop_evidence_binds_selected_network_mode_and_profile(self) -> None:
        direct = web_evidence()
        proxy = proxy_web_evidence()
        validate_web_evidence(direct, network_mode=NetworkMode.DIRECT)
        validate_web_evidence(proxy, network_mode=NetworkMode.PROXY)
        for evidence, wrong_mode in (
            (direct, NetworkMode.PROXY),
            (proxy, NetworkMode.DIRECT),
        ):
            with self.subTest(mode=wrong_mode), self.assertRaises(GateFailure):
                validate_web_evidence(evidence, network_mode=wrong_mode)

        forged = dict(proxy)
        forged["firefox-user.js"] += (
            'user_pref("network.proxy.socks", "10.0.2.2");\n'
        ).encode()
        with self.assertRaisesRegex(GateFailure, "network profile"):
            validate_web_evidence(forged, network_mode=NetworkMode.PROXY)

    def test_uploaded_baidu_screenshot_is_bound_to_fixture_digest(self) -> None:
        payload = png()
        summary = {
            "bytes": len(payload),
            "path": "/browser-quality/capture.png",
            "peer": "127.0.0.1",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        self.assertEqual(
            validate_uploaded_baidu_screenshot(
                summary, payload, expected_payload=payload
            ),
            summary,
        )
        with self.assertRaisesRegex(GateFailure, "upload evidence"):
            validate_uploaded_baidu_screenshot({**summary, "sha256": "0" * 64}, payload)
        with self.assertRaisesRegex(GateFailure, "upload evidence"):
            validate_uploaded_baidu_screenshot(
                summary, payload, expected_payload=png(width=3)
            )

    def test_post_stop_evidence_rejects_external_captcha(self) -> None:
        evidence = web_evidence()
        challenge = snapshot(
            "https://wappass.baidu.com/static/captcha/tuxing_v2.html?"
            "backurl=https%3A%2F%2Fwww.baidu.com%2Fs%3Fwd%3DAsterinas"
        )
        challenge_evidence = {
            **evidence,
            "baidu-search.json": (json.dumps(challenge) + "\n").encode(),
        }
        with self.assertRaisesRegex(GateFailure, "did not produce a result"):
            validate_web_evidence(challenge_evidence)
        challenge["url"] = challenge["url"].replace("Asterinas", "forged")
        with self.assertRaisesRegex(GateError, "back URL"):
            validate_web_evidence(
                {
                    **challenge_evidence,
                    "baidu-search.json": (json.dumps(challenge) + "\n").encode(),
                }
            )

        trust_digest = hashlib.sha256(evidence["trust-static.log"]).hexdigest().encode()
        trust_mismatch = evidence["security.log"].replace(trust_digest, b"0" * 64)
        with self.assertRaises(GateFailure):
            validate_web_evidence({**evidence, "security.log": trust_mismatch})

        duplicate_ca = (
            evidence["security.log"]
            .replace(b"TRUST_STATIC_SHA256", b"SYSTEM_CA_SHA256")
            .replace(
                b"path=/usr/share/asterinas/browser-web-trust-static.log",
                b"path=/etc/ssl/certs/ca-certificates.crt",
            )
        )
        with self.assertRaises(GateFailure):
            validate_web_evidence({**evidence, "security.log": duplicate_ca})

        ca_digest = hashlib.sha256(evidence["ca-certificates.crt"]).hexdigest().encode()
        with self.assertRaisesRegex(GateFailure, "system CA"):
            validate_web_evidence(
                {
                    **evidence,
                    "security.log": evidence["security.log"].replace(
                        ca_digest, b"f" * 64
                    ),
                }
            )

        with self.assertRaisesRegex(GateFailure, "log exceeds"):
            validate_web_evidence(
                {**evidence, "firefox-stderr.log": b"x" * (16 * 1024 * 1024 + 1)}
            )

        for log_name, log in (
            (
                "firefox-stderr.log",
                b"[Parent 42] Exiting due to channel error.\n",
            ),
            (
                "firefox-mozilla.log",
                b"EPERM SCM_RIGHTS contains a file container\n",
            ),
        ):
            with self.subTest(log_name=log_name), self.assertRaises(GateFailure):
                validate_web_evidence({**evidence, log_name: log})

        split_nonfatal = {
            **evidence,
            "firefox-stderr.log": b"SCM_RIGHTS diagnostic enabled\n",
            "firefox-mozilla.log": b"unrelated operation returned EPERM\n",
        }
        validate_web_evidence(split_nonfatal)

        for changed in (
            b"BROWSER_WEB_SECURITY service_pid=999 nrestarts=0 stable=1 active=1",
            b"BROWSER_WEB_SECURITY service_pid=100 nrestarts=1 stable=1 active=1",
        ):
            original = (
                b"BROWSER_WEB_SECURITY service_pid=100 nrestarts=0 stable=1 active=1"
            )
            with self.subTest(changed=changed), self.assertRaises(GateFailure):
                validate_web_evidence(
                    {
                        **evidence,
                        "security.log": evidence["security.log"].replace(
                            original, changed
                        ),
                    }
                )

    def test_online_root_checker_rejects_non_slirp_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (
                "var/lib/dpkg/status",
                "usr/bin/getent",
                "usr/bin/curl",
                "usr/lib/firefox-esr/firefox-esr",
                "etc/nsswitch.conf",
                "etc/resolv.conf",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
                path.chmod(0o755)
            (root / "var/lib/dpkg/status").write_text(
                "".join(
                    f"Package: {package}\nStatus: install ok installed\n\n"
                    for package in ("firefox-esr", "ca-certificates", "curl")
                )
            )
            (root / "etc/nsswitch.conf").write_text("hosts: files dns\n")
            (root / "etc/resolv.conf").write_text("nameserver 1.1.1.1\n")
            launcher = root / "usr/bin/firefox-esr"
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.symlink_to("../lib/firefox-esr/firefox-esr")
            checker = root / "trust.py"
            checker.write_text("#!/bin/sh\nexit 0\n")
            checker.chmod(0o755)
            with mock.patch(
                "tools.riscv.debian.rootfs.browser_web_online_rootfs_check.riscv_elf"
            ):
                with self.assertRaisesRegex(OnlineCheckFailure, "slirp DNS"):
                    check_online_root(root, checker)

    def test_firefox_jit_overlay_rejects_unfrozen_package_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            packages = Path(directory) / "packages"
            root.mkdir()
            packages.mkdir()
            paths = []
            for name in (
                "firefox_143.0.3-1_riscv64.deb",
                "libnss3_3.116-1_riscv64.deb",
                "libvpx11_1.15.2-1_riscv64.deb",
            ):
                path = packages / name
                path.write_bytes(b"forged")
                paths.append(path)
            with self.assertRaisesRegex(OverlayError, "hash mismatch"):
                install_firefox_jit_overlay(root, paths)

    def test_firefox_jit_overlay_extracts_verified_private_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            package_directory = Path(directory) / "packages"
            root.mkdir()
            package_directory.mkdir()
            source_policy = root / "usr/lib/firefox-esr/distribution/policies.json"
            source_policy.parent.mkdir(parents=True)
            source_policy.write_text("{}\n")

            packages = tuple(
                OverlayPackage(
                    role=f"role-{index}",
                    filename=f"package-{index}.deb",
                    package=f"package-{index}",
                    version="1",
                    sha1="0" * 40,
                    sha256=hashlib.sha256(f"frozen-{index}".encode()).hexdigest(),
                )
                for index in range(3)
            )
            paths = []
            for index, package in enumerate(packages):
                path = package_directory / package.filename
                path.write_bytes(f"frozen-{index}".encode())
                paths.append(path)

            extracted = []

            def extract(snapshot: Path, stage: Path) -> None:
                extracted.append(snapshot.read_bytes())
                if len(extracted) == 1:
                    for path in paths:
                        path.write_bytes(b"mutated-after-verification")
                    launcher = stage / "usr/bin/firefox"
                    launcher.parent.mkdir(parents=True)
                    launcher.symlink_to("../lib/firefox/firefox")

            with (
                mock.patch(
                    "tools.riscv.debian.rootfs.firefox_jit_overlay.PACKAGES",
                    packages,
                ),
                mock.patch(
                    "tools.riscv.debian.rootfs.firefox_jit_overlay.RUNTIME_FILES",
                    {},
                ),
                mock.patch(
                    "tools.riscv.debian.rootfs.firefox_jit_overlay._extract_data",
                    side_effect=extract,
                ),
            ):
                install_firefox_jit_overlay(root, paths)

            self.assertEqual(
                extracted,
                [f"frozen-{index}".encode() for index in range(3)],
            )

    def test_trust_checker_accepts_exact_jit_overlay_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = {}
            for name, relative in OVERLAY_RUNTIME_PATHS.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"runtime-{name}".encode())
                path.chmod(0o755 if name == "firefox" else 0o644)
                runtime[name] = {
                    "path": relative,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            status = root / "var/lib/dpkg/status"
            status.parent.mkdir(parents=True)
            status.write_text(
                "Package: firefox-esr\nStatus: install ok installed\n\n"
                "Package: ca-certificates\nStatus: install ok installed\n\n"
            )
            ca = root / "etc/ssl/certs/ca-certificates.crt"
            ca.parent.mkdir(parents=True)
            ca.write_text(
                "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"
                * 100,
                encoding="ascii",
            )
            marker = root / "usr/share/asterinas/firefox-riscv-jit-overlay.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps(
                    {
                        "architecture": "riscv64",
                        "jit_default_commit": OVERLAY_COMMIT,
                        "packages": list(OVERLAY_PACKAGES),
                        "runtime_files": runtime,
                        "schema_version": 1,
                        "trust_mode": "system-nss-jit-overlay",
                    }
                )
                + "\n"
            )

            def inspect(*argv: str) -> str:
                if argv[0] == "file":
                    return "ELF 64-bit LSB shared object, UCB RISC-V\n"
                if argv[:2] == ("readelf", "-d"):
                    return "Shared library: [libnss3.so]\n"
                if argv[:2] == ("readelf", "-Ws"):
                    return (
                        "NSS_Initialize@@NSS_3.2\n"
                        if argv[-1].endswith("libnss3.so")
                        else "NSS_Initialize@NSS_3.2\nC_GetFunctionList\n"
                    )
                return ""

            with mock.patch(
                "tools.riscv.debian.rootfs.browser_web_trust_check.output",
                side_effect=inspect,
            ):
                self.assertIn("mode=system-nss-jit-overlay", check_trust_root(root))

    @mock.patch("tools.riscv.debian.rootfs.browser_web_qemu_gate.subprocess.run")

    def test_post_stop_extraction_is_bounded_and_uses_safe_basenames(
        self, run: mock.Mock
    ) -> None:
        run.side_effect = subprocess.TimeoutExpired("debugfs", 15)
        with tempfile.TemporaryDirectory(prefix="browser web output ") as directory:
            image = Path(directory) / "root image.ext2"
            image.write_bytes(b"root")
            fd = image.open("rb")
            try:
                with self.assertRaisesRegex(GateFailure, "timed out"):
                    _extract_web_evidence(fd.fileno(), Path(directory))
            finally:
                fd.close()
        argv = run.call_args.args[0]
        self.assertEqual(argv[2].rsplit(" ", 1)[-1], "baidu-home.json")
        self.assertNotIn(directory, argv[2])
        self.assertEqual(run.call_args.kwargs["timeout"], 15.0)

    def test_baidu_home_and_search_require_live_dom_and_https_timing(self) -> None:
        home = snapshot("https://www.baidu.com/")
        home["dom"]["baiduKeyword"] = True
        home["dom"]["baiduSubmit"] = True
        home["dom"]["baiduLogo"] = True
        validate_baidu_home(home)
        no_tls = copy.deepcopy(home)
        no_tls["navigation"]["secureConnectionStart"] = 0
        with self.assertRaisesRegex(GateError, "TLS"):
            validate_baidu_home(no_tls)

        search = snapshot("https://www.baidu.com/s?wd=Asterinas", tls=0)
        search["dom"]["baiduResults"] = 2
        validate_baidu_search(search)
        forged = copy.deepcopy(search)
        forged["url"] = "https://www.baidu.com/s?wd=Other"
        with self.assertRaisesRegex(GateError, "exact query"):
            validate_baidu_search(forged)

    def test_bilibili_uses_live_home_bv_and_exact_detail_identity(self) -> None:
        home = snapshot("https://www.bilibili.com/")
        home["dom"]["bilibiliHome"] = True
        home["links"] = [
            "https://example.invalid/video/BVFORGED/",
            "https://www.bilibili.com/video/BV9VisibleFirst/",
            "https://www.bilibili.com/video/BV1Ab411c7De/",
        ]
        selected = select_bilibili_video(home)
        self.assertEqual(selected, "https://www.bilibili.com/video/BV9VisibleFirst/")
        detail = snapshot(selected, tls=0)
        detail["dom"]["bilibiliDetail"] = True
        validate_bilibili_detail(detail, selected)
        detail["url"] = "https://www.bilibili.com/video/BV9OtherId/"
        with self.assertRaisesRegex(GateError, "selected live BV"):
            validate_bilibili_detail(detail, selected)

    def test_bilibili_playback_requires_real_progress_and_secure_source(self) -> None:
        evidence = json.loads(web_evidence()["bilibili-detail.json"])
        playback = evidence["playback"]
        validate_bilibili_playback(
            playback, "https://www.bilibili.com/video/BV1Ab411c7De/"
        )

        stalled = copy.deepcopy(playback)
        stalled["finalCurrentTime"] = stalled["initialCurrentTime"]
        stalled["progress"] = 0
        with self.assertRaisesRegex(GateError, "currentTime"):
            validate_bilibili_playback(
                stalled, "https://www.bilibili.com/video/BV1Ab411c7De/"
            )

        insecure = copy.deepcopy(playback)
        insecure["source"] = "http://media.example.invalid/video.mp4"
        insecure["sourceKind"] = "http"
        with self.assertRaisesRegex(GateError, "secure"):
            validate_bilibili_playback(
                insecure, "https://www.bilibili.com/video/BV1Ab411c7De/"
            )

        no_frames = copy.deepcopy(playback)
        no_frames["decodedFrames"] = 0
        with self.assertRaisesRegex(GateError, "decoded no video frames"):
            validate_bilibili_playback(
                no_frames, "https://www.bilibili.com/video/BV1Ab411c7De/"
            )

    def test_bilibili_playback_probe_and_wait_are_bounded(self) -> None:
        expected_url = "https://www.bilibili.com/video/BV1Ab411c7De/"

        def sample(current_time: float) -> dict[str, object]:
            return {
                "url": expected_url,
                "state": "video",
                "currentSrc": "blob:https://www.bilibili.com/fixture-stream",
                "src": "blob:https://www.bilibili.com/fixture-stream",
                "sourceKind": "blob",
                "paused": False,
                "ended": False,
                "readyState": 4,
                "networkState": 2,
                "duration": 12.0,
                "currentTime": current_time,
                "bufferedEnd": 4.0,
                "videoWidth": 1920,
                "videoHeight": 1080,
                "errorCode": None,
                "errorMessage": None,
                "playPromise": "resolved",
                "playError": None,
                "events": {
                    "loadedmetadata": 1,
                    "canplay": 1,
                    "playing": 1,
                    "timeupdate": 2,
                    "waiting": 0,
                    "stalled": 0,
                    "ended": 0,
                    "error": 0,
                },
                "lastEvent": "timeupdate",
                "decodedFrames": 20,
                "droppedFrames": 2,
            }

        client = mock.Mock()
        client.command.side_effect = [
            {"value": json.dumps(sample(0.0))},
            {"value": json.dumps(sample(0.7))},
        ]
        with mock.patch(
            "tools.riscv.debian.rootfs.browser_web_marionette_gate.time.sleep"
        ):
            observed = _wait_for_bilibili_playback(
                client, expected_url, time.monotonic() + 5
            )
        self.assertEqual(observed["status"], "pass")
        self.assertGreaterEqual(observed["progress"], 0.5)
        self.assertEqual(client.command.call_count, 2)

    def test_playback_timeout_reports_observed_video_and_probe_error(self) -> None:
        url = "https://www.bilibili.com/video/BV1Ab411c7De/"
        sample = {
            "url": url,
            "state": "video",
            "currentSrc": "blob:" + url,
            "sourceKind": "blob",
            "errorCode": None,
            "playPromise": "pending",
            "currentTime": 63.953,
            "readyState": 4,
            "networkState": 2,
            "paused": False,
            "ended": False,
            "videoWidth": 1920,
            "videoHeight": 1080,
            "decodedFrames": 1600,
            "droppedFrames": 0,
            "bufferedEnd": 65.0,
            "events": {"playing": 0, "timeupdate": 0},
        }
        module = "tools.riscv.debian.rootfs.browser_web_marionette_gate"
        for last in (sample, GateError("transport unavailable")):
            # Take exactly two samples, then expire without a wall-clock wait.
            with self.subTest(last=type(last).__name__), mock.patch(
                module + ".time.monotonic", side_effect=[0, 0, 0, 0, 1, 1, 2]
            ), mock.patch(module + ".time.sleep"), mock.patch(
                module + "._playback_probe", side_effect=[sample, last]
            ):
                with self.assertRaises(GateError) as caught:
                    _wait_for_bilibili_playback(mock.Mock(), url, 2)
                message = str(caught.exception)
                self.assertNotIn("video element remained unavailable", message)
                self.assertIn("state=video", message)
                self.assertIn("play_promise=pending", message)
                self.assertIn("current_time=63.953", message)
                if isinstance(last, GateError):
                    self.assertIn("transport unavailable", message)

    def test_challenge_403_snapshot_always_fails(self) -> None:
        challenged = snapshot("https://www.baidu.com/")
        challenged["dom"]["baiduKeyword"] = True
        challenged["dom"]["baiduSubmit"] = True
        challenged["title"] = "403 Forbidden"
        challenged["bodyText"] = "安全验证 challenge access denied"
        with self.assertRaisesRegex(GateError, "403"):
            validate_baidu_home(challenged)

        captcha = snapshot("https://wappass.baidu.com/static/captcha/tuxing_v2.html")
        with self.assertRaisesRegex(GateError, "challenge host"):
            validate_baidu_search(captcha)

        valid_challenge = snapshot(
            "https://wappass.baidu.com/static/captcha/tuxing_v2.html?"
            "backurl=https%3A%2F%2Fwww.baidu.com%2Fs%3Fwd%3DAsterinas"
        )
        validate_baidu_challenge(valid_challenge)
        self.assertEqual(
            validate_baidu_search_outcome(valid_challenge), "external-captcha"
        )
        forged_challenge = copy.deepcopy(valid_challenge)
        forged_challenge["url"] = forged_challenge["url"].replace("Asterinas", "forged")
        with self.assertRaisesRegex(GateError, "back URL"):
            validate_baidu_search_outcome(forged_challenge)

    def test_gate_explicitly_disables_insecure_certs_and_records_evidence(self) -> None:
        gate = (ROOTFS / "browser_web_marionette_gate.py").read_text()
        self.assertIn('"acceptInsecureCerts": False', gate)
        self.assertIn('capabilities.get("acceptInsecureCerts") is not False', gate)
        self.assertIn("WebDriver:TakeScreenshot", gate)
        self.assertIn("DEBIAN_BROWSER_WEB_PLATFORM_READY", gate)
        self.assertIn("arguments[0] && arguments[0].lightweight", gate)
        self.assertIn("_snapshot(client, lightweight=True)", gate)
        self.assertLess(
            gate.index('run_phase("navigate-bilibili-home"'),
            gate.index('run_phase("submit-baidu-search"'),
        )
        self.assertIn("ResourceTiming", gate)
        self.assertIn("NavigationTiming", gate)
        self.assertNotIn("innerText", gate)
        self.assertNotIn("getBoundingClientRect", gate)
        for forbidden in (
            "mock",
            "snapshot.html",
            "host proxy",
            'acceptInsecureCerts": True',
        ):
            self.assertNotIn(forbidden, gate)


if __name__ == "__main__":
    unittest.main()
