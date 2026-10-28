#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from tools.riscv.tests.support.browser_web import *


class BrowserWebContractTests(_BrowserWebFixtures, unittest.TestCase):
    def test_basic_only_boot_skips_formal_browser_evidence(self) -> None:
        evidence = ROOTFS / "browser_web_evidence.sh"
        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "console"
            environment = {
                "PATH": os.environ["PATH"],
                "ASTERINAS_BROWSER_WEB_CONSOLE": str(console),
                "ASTERINAS_BROWSER_WEB_BASIC_ONLY": "1",
            }
            skipped = subprocess.run(
                ["bash", str(evidence)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(skipped.returncode, 0, skipped.stderr)
            self.assertIn(
                "DEBIAN_BROWSER_WEB_SKIP reason=basic-only",
                console.read_text(),
            )

            environment["ASTERINAS_BROWSER_WEB_BASIC_ONLY"] = "0"
            normal = subprocess.run(
                ["bash", str(evidence)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(normal.returncode, 0)
            self.assertIn(
                "DEBIAN_BROWSER_WEB_FAIL reason=network-mode-invalid",
                console.read_text(),
            )

    def test_desktop_shell_capture_adds_one_private_qmp_socket(self) -> None:
        monitor = Path("/tmp/private-qemu/monitor.sock")
        with mock.patch.object(
            BrowserWebQemuOperations, "_qemu_argv", return_value=("qemu",)
        ):
            argv = shell_gate.DesktopShellQemuOperations._qemu_argv(
                monitor_socket=monitor
            )
        self.assertEqual(
            argv,
            (
                "qemu",
                "-qmp",
                "unix:/tmp/private-qemu/desktop-shell-qmp.sock,server=on,wait=off",
            ),
        )

    @staticmethod
    def _framebuffer_metadata(
        *, width: int = 2, height: int = 2, stride: int = 12
    ) -> tuple[bytes, bytes]:
        variable = bytearray(160)
        fixed = bytearray(80)
        struct.pack_into("=7I", variable, 0, width, height, width, height, 0, 0, 32)
        struct.pack_into("=3I", variable, 32, 16, 8, 0)
        struct.pack_into("=3I", variable, 44, 8, 8, 0)
        struct.pack_into("=3I", variable, 56, 0, 8, 0)
        struct.pack_into("=3I", variable, 68, 24, 8, 0)
        struct.pack_into("=I", fixed, 24, stride * height)
        struct.pack_into("=I", fixed, 36, 2)
        struct.pack_into("=I", fixed, 48, stride)
        return bytes(variable), bytes(fixed)

    def test_bgr_reserved_framebuffer_encodes_a_bounded_rgb_png(self) -> None:
        # Two visible BGRX pixels followed by four padding bytes per row.
        raw = b"\x10\x20\x30\0\x40\x50\x60\0pad!\x70\x80\x90\0\xa0\xb0\xc0\0pad!"

        payload = web_gate._encode_bgr_reserved_framebuffer_png(
            raw, width=2, height=2, stride=12
        )

        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", payload[16:24]), (2, 2))
        offset = 8
        compressed = bytearray()
        while offset < len(payload):
            length = struct.unpack(">I", payload[offset : offset + 4])[0]
            kind = payload[offset + 4 : offset + 8]
            contents = payload[offset + 8 : offset + 8 + length]
            if kind == b"IDAT":
                compressed.extend(contents)
            offset += 12 + length
        self.assertEqual(
            zlib.decompress(compressed),
            b"\0\x30\x20\x10\x60\x50\x40\0\x90\x80\x70\xc0\xb0\xa0",
        )

    def test_framebuffer_png_rejects_a_uniform_or_short_scanout(self) -> None:
        with self.assertRaisesRegex(GateError, "framebuffer image is blank"):
            web_gate._encode_bgr_reserved_framebuffer_png(
                b"\x20\x20\x20\0" * 4, width=2, height=2, stride=8
            )
        with self.assertRaisesRegex(GateError, "framebuffer read is incomplete"):
            web_gate._encode_bgr_reserved_framebuffer_png(
                b"\0" * 31, width=2, height=4, stride=8
            )

    def test_framebuffer_layout_accepts_only_current_true_color_abi(self) -> None:
        variable, fixed = self._framebuffer_metadata()
        layout = web_gate._parse_framebuffer_layout(variable, fixed)
        self.assertEqual((layout.width, layout.height), (2, 2))
        self.assertEqual(layout.stride, 12)
        self.assertEqual(layout.buffer_bytes, 24)

        unsupported = bytearray(variable)
        struct.pack_into("=I", unsupported, 24, 24)
        with self.assertRaisesRegex(GateError, "framebuffer layout is unsupported"):
            web_gate._parse_framebuffer_layout(bytes(unsupported), fixed)

    def test_framebuffer_capture_uses_fbdev_metadata_and_private_output(self) -> None:
        raw = b"\x10\x20\x30\0\x40\x50\x60\0pad!\x70\x80\x90\0\xa0\xb0\xc0\0pad!"
        variable, fixed = self._framebuffer_metadata()

        def ioctl(
            _descriptor: int, request: int, output: bytearray, mutate: bool
        ) -> int:
            self.assertTrue(mutate)
            output[:] = variable if request == web_gate.FBIOGET_VSCREENINFO else fixed
            return 0

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "fb0"
            output = root / "capture.png"
            device.write_bytes(raw)
            with (
                mock.patch.object(web_gate, "FRAMEBUFFER_DEVICE", device),
                mock.patch.object(web_gate.stat, "S_ISCHR", return_value=True),
                mock.patch.object(web_gate.fcntl, "ioctl", side_effect=ioctl) as call,
            ):
                dimensions = web_gate._capture_framebuffer_png(output)

            self.assertEqual(dimensions, (2, 2))
            self.assertEqual(call.call_count, 2)
            self.assertTrue(output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_framebuffer_home_capture_runs_after_marionette_transport_closes(
        self,
    ) -> None:
        client = mock.Mock()
        home = snapshot("https://www.baidu.com/")
        home["title"] = "百度一下，你就知道"
        events: list[str] = []
        client.close.side_effect = lambda: events.append("close")

        def capture(_path: Path) -> tuple[int, int]:
            self.assertEqual(events, ["close"])
            events.append("framebuffer")
            return (1920, 1080)

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(web_gate, "_connect", return_value=client),
            mock.patch.object(web_gate, "_start_webdriver_session"),
            mock.patch.object(web_gate, "_capture_baidu_home", return_value=home),
            mock.patch.object(
                web_gate, "_capture_framebuffer_png", side_effect=capture
            ) as capture_framebuffer,
        ):
            evidence = Path(directory)
            result = run_baidu_home_gate(
                "127.0.0.1",
                2828,
                30,
                evidence,
                firefox_pid=116,
                screenshot_backend="framebuffer",
            )

        self.assertEqual(result, home)
        self.assertEqual(events, ["close", "framebuffer"])
        capture_framebuffer.assert_called_once_with(evidence / "baidu-home.png")

    def test_framebuffer_cli_marker_binds_source_dimensions_and_hash(self) -> None:
        home = snapshot("https://www.baidu.com/")
        home["title"] = "百度一下，你就知道"
        screenshot = png(1920, 1080)

        def run_home(
            _host: str,
            _port: int,
            _timeout: float,
            evidence: Path,
            _firefox_pid: int,
            *,
            screenshot_backend: str,
        ) -> dict[str, object]:
            self.assertEqual(screenshot_backend, "framebuffer")
            (evidence / "baidu-home.png").write_bytes(screenshot)
            return home

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(web_gate, "validate_network_namespace"),
            mock.patch.object(web_gate, "run_baidu_home_gate", side_effect=run_home),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            status = marionette_gate_main(
                [
                    "--scope",
                    "baidu-home",
                    "--screenshot-backend",
                    "framebuffer",
                    "--firefox-pid",
                    "116",
                    "--timeout",
                    "30",
                    "--evidence-dir",
                    directory,
                ]
            )

        self.assertEqual(status, 0)
        marker = json.loads(stdout.getvalue())
        self.assertEqual(marker["screenshot_source"], "framebuffer")
        self.assertEqual(marker["screenshot_width"], 1920)
        self.assertEqual(marker["screenshot_height"], 1080)
        self.assertEqual(
            marker["screenshot_sha256"], hashlib.sha256(screenshot).hexdigest()
        )

    def test_full_scope_rejects_the_framebuffer_screenshot_backend(self) -> None:
        with (
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            self.assertRaises(SystemExit),
        ):
            marionette_gate_main(
                [
                    "--scope",
                    "full",
                    "--screenshot-backend",
                    "framebuffer",
                    "--firefox-pid",
                    "116",
                ]
            )
        self.assertIn(
            "framebuffer screenshot backend requires baidu-home scope",
            stderr.getvalue(),
        )

    def test_screenshot_command_has_an_independent_deadline(self) -> None:
        client = mock.Mock()
        client.command.return_value = {"value": base64.b64encode(png()).decode("ascii")}
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "tools.riscv.debian.rootfs.browser_web_marionette_gate.time.monotonic",
                side_effect=(100.0, 101.0),
            ),
        ):
            _write_evidence(
                client,
                Path(directory),
                "baidu-home",
                snapshot("https://www.baidu.com/"),
                deadline=300.0,
            )

        self.assertEqual(MAX_SCREENSHOT_COMMAND_SECONDS, 90.0)
        self.assertEqual(
            client.set_timeout.call_args_list,
            [
                mock.call(MAX_SCREENSHOT_COMMAND_SECONDS),
                mock.call(199.0),
            ],
        )
        client.command.assert_called_once_with(
            "WebDriver:TakeScreenshot", {"full": False}
        )

    _BACKGROUND_STARTUP_PREFERENCES = {
        'user_pref("browser.newtabpage.enabled", false);',
        'user_pref("browser.pagethumbnails.capturing_disabled", true);',
        'user_pref("browser.region.network.url", "");',
        'user_pref("browser.topsites.contile.enabled", false);',
        'user_pref("dom.ipc.processCount", 1);',
        'user_pref("dom.ipc.processPrelaunch.enabled", false);',
        'user_pref("fission.autostart", false);',
        'user_pref("media.rdd-process.enabled", true);',
        'user_pref("network.captive-portal-service.enabled", false);',
        'user_pref("network.connectivity-service.enabled", false);',
    }
    _DOWNLOAD_PREFERENCES = {
        'user_pref("browser.download.folderList", 2);',
        'user_pref("browser.download.dir", "/home/asterinas/Downloads");',
        'user_pref("browser.download.useDownloadDir", true);',
        'user_pref("browser.helperApps.neverAsk.saveToDisk", "application/octet-stream");',
    }

    def _prepare_firefox_profile(
        self,
        home: Path,
        *,
        mode: str,
        proxy_host: str = "",
        proxy_port: str = "",
        basic_only: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "HOME": str(home),
            "ASTERINAS_WEB_NETWORK_MODE": mode,
            "ASTERINAS_BROWSER_WEB_BASIC_ONLY": "1" if basic_only else "0",
            "ASTERINAS_DESKTOP_PROXY_HOST": proxy_host,
            "ASTERINAS_DESKTOP_PROXY_PORT": proxy_port,
        }
        return subprocess.run(
            ["/bin/bash", str(ROOTFS / "browser_web_firefox.sh"), "--prepare-profile"],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_firefox_basic_profile_disables_public_background_fetchers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            result = self._prepare_firefox_profile(home, mode="direct", basic_only=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            profile = (home / ".mozilla/asterinas-browser-web/user.js").read_text(
                encoding="utf-8"
            )
            for preference in (
                'user_pref("app.update.enabled", false);',
                'user_pref("browser.safebrowsing.downloads.remote.enabled", false);',
                'user_pref("extensions.update.enabled", false);',
                'user_pref("network.trr.mode", 5);',
                'user_pref("services.settings.server", "");',
            ):
                self.assertIn(preference, profile)

    def test_firefox_proxy_profile_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            result = self._prepare_firefox_profile(
                home,
                mode="proxy",
                proxy_host="10.100.19.216",
                proxy_port="17893",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            profile = (home / ".mozilla/asterinas-browser-web/user.js").read_text(
                encoding="utf-8"
            )
            proxy_preferences = {
                'user_pref("network.proxy.type", 1);',
                'user_pref("network.proxy.http", "10.100.19.216");',
                'user_pref("network.proxy.http_port", 17893);',
                'user_pref("network.proxy.ssl", "10.100.19.216");',
                'user_pref("network.proxy.ssl_port", 17893);',
                'user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1, 10.100.19.216");',
            }
            self.assertEqual(
                set(profile.splitlines()),
                self._BACKGROUND_STARTUP_PREFERENCES
                | self._DOWNLOAD_PREFERENCES
                | proxy_preferences,
            )
            self.assertEqual(
                oct(
                    (home / ".mozilla/asterinas-browser-web/user.js").stat().st_mode
                    & 0o777
                ),
                "0o600",
            )
            self.assertEqual(
                list((home / ".mozilla/asterinas-browser-web").glob("user.js.tmp.*")),
                [],
            )
            launcher = (ROOTFS / "browser_web_firefox.sh").read_text()
            self.assertIn(
                'export ASTERINAS_FIREFOX_WEB_NETWORK_MODE="$NETWORK_MODE"',
                launcher,
            )
            self.assertIn(
                "unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY",
                launcher,
            )

    def test_firefox_infers_proxy_mode_from_the_complete_pinned_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            result = self._prepare_firefox_profile(
                home,
                mode="",
                proxy_host="10.100.19.216",
                proxy_port="17893",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            profile = (home / ".mozilla/asterinas-browser-web/user.js").read_text(
                encoding="utf-8"
            )
            self.assertIn('user_pref("network.proxy.type", 1);', profile)
            self.assertIn(
                'user_pref("network.proxy.http", "10.100.19.216");', profile
            )
            self.assertIn('user_pref("network.proxy.http_port", 17893);', profile)

    def test_invalid_browser_network_profile_does_not_restart_forever(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._prepare_firefox_profile(Path(temporary), mode="")
        self.assertEqual(result.returncode, 64)
        self.assertIn("ASTERINAS_FIREFOX_WEB_FAIL reason=invalid-network-profile", result.stderr)
        service = (ROOTFS / "browser_web.service").read_text()
        self.assertIn("RestartPreventExitStatus=64", service)

    def test_firefox_direct_profile_removes_proxy_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            proxy = self._prepare_firefox_profile(
                home,
                mode="proxy",
                proxy_host="10.100.19.216",
                proxy_port="17893",
            )
            self.assertEqual(proxy.returncode, 0, proxy.stderr)

            direct = self._prepare_firefox_profile(home, mode="direct")

            self.assertEqual(direct.returncode, 0, direct.stderr)
            profile = (home / ".mozilla/asterinas-browser-web/user.js").read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                set(profile.splitlines()),
                self._BACKGROUND_STARTUP_PREFERENCES
                | self._DOWNLOAD_PREFERENCES
                | {'user_pref("network.proxy.type", 0);'},
            )
            self.assertNotIn("network.proxy.http", profile)
            self.assertNotIn("network.proxy.ssl", profile)
            self.assertNotIn("network.proxy.no_proxies_on", profile)

            for mode, host, port in (
                ("", "", ""),
                ("invalid", "", ""),
                ("proxy", "", "17893"),
                ("proxy", "10.100.19.216", "abc"),
                ("proxy", "10.100.19.216", "65536"),
            ):
                with self.subTest(mode=mode, host=host, port=port):
                    invalid = self._prepare_firefox_profile(
                        home,
                        mode=mode,
                        proxy_host=host,
                        proxy_port=port,
                    )
                    self.assertNotEqual(invalid.returncode, 0)
                    self.assertEqual(
                        (home / ".mozilla/asterinas-browser-web/user.js").read_text(
                            encoding="utf-8"
                        ),
                        profile,
                    )

    def test_baidu_home_requires_logo(self) -> None:
        home = snapshot("https://www.baidu.com/")
        home["dom"]["baiduKeyword"] = True
        home["dom"]["baiduSubmit"] = True
        home["dom"]["baiduLogo"] = True
        validate_baidu_home(home)

        home["dom"]["baiduLogo"] = False
        with self.assertRaisesRegex(GateError, "logo"):
            validate_baidu_home(home)

    def test_baidu_search_contains_fixed_query(self) -> None:
        search = snapshot("https://www.baidu.com/s?wd=Asterinas", tls=0)
        search["dom"]["baiduResults"] = 1
        search["title"] = "Asterinas_百度搜索"
        validate_baidu_search(search)

        wrong_path = copy.deepcopy(search)
        wrong_path["url"] = "https://www.baidu.com/other?wd=Asterinas"
        with self.assertRaisesRegex(GateError, "search URL"):
            validate_baidu_search(wrong_path)

        missing_query_content = copy.deepcopy(search)
        missing_query_content["title"] = "百度搜索"
        missing_query_content["bodyText"] = (
            "这是足够长的百度搜索结果正文，但其中刻意不包含固定的英文查询关键词。"
        )
        with self.assertRaisesRegex(GateError, "query content"):
            validate_baidu_search(missing_query_content)

    def test_mode_qualified_firefox_ready_marker(self) -> None:
        direct = ("\n".join(BROWSER_WEB_MILESTONES) + "\n").encode()
        self.assertIn(firefox_ready_marker(NetworkMode.DIRECT), BROWSER_WEB_MILESTONES)
        self.assertTrue(
            classify_browser_web_qemu(
                direct,
                expected_debian_release="13.6",
                network_mode=NetworkMode.DIRECT,
            ).passed
        )
        proxy = ("\n".join(browser_web_milestones(NetworkMode.PROXY)) + "\n").encode()
        self.assertTrue(
            classify_browser_web_qemu(
                proxy,
                expected_debian_release="13.6",
                network_mode=NetworkMode.PROXY,
            ).passed
        )
        self.assertFalse(
            classify_browser_web_qemu(
                proxy.replace(b"DEBIAN_WEB_NETWORK_READY mode=proxy layers=10\n", b""),
                expected_debian_release="13.6",
                network_mode=NetworkMode.PROXY,
            ).passed
        )
        self.assertFalse(
            classify_browser_web_qemu(
                proxy,
                expected_debian_release="13.6",
                network_mode=NetworkMode.DIRECT,
            ).passed
        )
        self.assertFalse(
            classify_browser_web_qemu(
                proxy + firefox_ready_marker(NetworkMode.DIRECT).encode(),
                expected_debian_release="13.6",
                network_mode=NetworkMode.PROXY,
            ).passed
        )

    def test_fixture_capabilities_are_validated_on_explicit_page(self) -> None:
        url = "http://10.0.2.2:17894/browser-quality/index.html?capabilities=1"
        source = snapshot(url)
        source["title"] = "Asterinas Browser Quality"
        source["browserCapabilities"] = fixture_capabilities("home")
        probe = {
            name: source[name]
            for name in (
                "url",
                "title",
                "readyState",
                "bodyText",
                "jsComplete",
                "browserCapabilities",
                "dom",
            )
        }
        probe_fixture_capabilities(probe, url)

    def test_readiness_probe_accepts_optional_capability_types(self) -> None:
        source = snapshot("https://www.baidu.com/")
        source["dom"]["baiduKeyword"] = True
        source["dom"]["baiduSubmit"] = True
        source["dom"]["baiduLogo"] = True
        probe = {
            name: source[name]
            for name in (
                "url",
                "title",
                "readyState",
                "bodyText",
                "jsComplete",
                "browserCapabilities",
                "dom",
            )
        }
        probe["apiTypes"] = {
            "wasm": "undefined",
            "worker": "function",
            "indexedDb": "object",
            "audio": "function",
            "fetch": "function",
        }
        self.assertEqual(_probe_mapping(probe)["apiTypes"], probe["apiTypes"])
        probe_baidu_home(probe)

    def _schema7_payload(self) -> dict[str, object]:
        profile = get_profile("browser-web")
        zero = "0" * 64
        return {
            "schema_version": 7,
            "profile": "browser-web",
            "suite": "trixie",
            "debian_release": "13.6",
            "architecture": "riscv64",
            "signed_sources": [
                {
                    "role": source.role,
                    "mirror_url": source.mirror_url,
                    "suite": source.suite,
                    "inrelease_url": source.inrelease_url,
                    "inrelease_sha256": zero,
                }
                for source in SIGNED_SOURCES
            ],
            "packages_lock_sha256": zero,
            "downloaded_packages": [
                {
                    "name": "firefox-esr",
                    "architecture": "riscv64",
                    "version": "140.14.0esr-1~deb13u1",
                    "sha256": zero,
                    "source_role": "security",
                }
            ],
            "filesystem": {
                "type": "ext2",
                "label": profile.root_label,
                "uuid": profile.root_uuid,
                "size_bytes": profile.root_size_bytes,
                "block_size_bytes": 4096,
            },
            "tool_versions": {
                "browser-web-runtime": "a" * 64,
                "debootstrap": "test",
            },
            "build_timestamp": "2026-08-28T00:00:00Z",
            "root_image_sha256": zero,
            "gate_packages": {name: "1" for name in profile.identity_packages},
        }

    def test_profile_uses_firefox_private_nss_and_embedded_xul_trust(self) -> None:
        profile = get_profile("browser-web")
        self.assertEqual(profile.schema_version, 7)
        self.assertEqual(profile.root_label, "ASTER_BROWSERWEB")
        # The single daily image keeps a 3 GiB root; the milestone images
        # retain their 2 GiB size.
        self.assertEqual(profile.root_size_bytes, 3 * 1024 * 1024 * 1024)
        for package in (
            "firefox-esr",
            "python3-minimal",
            "ca-certificates",
            "curl",
            "iproute2",
            "iputils-ping",
            "libavcodec-extra61",
            "xdotool",
            "mousepad",
            "ristretto",
            "atril",
            "xarchiver",
            "zip",
            "unzip",
            "dbus-x11",
        ):
            self.assertIn(package, profile.requested_packages)
            self.assertIn(package, profile.identity_packages)
        for package in ("libnss3", "libnss3-tools", "p11-kit", "p11-kit-modules"):
            self.assertNotIn(package, profile.requested_packages)
            self.assertNotIn(package, profile.identity_packages)
        self.assertEqual(
            len(profile.requested_packages), len(set(profile.requested_packages))
        )
        self.assertEqual(
            len(profile.identity_packages), len(set(profile.identity_packages))
        )
        printed = subprocess.run(
            [
                str(ROOTFS / "build_rootfs.sh"),
                "--profile",
                "browser-web",
                "--print-packages",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertEqual(printed, list(profile.requested_packages))

    def test_browser_web_runtime_digest_changes_with_every_gate_input(self) -> None:
        inputs = (
            "desktop_session.sh",
            "desktop_wallpaper.svg",
            "desktop_anime_wallpaper.png",
            "desktop_files_icon.svg",
            "desktop_terminal_icon.svg",
            "desktop_pcmanfm.conf",
            "desktop_online_lxpanel.conf",
            "asterinas_firefox.desktop",
            "asterinas-files.desktop",
            "asterinas-terminal.desktop",
            "browser_web_open_firefox.sh",
            "desktop_network_evidence.sh",
            "desktop_network_gate.py",
            "browser_web_firefox.sh",
            "browser_web_marionette_gate.py",
            "megrez_clock_sync.py",
            "browser_marionette_transport.py",
            "browser_web_evidence.sh",
            "browser_web.service",
            "browser_web_evidence.service",
            "browser_web_network.conf",
            "physical_graphics_interaction.html",
            "physical_graphics_gate.py",
            "physical_external_services_quiesce.sh",
            "browser_interaction_perf.py",
            "desktop_display_provider.sh",
            "desktop_fbdev_no_input.conf",
            "browser_performance_provenance.py",
            "firefox_diagnostic_snapshot.py",
            "browser_web_trust_check.py",
            "browser_web_online_rootfs_check.py",
            "firefox_jit_overlay.py",
            "physical_graphics_control.sh",
            "desktop_m3_device_access.sh",
        )

        def digest(source_directory: Path) -> str:
            result = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    'source "$1"; browser_web_runtime_digest "$2"',
                    "browser-web-runtime-test",
                    str(ROOTFS / "build_rootfs.sh"),
                    str(source_directory),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            value = result.stdout.strip()
            self.assertRegex(value, r"\A[0-9a-f]{64}\Z")
            return value

        with tempfile.TemporaryDirectory() as directory:
            source_directory = Path(directory)
            for name in inputs:
                (source_directory / name).write_bytes((ROOTFS / name).read_bytes())
            baseline = digest(source_directory)
            for name in inputs:
                with self.subTest(name=name):
                    path = source_directory / name
                    original = path.read_bytes()
                    path.write_bytes(original + b"\n# runtime-contract-test\n")
                    self.assertNotEqual(digest(source_directory), baseline)
                    path.write_bytes(original)

        builder = (ROOTFS / "build_rootfs.sh").read_text()
        self.assertIn(
            '--tool-version "browser-web-runtime=$browser_web_runtime_version"',
            builder,
        )

    def test_browser_web_builder_wires_explicit_frozen_jit_packages(self) -> None:
        builder = (ROOTFS / "build_rootfs.sh").read_text()
        self.assertIn("--firefox-jit-package-dir", builder)
        self.assertIn('python3 "$script_directory/firefox_jit_overlay.py"', builder)
        self.assertIn('--package-dir "$FIREFOX_JIT_PACKAGE_DIR"', builder)
        self.assertIn(
            '--tool-version "firefox-jit-overlay=$firefox_jit_overlay_version"',
            builder,
        )

    def test_firefox_jit_overlay_cli_accepts_package_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            packages = Path(directory) / "packages"
            root.mkdir()
            packages.mkdir()
            for package in OVERLAY_PACKAGES:
                (packages / package["filename"]).write_bytes(b"forged")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOTFS / "firefox_jit_overlay.py"),
                    "--root",
                    str(root),
                    "--package-dir",
                    str(packages),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("hash mismatch", result.stderr)

    def test_firefox_jit_overlay_cli_preserves_positional_packages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            paths = []
            for package in OVERLAY_PACKAGES:
                path = Path(directory) / package["filename"]
                path.write_bytes(b"forged")
                paths.append(path)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOTFS / "firefox_jit_overlay.py"),
                    "--root",
                    str(root),
                    *(str(path) for path in paths),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("hash mismatch", result.stderr)

    def test_firefox_jit_builder_rejects_non_browser_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_directory = root / "packages"
            package_directory.mkdir()
            result = subprocess.run(
                [
                    str(ROOTFS / "build_rootfs.sh"),
                    "--profile",
                    "minimal",
                    "--output-dir",
                    str(root / "output"),
                    "--cache-dir",
                    str(root / "cache"),
                    "--firefox-jit-package-dir",
                    str(package_directory),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "Firefox JIT overlay is only valid for the browser-web profile",
                result.stderr,
            )

    def test_schema_seven_validates_optional_jit_overlay_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            payload = self._schema7_payload()
            payload["tool_versions"]["firefox-jit-overlay"] = "b" * 64
            path.write_text(json.dumps(payload))
            self.assertEqual(load_manifest(path).profile, "browser-web")

            payload["tool_versions"]["firefox-jit-overlay"] = "not-a-digest"
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(
                ContractError, "tool_versions.firefox-jit-overlay"
            ):
                load_manifest(path)

    def test_physical_graphics_witness_is_installed_fail_closed(self) -> None:
        builder = (ROOTFS / "build_rootfs.sh").read_text()
        self.assertIn('"$script_directory/physical_graphics_interaction.html"', builder)
        self.assertIn(
            '"$stage/usr/share/asterinas/physical-graphics/index.html"', builder
        )
        self.assertIn('"$script_directory/physical_graphics_gate.py"', builder)
        self.assertIn('"$stage/usr/lib/asterinas/physical-graphics-gate"', builder)
        self.assertIn('"$script_directory/browser_interaction_perf.py"', builder)
        self.assertIn('"$stage/usr/lib/asterinas/browser_interaction_perf.py"', builder)
        self.assertIn(
            '"$script_directory/physical_external_services_quiesce.sh"', builder
        )
        self.assertIn(
            '"$stage/usr/lib/asterinas/physical-external-services-quiesce"',
            builder,
        )
        self.assertNotIn("physical-graphics-evidence", builder)
        runtime_inputs = builder[
            builder.index("browser_web_runtime_digest()") : builder.index(
                "publish_artifacts()"
            )
        ]
        self.assertIn("physical_graphics_interaction.html", runtime_inputs)
        self.assertIn("physical_graphics_gate.py", runtime_inputs)
        self.assertIn("browser_interaction_perf.py", runtime_inputs)
        self.assertIn("physical_external_services_quiesce.sh", runtime_inputs)

    def test_display_provider_resolver_defaults_to_the_installed_fbdev_config(
        self,
    ) -> None:
        resolver = ROOTFS / "desktop_display_provider.sh"
        self.assertTrue(resolver.is_file())
        with tempfile.TemporaryDirectory() as directory:
            provider_root = Path(directory)
            config_directory = provider_root / "fbdev/xorg.conf.d"
            config_directory.mkdir(parents=True)
            (config_directory / "20-asterinas.conf").write_text('Section "Device"\n')
            environment = {
                **os.environ,
                "ASTERINAS_DISPLAY_PROVIDER_ROOT": str(provider_root),
            }

            result = subprocess.run(
                ["bash", str(resolver)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{config_directory}\n")
        self.assertEqual(result.stderr, "")

    def test_rootfs_installs_and_versions_the_display_provider_resolver(self) -> None:
        builder = (ROOTFS / "build_rootfs.sh").read_text()

        self.assertIn('"$script_directory/desktop_display_provider.sh"', builder)
        self.assertIn('"$stage/usr/lib/asterinas/desktop-display-provider"', builder)
        self.assertIn(
            "Environment=ASTERINAS_DISPLAY_PROVIDER=$display_provider", builder
        )
        self.assertIn('local display_provider="${4:-fbdev}"', builder)
        runtime_inputs = builder[
            builder.index("browser_web_runtime_digest()") : builder.index(
                "publish_artifacts()"
            )
        ]
        self.assertIn("desktop_display_provider.sh", runtime_inputs)

    def test_runtime_provenance_is_installed_collected_and_host_bound(self) -> None:
        builder = (ROOTFS / "build_rootfs.sh").read_text()
        evidence = (ROOTFS / "browser_web_evidence.sh").read_text()
        qemu_gate = (ROOTFS / "browser_web_qemu_gate.py").read_text()

        self.assertIn('"$script_directory/browser_performance_provenance.py"', builder)
        self.assertIn(
            '"$stage/usr/lib/asterinas/browser-performance-provenance"', builder
        )
        self.assertIn("browser_performance_provenance.py", builder)
        self.assertIn("/usr/lib/asterinas/browser-performance-provenance", evidence)
        self.assertIn('--output "$RUNTIME_PROVENANCE"', evidence)
        self.assertIn("RUNTIME_PROVENANCE_ERROR", evidence)
        self.assertIn(
            "DEBIAN_BROWSER_WEB_DIAGNOSTIC component=runtime-provenance stderr_hex=",
            evidence,
        )
        self.assertIn('"runtime-provenance.json"', qemu_gate)
        self.assertIn("bind_runtime_provenance", qemu_gate)
        self.assertIn('"browser-performance-provenance.json"', qemu_gate)

    def test_display_provider_resolver_rejects_missing_and_unknown_providers(
        self,
    ) -> None:
        resolver = ROOTFS / "desktop_display_provider.sh"
        self.assertTrue(resolver.is_file())
        with tempfile.TemporaryDirectory() as directory:
            for provider, expected_status in (("drm", 65), ("unknown", 64)):
                with self.subTest(provider=provider):
                    result = subprocess.run(
                        ["bash", str(resolver)],
                        env={
                            **os.environ,
                            "ASTERINAS_DISPLAY_PROVIDER": provider,
                            "ASTERINAS_DISPLAY_PROVIDER_ROOT": directory,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, expected_status)
                    self.assertEqual(result.stdout, "")

    def test_display_provider_resolver_accepts_only_fbdev_legacy_config(self) -> None:
        resolver = ROOTFS / "desktop_display_provider.sh"
        with tempfile.TemporaryDirectory() as directory:
            provider_root = Path(directory) / "providers"
            legacy_directory = Path(directory) / "legacy"
            legacy_directory.mkdir()
            (legacy_directory / "20-asterinas.conf").write_text('Section "Device"\n')
            common = {
                **os.environ,
                "ASTERINAS_DISPLAY_PROVIDER_ROOT": str(provider_root),
                "ASTERINAS_LEGACY_XORG_CONFIG_DIR": str(legacy_directory),
            }

            fbdev = subprocess.run(
                ["bash", str(resolver)],
                env={**common, "ASTERINAS_DISPLAY_PROVIDER": "fbdev"},
                capture_output=True,
                text=True,
                check=False,
            )
            drm = subprocess.run(
                ["bash", str(resolver)],
                env={**common, "ASTERINAS_DISPLAY_PROVIDER": "drm"},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(fbdev.returncode, 0, fbdev.stderr)
        self.assertEqual(fbdev.stdout, f"{legacy_directory}\n")
        self.assertEqual(drm.returncode, 65)
        self.assertEqual(drm.stdout, "")

    def test_daily_browser_skips_full_network_evidence(self) -> None:
        service = (ROOTFS / "browser_web_evidence.service").read_text()
        self.assertIn(
            "Wants=network-online.target asterinas-desktop.service", service
        )
        self.assertIn(
            "After=network-online.target asterinas-desktop.service", service
        )
        self.assertNotIn("Requires=network-online.target", service)
        self.assertNotIn("Requires=asterinas-desktop.service", service)
        self.assertNotIn("Environment=ASTERINAS_WEB_NETWORK_MODE=", service)
        self.assertNotIn("Environment=ASTERINAS_DESKTOP_PROXY", service)
        self.assertIn(
            "Environment=PYTHONPYCACHEPREFIX=/run/asterinas-browser-web-pycache",
            service,
        )
        self.assertIn("Environment=PYTHONDONTWRITEBYTECODE=1", service)
        browser_service = (ROOTFS / "browser_web.service").read_text()
        self.assertNotIn("asterinas-desktop-network.service", browser_service)
        self.assertIn(
            "After=asterinas-browser-web-timeline-basic.service", browser_service
        )
        self.assertIn("Requires=asterinas-desktop-network.service", service)
        self.assertIn(
            "ConditionKernelCommandLine=!systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1",
            service,
        )
        network_drop_in = (ROOTFS / "browser_web_network.conf").read_text()
        self.assertIn(
            "ConditionKernelCommandLine=!systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1",
            network_drop_in,
        )
        self.assertIn("TimeoutStartSec=45s", network_drop_in)
        builder = (ROOTFS / "build_rootfs.sh").read_text()
        self.assertIn(
            '"$stage/etc/systemd/system/'
            'asterinas-desktop-network.service.d/browser-web.conf"',
            builder,
        )
        self.assertIn('"$script_directory/browser_web_network.conf"', builder)
        runtime_inputs = builder[
            builder.index("browser_web_runtime_digest()") : builder.index(
                "publish_artifacts()"
            )
        ]
        self.assertIn("browser_web_network.conf", runtime_inputs)

    def test_gate_versions_accept_architecture_all_identity_packages(self) -> None:
        profile = get_profile("browser-web")
        rows = tuple(
            (name, "all" if name == "ca-certificates" else "riscv64", "1")
            for name in profile.identity_packages
        )
        versions = _gate_versions(rows, profile)
        self.assertEqual(versions["ca-certificates"], "1")

    def test_schema_seven_manifest_load_mismatch_and_source_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            payload = self._schema7_payload()
            path.write_text(json.dumps(payload))
            manifest = load_manifest(path)
            self.assertEqual(
                (manifest.schema_version, manifest.profile), (7, "browser-web")
            )
            self.assertEqual(manifest.downloaded_packages[0][4], "security")
            for mutation in (
                "schema6-browser-web",
                "schema7-desktop-network",
                "missing-source-role",
                "missing-runtime-digest",
                "malformed-runtime-digest",
            ):
                forged = copy.deepcopy(payload)
                if mutation == "schema6-browser-web":
                    forged["schema_version"] = 6
                elif mutation == "schema7-desktop-network":
                    forged["profile"] = "desktop-network"
                elif mutation == "missing-runtime-digest":
                    forged["tool_versions"].pop("browser-web-runtime")
                elif mutation == "malformed-runtime-digest":
                    forged["tool_versions"]["browser-web-runtime"] = "not-a-digest"
                else:
                    forged["downloaded_packages"][0].pop("source_role")
                path.write_text(json.dumps(forged))
                with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                    load_manifest(path)

    def test_schema_seven_loads_previous_browser_web_package_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            payload = self._schema7_payload()
            previous_identity = (
                "mousepad",
                "ristretto",
                "atril",
                "xarchiver",
                "zip",
                "unzip",
                "dbus-x11",
            )
            for package in previous_identity:
                payload["gate_packages"].pop(package)
            path.write_text(json.dumps(payload))
            manifest = load_manifest(path)
            self.assertEqual(
                set(dict(manifest.gate_packages)), set(payload["gate_packages"])
            )

            payload["gate_packages"]["atril"] = "1"
            path.write_text(json.dumps(payload))
            with self.assertRaises(ContractError):
                load_manifest(path)

    @mock.patch(
        "tools.riscv.debian.rootfs.contract._write_validated_manifest_atomically"
    )

    def test_schema_seven_writer_emits_profile_and_source_roles(
        self, publish: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = get_profile("browser-web")
            names = sorted(set(profile.requested_packages + profile.identity_packages))
            rows = [(name, "riscv64", "1") for name in names]
            image = root / "image"
            image.write_bytes(b"image")
            package_lock = root / "packages.lock"
            package_lock.write_text("".join(f"{n}\t{a}\t{v}\n" for n, a, v in rows))
            checksums = root / "checksums"
            checksums.write_text(
                "".join(
                    f"{n}\t{a}\t{v}\t{hashlib.sha256(n.encode()).hexdigest()}\t"
                    f"{'security' if n == 'firefox-esr' else 'base'}\n"
                    for n, a, v in rows
                )
            )
            inrelease = root / "legacy-InRelease"
            inrelease.write_bytes(b"legacy")
            sources = {
                role: root / f"{role}-InRelease" for role in ("base", "security")
            }
            for role, path in sources.items():
                path.write_bytes(role.encode())
            write_manifest(
                output=root / "manifest.json",
                image=image,
                packages_lock=package_lock,
                inrelease=inrelease,
                package_checksums=checksums,
                mirror_url=SIGNED_SOURCES[0].mirror_url,
                suite="trixie",
                debian_release="13.6",
                build_timestamp="2026-08-28T00:00:00Z",
                tool_versions=("debootstrap=test", "browser-web-runtime=" + "a" * 64),
                profile_name="browser-web",
                signed_source_files=sources,
            )
        payload = json.loads(publish.call_args.args[1])
        self.assertEqual(
            (payload["schema_version"], payload["profile"]), (7, "browser-web")
        )
        firefox = next(
            row
            for row in payload["downloaded_packages"]
            if row["name"] == "firefox-esr"
        )
        self.assertEqual(firefox["source_role"], "security")

    def test_unit_launcher_and_evidence_audit_actual_sandbox_and_tls(self) -> None:
        unit = (ROOTFS / "browser_web.service").read_text()
        launcher = (ROOTFS / "browser_web_firefox.sh").read_text()
        evidence = (ROOTFS / "browser_web_evidence.sh").read_text()
        self.assertIn("User=asterinas", unit)
        self.assertIn("AmbientCapabilities=\n", unit)
        self.assertIn("CapabilityBoundingSet=\n", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertNotIn("PrivateNetwork", unit)
        self.assertNotIn("CAP_SYS_ADMIN", unit)
        self.assertNotIn("--offline", launcher)
        self.assertNotIn("--no-sandbox", launcher)
        self.assertNotIn("acceptInsecureCerts", launcher)
        # The desktop service is the sole Xorg owner.  A browser-side fallback
        # would race it for :0 and produce a misleading cannot-open-display
        # failure when the provider is still coming up.
        self.assertNotIn("/usr/bin/Xorg :0", launcher)
        self.assertIn("about:blank", launcher)
        self.assertIn("ASTERINAS_FIREFOX_WEB_TARGET_URL", launcher)
        self.assertIn("A_WEB_TIMELINE_UNAVAILABLE", launcher)
        self.assertIn(
            "Environment=ASTERINAS_FIREFOX_WEB_TARGET_URL=https://www.baidu.com/", unit
        )
        for required in (
            'readonly NETWORK_RESOLVER="${ASTERINAS_WEB_NETWORK_RESOLVER:-}"',
            'grep -Fqx "nameserver $NETWORK_RESOLVER"',
            "getent ahostsv4",
            "--proto '=https'",
            "--tlsv1.2",
            "%{ssl_verify_result}",
            "Seccomp:[[:space:]]*",
            "NoNewPrivs:[[:space:]]+1",
            "embedded-xul|system-nss-jit-overlay",
        ):
            self.assertIn(required, evidence)
        self.assertIn("/usr/bin/timeout 30 getent ahostsv4", evidence)
        self.assertIn("/usr/bin/timeout 135 curl", evidence)
        self.assertNotIn("curl -k", evidence)
        self.assertNotIn("--insecure", evidence)
        self.assertIn("DEBIAN_BROWSER_WEB_GATE_DIAGNOSTIC", evidence)
        self.assertIn("ASTERINAS_BROWSER_WEB_PROC_DIAGNOSTIC", evidence)
        self.assertIn('emit "$line"', evidence)
        self.assertIn('/usr/bin/tee -a "$GATE_STDERR" >>"$CONSOLE"', evidence)
        self.assertIn(
            "DEBIAN_BROWSER_WEB_EXTERNAL_BLOCK site=baidu reason=captcha", evidence
        )
        self.assertIn("unavailable-firefox-riscv64-build", evidence)
        self.assertIn('emit "DEBIAN_BROWSER_WEB_FAIL reason=browser-content"', evidence)
        self.assertLess(
            evidence.index('emit "DEBIAN_BROWSER_WEB_FAIL reason=browser-content"'),
            evidence.index("/usr/bin/timeout 20 /usr/bin/sync || true"),
        )
        self.assertIn(
            "/usr/bin/timeout 20 /usr/bin/sync || fail evidence-sync", evidence
        )
        self.assertNotIn("sync /home/asterinas/browser-web-evidence", evidence)
        self.assertLess(
            evidence.rindex("/usr/bin/timeout 20 /usr/bin/sync"),
            evidence.index('emit "DEBIAN_FIREFOX_BAIDU_READY'),
        )
        builder = (ROOTFS / "build_rootfs.sh").read_text()
        self.assertIn(
            'install -d -m 0700 -- "$stage/home/asterinas/browser-web-evidence"',
            builder,
        )
        for name in (
            "baidu-home.json",
            "baidu-home.png",
            "baidu-search.json",
            "baidu-search.png",
            "fixture-search.json",
            "fixture-search.png",
            "fixture-download.json",
            "bilibili-home.json",
            "bilibili-home.png",
            "bilibili-detail.json",
            "bilibili-detail.png",
        ):
            self.assertIn(name, builder)
        self.assertIn("systemctl_bounded", evidence)
        self.assertIn("/usr/bin/timeout 5 /usr/bin/systemctl", evidence)
        # systemd-manager queries are deliberately excluded from the critical
        # path before the content gate.  They remain bounded final assertions.
        self.assertEqual(evidence.count("--property NRestarts"), 1)
        self.assertIn("--property MainPID", evidence)
        self.assertIn("systemctl_bounded is-active --quiet", evidence)
        self.assertNotIn("firefox-restarted-before-gate", evidence)
        self.assertIn("firefox-pid-changed-during-gate", evidence)
        self.assertIn("/usr/bin/timeout 2 /usr/bin/cat", evidence)
        self.assertIn("/usr/bin/timeout 2 /usr/bin/sleep 1", evidence)

    def test_evidence_marks_baidu_captcha_with_trailing_capability_fields(self) -> None:
        evidence = (ROOTFS / "browser_web_evidence.sh").read_text()
        lines = evidence.splitlines()
        index = next(
            index
            for index, line in enumerate(lines)
            if "if [[" in line and "baidu_outcome=external-captcha" in line
        )
        branch = "\n".join(lines[index : index + 3])
        script = (
            "content=$1\n"
            "emit() { printf '%s\\n' \"$1\"; }\n"
            f"{branch}\n"
        )
        prefix = (
            "DEBIAN_BROWSER_WEB_CONTENT fixture_search=pass baidu_home=pass "
            "baidu_search=observed bilibili_home=pass bilibili_detail=pass "
            "bilibili_playback=pass bv=BV1nMYb6KEhF tls=verified"
        )
        for outcome, expected in (
            ("external-captcha", "DEBIAN_BROWSER_WEB_EXTERNAL_BLOCK site=baidu reason=captcha\n"),
            ("pass", ""),
        ):
            payload = f"{prefix} baidu_outcome={outcome} capabilities=pass download=pass"
            with self.subTest(outcome=outcome):
                completed = subprocess.run(
                    ["bash", "-c", script, "-", payload],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.stdout, expected)

    def test_browser_web_keeps_formal_markers_without_debug_console_flood(self) -> None:
        builder = (ROOTFS / "build_rootfs.sh").read_text()
        online_evidence = (ROOTFS / "browser_web_evidence.sh").read_text()
        self.assertIn("desktop_standard_output=journal", builder)
        self.assertIn("desktop_standard_error=journal", builder)
        self.assertIn("StandardOutput=$desktop_standard_output", builder)
        self.assertIn("StandardError=$desktop_standard_error", builder)
        self.assertNotIn("systemd.log_level=debug", DESKTOP_NETWORK_QEMU_BOOTARGS)
        self.assertIn("DEBIAN_FIREFOX_BAIDU_READY", online_evidence)
        self.assertIn(
            'readonly CONSOLE="${ASTERINAS_BROWSER_WEB_CONSOLE:-/dev/console}"',
            online_evidence,
        )

    def test_physical_timeline_reset_discards_prior_boot_markers(self) -> None:
        source = (ROOTFS / "browser_web_timeline.sh").read_text()
        with tempfile.TemporaryDirectory() as directory:
            timeline = Path(directory) / "timeline.log"
            timeline.write_text("old Firefox exec and ready markers\n")
            script = Path(directory) / "timeline.sh"
            script.write_text(
                source.replace(
                    "readonly TIMELINE=/home/asterinas/browser-web-timeline.log",
                    f"readonly TIMELINE={timeline}",
                )
            )
            result = subprocess.run(
                ["bash", str(script), "reset-physical"],
                env={**os.environ, "ASTERINAS_BROWSER_WEB_CONSOLE": "/dev/null"},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(
                timeline.read_text(),
                r"\AA_WEB_TIMELINE marker=BOOT_PHYSICAL_BROWSER_BEGIN "
                r"guest_monotonic_ns=[1-9][0-9]* firefox_pid=0\n\Z",
            )

    def test_browser_timeline_writes_are_owner_safe_and_best_effort(self) -> None:
        timeline_script = (ROOTFS / "browser_web_timeline.sh").read_text()
        launcher = (ROOTFS / "browser_web_firefox.sh").read_text()
        for script in (timeline_script, launcher):
            self.assertIn("if ! printf", script)
            self.assertIn('>>"$TIMELINE" 2>/dev/null; then', script)
            self.assertIn("A_WEB_TIMELINE_UNAVAILABLE", script)
        self.assertIn("/usr/bin/timeout 5 /usr/bin/install", timeline_script)
        self.assertIn("-o 1000 -g 1000", timeline_script)

    def test_startup_timeline_requires_ordered_guest_monotonic_phases(self) -> None:
        evidence = web_evidence()
        validate_web_evidence(evidence)
        self.assertEqual(evidence["timeline.log"].count(b"A_WEB_TIMELINE marker="), 15)
        unit = (ROOTFS / "browser_web.service").read_text()
        builder = (ROOTFS / "build_rootfs.sh").read_text()
        self.assertNotIn(
            "ExecStartPre=/usr/lib/asterinas/browser-web-timeline wait-x", unit
        )
        self.assertIn("ExecStartPre=+/usr/lib/asterinas/desktop-network-device-access", unit)
        self.assertIn(
            "/usr/lib/asterinas/browser-web-timeline wait-x",
            (ROOTFS / "browser_web_firefox.sh").read_text(),
        )
        timeline_script = (ROOTFS / "browser_web_timeline.sh").read_text()
        self.assertIn("/usr/bin/xdpyinfo -display", timeline_script)
        self.assertIn("/usr/bin/timeout 5 /usr/bin/xdpyinfo", timeline_script)
        self.assertIn("</proc/uptime", timeline_script)
        self.assertNotIn("EPOCHREALTIME", timeline_script)
        self.assertNotIn("x-probe-fallback", timeline_script)
        self.assertNotIn("reason=socket-metadata-unavailable", timeline_script)
        self.assertIn("stale X11 socket", timeline_script)
        self.assertIn("browser_web_timeline_begin.service", builder)
        self.assertIn("browser_web_timeline_basic.service", builder)
        self.assertIn(
            "basic.target.wants/asterinas-browser-web-timeline-basic.service", builder
        )
        self.assertIn('desktop_after="local-fs.target dbus.service"', builder)
        self.assertIn("desktop_session_options=$'TTYPath=/dev/tty1", builder)
        self.assertIn("StandardInput=tty", builder)
        self.assertIn("Environment=ASTERINAS_BROWSER_WEB_SESSION=1", builder)
        self.assertIn("SupplementaryGroups=video input tty", builder)
        self.assertIn("desktop_user=root", builder)
        desktop_session = (ROOTFS / "desktop_session.sh").read_text()
        self.assertIn("desktop-display-provider", desktop_session)
        self.assertIn('-configdir "$provider_config_directory"', desktop_session)
        self.assertIn("display-provider-ready", desktop_session)
        self.assertNotIn("-extension MIT-SHM", desktop_session)
        self.assertIn('-logfile "$HOME/Xorg.0.log" vt1', desktop_session)
        self.assertIn("-novtswitch -keeptty", desktop_session)
        self.assertIn(
            "/usr/sbin/runuser --user asterinas --preserve-environment",
            desktop_session,
        )
        self.assertNotIn("/usr/bin/runuser", desktop_session)
        self.assertIn('/usr/bin/tail -n 0 -f "$HOME/Xorg.0.log" >&2', desktop_session)
        self.assertIn("/usr/bin/rm -f -- /tmp/.X11-unix/X0", desktop_session)
        self.assertIn(
            '/usr/bin/timeout 5 /usr/bin/xdpyinfo -display "$DISPLAY"', desktop_session
        )
        self.assertIn(
            "firefox-web-stderr.log", (ROOTFS / "browser_web_firefox.sh").read_text()
        )
        launcher = (ROOTFS / "browser_web_firefox.sh").read_text()
        self.assertIn("ASTERINAS_FIREFOX_PS_DIAGNOSTIC", launcher)
        self.assertIn("/usr/bin/timeout 5 /usr/bin/ps", launcher)
        self.assertIn("/usr/bin/timeout 12 /usr/bin/sleep 10", launcher)
        self.assertIn("ASTERINAS_FIREFOX_PREWARM", launcher)
        self.assertIn("FIREFOX_LIBRARY_DIR=/usr/lib/firefox-esr", launcher)
        self.assertIn("FIREFOX_LIBRARY_DIR=/usr/lib/firefox", launcher)
        self.assertIn('exec "$FIREFOX_BIN"', launcher)
        self.assertIn('"$FIREFOX_HOME/Downloads"', launcher)
        self.assertIn("browser.download.useDownloadDir", launcher)
        self.assertIn("browser.helperApps.neverAsk.saveToDisk", launcher)
        self.assertIn("</proc/uptime", launcher)
        self.assertNotIn("EPOCHREALTIME", launcher)
        self.assertIn("browser-web-firefox.pid", builder)
        self.assertIn('asterinas-browser-web"', builder)
        self.assertIn(
            "BROWSER_WEB_DESKTOP_STAGE=device-access-start",
            (ROOTFS / "desktop_m3_device_access.sh").read_text(),
        )
        device_access = (ROOTFS / "desktop_m3_device_access.sh").read_text()
        self.assertIn("device_deadline=$((SECONDS + 120))", device_access)
        self.assertIn("/usr/bin/sleep 1", device_access)
        self.assertIn('readonly XKB_CACHE_DIR="/var/lib/xkb"', device_access)
        self.assertIn('/usr/bin/mountpoint -q "$XKB_CACHE_DIR"', device_access)
        self.assertIn(
            "/usr/bin/mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs",
            device_access,
        )
        self.assertIn("/proc/self/mounts", device_access)
        self.assertIn("$2 == path { print $3, $4; matches++ }", device_access)
        self.assertIn('[[ "$xkb_fstype" != tmpfs ]]', device_access)
        for option in ("rw", "nosuid", "nodev"):
            self.assertIn(f'",$xkb_options," != *",{option},"*', device_access)
        self.assertIn("xkb_cache_created=0", device_access)
        self.assertIn("xkb_cache_created=1", device_access)
        self.assertIn('/usr/bin/chown root:root "$XKB_CACHE_DIR"', device_access)
        self.assertIn('/usr/bin/chmod 01777 "$XKB_CACHE_DIR"', device_access)
        self.assertIn('/usr/bin/stat -c "%u %g %a" "$XKB_CACHE_DIR"', device_access)
        self.assertIn('[[ "$xkb_owner_mode" != "0 0 1777" ]]', device_access)
        self.assertLess(
            device_access.index("/proc/self/mounts"),
            device_access.index('/usr/bin/chown root:root "$XKB_CACHE_DIR"'),
        )
        self.assertIn("BROWSER_WEB_DESKTOP_STAGE=xkb-cache-ready", device_access)
        self.assertIn(
            'readonly DISPLAY_PROVIDER="${ASTERINAS_DISPLAY_PROVIDER:-fbdev}"',
            device_access,
        )
        self.assertIn('while [[ ! -c "$DISPLAY_DEVICE" ]]', device_access)
        self.assertIn("printf '%s' /dev/fb0", device_access)
        self.assertIn("input-devices-absent", device_access)
        self.assertIn(
            "device-access-failed reason=${DISPLAY_DEVICE##*/}-timeout",
            device_access,
        )
        self.assertIn(
            "device-access-failed reason=${DISPLAY_DEVICE##*/}-permissions",
            device_access,
        )
        self.assertIn(
            "BROWSER_WEB_DESKTOP_STAGE=${DISPLAY_DEVICE##*/}-ready", device_access
        )
        self.assertIn('"$stage/etc/systemd/system/systemd-udevd.service"', builder)
        self.assertIn('"$stage/etc/systemd/system/systemd-logind.service"', builder)
        self.assertIn(
            'configure_desktop_network "$stage" browser lightweight', builder
        )
        self.assertNotIn("2> >(tee", (ROOTFS / "browser_web_evidence.sh").read_text())
        begin_unit = (ROOTFS / "browser_web_timeline_begin.service").read_text()
        basic_unit = (ROOTFS / "browser_web_timeline_basic.service").read_text()
        self.assertIn("User=asterinas", begin_unit)
        self.assertIn("User=asterinas", basic_unit)
        self.assertIn("After=systemd-remount-fs.service", begin_unit)
        for boundary in (
            "systemd-sysusers.service",
            "ldconfig.service",
            "systemd-journal-catalog-update.service",
            "sysinit.target",
        ):
            self.assertIn(boundary, begin_unit)
        self.assertIn("DefaultDependencies=no", basic_unit)
        self.assertIn("After=sysinit.target", basic_unit)
        self.assertIn("Before=basic.target", basic_unit)
        self.assertIn("WantedBy=basic.target", basic_unit)
        for timeline in (
            evidence["timeline.log"].replace(
                b"page=bilibili-detail", b"page=missing-detail"
            ),
            evidence["timeline.log"].replace(
                b"guest_monotonic_ns=1400", b"guest_monotonic_ns=1"
            ),
            evidence["timeline.log"].replace(b"firefox_pid=100", b"firefox_pid=101", 1),
            evidence["timeline.log"].replace(b"firefox_pid=100", b"firefox_pid=999"),
            evidence["timeline.log"].replace(
                b"guest_monotonic_ns=1400", b"guest_monotonic_ns=720000000000000"
            ),
        ):
            with self.assertRaises(GateFailure):
                validate_web_evidence({**evidence, "timeline.log": timeline})

        legacy = evidence["timeline.log"]
        for old, new in zip(
            range(100, 800, 100),
            range(1_700_000_000_000_100, 1_700_000_000_000_800, 100),
            strict=True,
        ):
            legacy = legacy.replace(
                f"guest_monotonic_ns={old} ".encode(),
                f"guest_monotonic_ns={new} ".encode(),
                1,
            )
        index = validate_web_evidence({**evidence, "timeline.log": legacy})
        self.assertEqual(
            index["timeline.log"]["clock_outcome"],
            "legacy-split-realtime-monotonic",
        )
        with self.assertRaisesRegex(GateFailure, "invalid record"):
            validate_web_evidence(
                {
                    **evidence,
                    "timeline.log": evidence["timeline.log"] + b"unstructured\n",
                }
            )

    def test_build_time_cache_checker_is_fail_closed(self) -> None:
        builder = (ROOTFS / "build_rootfs.sh").read_text()
        for command in (
            'chroot "$stage" /usr/bin/systemd-sysusers',
            'chroot "$stage" /sbin/ldconfig',
            'qemu-riscv64-static -L "$stage" "$stage/usr/bin/systemd-hwdb"',
            '--root="$stage" update --usr',
            'chroot "$stage" /usr/bin/journalctl --update-catalog',
            'chroot "$stage" /usr/bin/fc-cache -f',
            'run_chroot "$stage" /usr/bin/update-mime-database /usr/share/mime',
            ': >"$stage/etc/.updated"',
            ': >"$stage/var/.updated"',
        ):
            self.assertIn(command, builder)
        cache_function = builder.split("finalize_browser_startup_caches()", 1)[1].split(
            "configure_desktop_network()", 1
        )[0]
        self.assertNotIn("systemd-sysusers --root", cache_function)
        self.assertNotIn("journalctl --root", cache_function)
        self.assertNotIn("|| true", cache_function)
        self.assertNotIn("systemctl mask", cache_function)
        self.assertNotIn("/dev/null", cache_function)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (
                "etc/systemd/system",
                "usr/share/asterinas",
                "usr/share/mime",
                "usr/lib/udev",
                "usr/lib/systemd/system",
                "var/lib/systemd/catalog",
                "var/cache/fontconfig",
            ):
                (root / relative).mkdir(parents=True, exist_ok=True)
            passwd = root / "etc/passwd"
            passwd.write_text(
                "".join(
                    f"{name}:x:{uid}:{gid}:{name}:/:/usr/sbin/nologin\n"
                    for name, (uid, gid) in cache_check.EXPECTED_USERS.items()
                )
            )
            groups = root / "etc/group"
            groups.write_text(
                "".join(
                    f"{name}:x:{gid}:\n"
                    for name, gid in cache_check.EXPECTED_GROUPS.items()
                )
            )
            (root / "etc/shadow").write_text(
                "root:!:0:0:99999:7:::\nasterinas:!:0:0:99999:7:::\n"
            )
            cache = root / "etc/ld.so.cache"
            cache.write_bytes(b"glibc-ld.so.cache1.1fixture")
            listing = root / "usr/share/asterinas/browser-startup-ldconfig.log"
            listing.write_text(
                f"LD_SO_CACHE_SHA256 {hashlib.sha256(cache.read_bytes()).hexdigest()}\n"
                "libc.so.6 (libc6,double-float) => "
                "/lib/riscv64-linux-gnu/libc.so.6\n"
            )
            (root / "usr/lib/udev/hwdb.bin").write_bytes(b"KSLPHHRH" + b"\0" * 24)
            (root / "var/lib/systemd/catalog/database").write_bytes(
                b"RHHHKSLP" + b"\0" * 24
            )
            (root / "usr/share/mime/mime.cache").write_bytes(
                b"\0\x01\0\x02" + b"\0" * 32
            )
            (root / "usr/share/mime/globs2").write_text(
                "50:application/x-desktop:*.desktop\n50:image/svg+xml:*.svg\n"
            )
            font_directory = root / "usr/share/fonts/fixture"
            font_directory.mkdir(parents=True)
            os.utime(font_directory, ns=(1704067200000000000,) * 2)
            cache_dir_name = b"/usr/share/fonts/fixture\0"
            font_cache_bytes = (
                struct.pack(
                    "<II7q",
                    0xFC02FC04,
                    9,
                    64 + len(cache_dir_name),
                    64,
                    0,
                    0,
                    0,
                    1704067200,
                    0,
                )
                + cache_dir_name
            )
            (root / "var/cache/fontconfig/fixture.cache-9").write_bytes(
                font_cache_bytes
            )
            unit = root / "etc/systemd/system/asterinas-browser-web.service"
            unit.write_text(
                "[Service]\nUser=asterinas\nAmbientCapabilities=\n"
                "CapabilityBoundingSet=\nNoNewPrivileges=yes\n"
            )
            for (
                maintenance_unit,
                required_lines,
            ) in cache_check.MAINTENANCE_UNITS.items():
                (root / "usr/lib/systemd/system" / maintenance_unit).write_text(
                    "[Unit]\n" + "\n".join(required_lines) + "\n"
                )
            for marker in (root / "etc/.updated", root / "var/.updated"):
                marker.touch()
            os.utime(root / "usr", ns=(100, 100))
            os.utime(root / "etc/ld.so.cache", ns=(100, 100))
            with mock.patch.object(cache_check, "EXPECTED_OWNER_UID", os.getuid()):
                self.assertIn("ldconfig=riscv64", cache_check.check_cache_profile(root))
                mime_cache = root / "usr/share/mime/mime.cache"
                mime_cache.chmod(0o600)
                with self.assertRaisesRegex(cache_check.CacheCheckError, "readable"):
                    cache_check.check_cache_profile(root)
                mime_cache.chmod(0o644)
                mime_cache.unlink()
                with self.assertRaisesRegex(cache_check.CacheCheckError, "mime.cache"):
                    cache_check.check_cache_profile(root)
                mime_cache.write_bytes(b"\0\x01\0\x02" + b"\0" * 32)
                mime_cache.write_bytes(b"invalid-cache")
                with self.assertRaisesRegex(cache_check.CacheCheckError, "MIME"):
                    cache_check.check_cache_profile(root)
                mime_cache.write_bytes(b"\0\x01\0\x02" + b"\0" * 32)
                mime_globs = root / "usr/share/mime/globs2"
                mime_globs.write_text("50:image/svg+xml:*.svg\n")
                with self.assertRaisesRegex(cache_check.CacheCheckError, "desktop"):
                    cache_check.check_cache_profile(root)
                mime_globs.write_text(
                    "50:application/x-desktop:*.desktop\n50:image/svg+xml:*.svg\n"
                )
                font_cache = root / "var/cache/fontconfig/fixture.cache-9"
                font_cache.chmod(0o600)
                with self.assertRaisesRegex(cache_check.CacheCheckError, "fontconfig"):
                    cache_check.check_cache_profile(root)
                font_cache.chmod(0o644)
                os.utime(font_directory, ns=(1704067200000000001,) * 2)
                with self.assertRaisesRegex(cache_check.CacheCheckError, "fontconfig"):
                    cache_check.check_cache_profile(root)
                os.utime(font_directory, ns=(1704067200000000000,) * 2)
                font_cache.write_bytes(b"\x04\xfc\x02\xfcPENDING")
                with self.assertRaisesRegex(cache_check.CacheCheckError, "fontconfig"):
                    cache_check.check_cache_profile(root)
                font_cache.write_bytes(font_cache_bytes)

                original = passwd.read_text()
                for mutation in (
                    original.replace("asterinas:x:1000:1000", "asterinas:x:1001:1000"),
                    original + "duplicate:x:1000:1001::/:/bin/false\n",
                    original.replace("messagebus:x:997:997", "messagebus:x:996:997"),
                    "\n".join(
                        line
                        for line in original.splitlines()
                        if not line.startswith("systemd-network:")
                    )
                    + "\n",
                    original + "uid-alias:x:998:998:alias:/:/usr/sbin/nologin\n",
                ):
                    passwd.write_text(mutation)
                    with self.assertRaises(cache_check.CacheCheckError):
                        cache_check.check_cache_profile(root)
                passwd.write_text(original)

                original_groups = groups.read_text()
                for mutation in (
                    original_groups.replace("render:x:992:\n", ""),
                    original_groups + "render:x:991:\n",
                    original_groups.replace("kvm:x:993:", "kvm:x:991:"),
                    original_groups.replace("asterinas:x:1000:", "asterinas:x:1001:"),
                    original_groups + "gid-alias:x:999:\n",
                ):
                    groups.write_text(mutation)
                    with self.assertRaises(cache_check.CacheCheckError):
                        cache_check.check_cache_profile(root)
                groups.write_text(original_groups)

                original_cache = cache.read_bytes()
                cache.write_bytes(b"")
                with self.assertRaises(cache_check.CacheCheckError):
                    cache_check.check_cache_profile(root)
                cache.write_bytes(original_cache)
                cache.unlink()
                cache.symlink_to("/dev/null")
                with self.assertRaises(cache_check.CacheCheckError):
                    cache_check.check_cache_profile(root)
                cache.unlink()
                cache.write_bytes(original_cache)
                os.utime(cache, ns=(100, 100))
                cache.write_bytes(b"host-cache-format")
                with self.assertRaisesRegex(
                    cache_check.CacheCheckError, "unknown format"
                ):
                    cache_check.check_cache_profile(root)
                cache.write_bytes(original_cache)

                original_listing = listing.read_text()
                cache.write_bytes(original_cache)
                listing.write_text(
                    original_listing.replace("riscv64-linux", "x86_64-linux")
                )
                with self.assertRaisesRegex(cache_check.CacheCheckError, "host paths"):
                    cache_check.check_cache_profile(root)
                listing.write_text(
                    original_listing.replace(
                        "/lib/riscv64-linux-gnu/libc.so.6", "/usr/lib/x86_64/libhost.so"
                    )
                )
                with self.assertRaisesRegex(cache_check.CacheCheckError, "host paths"):
                    cache_check.check_cache_profile(root)
                listing.write_text(original_listing)

                cache.write_bytes(b"glibc-ld.so.cache1.1other-fixture")
                with self.assertRaisesRegex(cache_check.CacheCheckError, "hash-bound"):
                    cache_check.check_cache_profile(root)
                cache.write_bytes(original_cache)

                os.utime(cache, ns=(100, 100))
                os.utime(root / "usr", ns=(200, 200))
                with self.assertRaisesRegex(
                    cache_check.CacheCheckError, "cache is older"
                ):
                    cache_check.check_cache_profile(root)
                os.utime(root / "usr", ns=(100, 100))

                font = root / "var/cache/fontconfig/fixture.cache-9"
                font.unlink()
                (root / "var/cache/fontconfig/CACHEDIR.TAG").write_text("tag")
                with self.assertRaisesRegex(cache_check.CacheCheckError, "fontconfig"):
                    cache_check.check_cache_profile(root)
                font.write_bytes(font_cache_bytes)
                font.write_bytes(b"arbitrary-font-bytes")
                with self.assertRaisesRegex(cache_check.CacheCheckError, "fontconfig"):
                    cache_check.check_cache_profile(root)
                font.write_bytes(font_cache_bytes)

                catalog = root / "var/lib/systemd/catalog/database"
                catalog.write_bytes(b"")
                with self.assertRaisesRegex(
                    cache_check.CacheCheckError, "empty cache input"
                ):
                    cache_check.check_cache_profile(root)
                catalog.write_bytes(b"arbitrary-catalog")
                with self.assertRaisesRegex(
                    cache_check.CacheCheckError, "unknown format"
                ):
                    cache_check.check_cache_profile(root)
                catalog.write_bytes(b"RHHHKSLP" + b"\0" * 24)

                hwdb = root / "usr/lib/udev/hwdb.bin"
                original_hwdb = hwdb.read_bytes()
                hwdb.write_bytes(b"arbitrary-hwdb")
                with self.assertRaisesRegex(
                    cache_check.CacheCheckError, "unknown format"
                ):
                    cache_check.check_cache_profile(root)
                hwdb.write_bytes(original_hwdb)
                local_hwdb = root / "etc/udev/hwdb.bin"
                local_hwdb.parent.mkdir(parents=True, exist_ok=True)
                local_hwdb.write_bytes(b"KSLPHHRH" + b"\0" * 24)
                with self.assertRaisesRegex(cache_check.CacheCheckError, "suppressed"):
                    cache_check.check_cache_profile(root)
                local_hwdb.unlink()

                stamp = root / "etc/.updated"
                os.utime(root / "usr", ns=(200, 200))
                os.utime(cache, ns=(200, 200))
                os.utime(stamp, ns=(100, 100))
                with self.assertRaisesRegex(
                    cache_check.CacheCheckError, "older than /usr"
                ):
                    cache_check.check_cache_profile(root)
                os.utime(stamp, ns=(200, 200))
                other_stamp = root / "var/.updated"
                other_stamp.unlink()
                with self.assertRaisesRegex(
                    cache_check.CacheCheckError, "missing or unsafe"
                ):
                    cache_check.check_cache_profile(root)

                other_stamp.touch()
                os.utime(other_stamp, ns=(200, 200))
                maintenance = "systemd-sysusers.service"
                override = root / "etc/systemd/system" / maintenance
                override.symlink_to("/dev/null")
                with self.assertRaisesRegex(
                    cache_check.CacheCheckError, "masked or overridden"
                ):
                    cache_check.check_cache_profile(root)
                override.unlink()
                vendor = root / "usr/lib/systemd/system" / maintenance
                vendor_contents = vendor.read_text()
                vendor.unlink()
                with self.assertRaisesRegex(
                    cache_check.CacheCheckError, "missing or unsafe"
                ):
                    cache_check.check_cache_profile(root)
                vendor.write_text(vendor_contents)
                dropin = root / "etc/systemd/system" / f"{maintenance}.d"
                dropin.mkdir()
                (dropin / "bypass.conf").write_text(
                    "[Service]\nExecStart=\nExecStart=/bin/true\n"
                )
                with self.assertRaisesRegex(
                    cache_check.CacheCheckError, "masked or overridden"
                ):
                    cache_check.check_cache_profile(root)
                (dropin / "bypass.conf").unlink()
                dropin.rmdir()

            with mock.patch.object(cache_check, "EXPECTED_OWNER_UID", -1):
                with self.assertRaisesRegex(
                    cache_check.CacheCheckError, "non-root-owned"
                ):
                    cache_check.check_cache_profile(root)

    def test_fontconfig_cache_uses_audited_scan_and_fails_closed(self) -> None:
        script = r"""
source "$1"
stage="$2/stage"
mkdir -p "$stage/var/cache/fontconfig"
mkdir -p "$stage/usr/share/fonts/fixture" "$stage/usr/local/share/fonts"
touch -d @1800000000 "$stage/usr/share/fonts/fixture" "$stage/usr/local/share/fonts"
attempt_file="$2/attempts"
scenario="$3"
printf '0\n' >"$attempt_file"
export SOURCE_DATE_EPOCH=1704067200
umask 077
if [[ "$scenario" == failed ]]; then
    printf 'old cache\n' >"$stage/var/cache/fontconfig/old.cache-9"
fi
chroot() {
    current="$(cat "$attempt_file")"
    current="$((current + 1))"
    printf '%s\n' "$current" >"$attempt_file"
    [[ "$scenario" != failed ]] || return 1
    if [[ "$scenario" == success && -z "${SOURCE_DATE_EPOCH-}" && " $* " == *" -v "* ]]; then
        [[ "$(stat -c %Y "$1/usr/share/fonts/fixture")" == 1704067200 ]] || return 1
        [[ "$(stat -c %Y "$1/usr/local/share/fonts")" == 1704067200 ]] || return 1
        printf 'cache\n' >"$1/var/cache/fontconfig/retry.cache-9"
    fi
    return 0
}
generate_fontconfig_cache "$stage" "$3"
[[ "$(umask)" == 0077 ]] || exit 3
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for scenario, expected_status, expected_attempts in (
                ("success", 0, "1"),
                ("empty", 2, "1"),
                ("failed", 2, "1"),
            ):
                with self.subTest(scenario=scenario):
                    work = root / scenario
                    work.mkdir()
                    result = subprocess.run(
                        [
                            "/bin/bash",
                            "-c",
                            script,
                            "fontconfig-retry-test",
                            str(ROOTFS / "build_rootfs.sh"),
                            str(work),
                            scenario,
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )

                    self.assertEqual(result.returncode, expected_status, result.stderr)
                    self.assertEqual(
                        (work / "attempts").read_text().strip(), expected_attempts
                    )
                    if scenario == "empty":
                        self.assertIn("fontconfig cache is absent", result.stderr)
                    elif scenario == "failed":
                        self.assertIn("fontconfig cache rebuild failed", result.stderr)
                    else:
                        self.assertEqual(
                            (work / "stage/var/cache/fontconfig/retry.cache-9")
                            .stat()
                            .st_mode
                            & 0o777,
                            0o644,
                        )


if __name__ == "__main__":
    unittest.main()
