#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from tools.riscv.tests.support.debian_rootfs import *


class DebianRootfsBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_cleanup_unmounts_only_private_workspace_descendants(self) -> None:
        work_directory = self.directory / "cleanup-work"
        (work_directory / "stage/proc").mkdir(parents=True)
        fake_bin = self.directory / "cleanup-tools"
        fake_bin.mkdir()
        umount_log = self.directory / "umount.log"
        (fake_bin / "findmnt").write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' / /root/asterinas "
            '"$ASTERINAS_TEST_STAGE/proc" "$ASTERINAS_TEST_STAGE"\n',
            encoding="utf-8",
        )
        (fake_bin / "umount").write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$3" >>"$ASTERINAS_TEST_UMOUNT_LOG"\n',
            encoding="utf-8",
        )
        (fake_bin / "findmnt").chmod(0o755)
        (fake_bin / "umount").chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_TEST_STAGE=str(work_directory / "stage"),
            ASTERINAS_TEST_UMOUNT_LOG=str(umount_log),
        )

        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                'source "$1"; WORK_DIR="$2"; cleanup',
                "builder-cleanup-test",
                str(BUILD_SCRIPT),
                str(work_directory),
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            umount_log.read_text(encoding="utf-8").splitlines(),
            [str(work_directory / "stage/proc"), str(work_directory / "stage")],
        )

    def test_prints_exact_required_tool_contract(self) -> None:
        result = _run_builder("--print-tools", cwd=self.directory)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), list(REQUIRED_TOOLS))
        self.assertEqual(result.stderr, "")

    def test_prints_exact_explicit_package_contract(self) -> None:
        result = _run_builder("--print-packages", cwd=self.directory)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), list(INSTALL_PACKAGES))
        self.assertEqual(result.stderr, "")

    def test_prints_exact_systemd_ext4_package_contract(self) -> None:
        result = _run_builder(
            "--profile",
            "systemd-ext4",
            "--print-packages",
            cwd=self.directory,
        )

        profile = get_profile("systemd-ext4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), list(profile.requested_packages))
        self.assertEqual(profile.filesystem_type, "ext4")
        self.assertTrue(profile.journal)
        self.assertEqual(result.stderr, "")

    def test_rejects_unknown_and_duplicate_profiles_before_tools(self) -> None:
        environment = os.environ.copy()
        environment["PATH"] = "/missing-tools"
        cases = (
            (("--profile", "desktop"), "unknown rootfs profile"),
            (
                ("--profile", "minimal", "--profile", "minimal"),
                "duplicate argument: --profile",
            ),
        )

        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                result = _run_builder(
                    *arguments,
                    cwd=self.directory,
                    environment=environment,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("missing required tool", result.stderr)

    def test_configures_browser_web_desktop_shell(self) -> None:
        work_directory = self.directory / "configure-browser-web-desktop"
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
                'source "$1"; install_online_desktop_shell "$2"',
                "builder-configure-online-desktop-test",
                str(BUILD_SCRIPT),
                str(stage),
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        wallpaper = stage / "usr/share/asterinas/desktop-wallpaper.svg"
        anime_wallpaper = stage / "usr/share/asterinas/desktop-anime-wallpaper.png"
        pcmanfm = stage / "home/asterinas/.config/pcmanfm/Asterinas/desktop-items-0.conf"
        panel = stage / "home/asterinas/.config/lxpanel/Asterinas/panels/panel"
        self.assertEqual(wallpaper.read_bytes(), DESKTOP_WALLPAPER.read_bytes())
        self.assertEqual(anime_wallpaper.read_bytes(), DESKTOP_ANIME_WALLPAPER.read_bytes())
        self.assertIn("wallpaper_mode=crop", pcmanfm.read_text())
        self.assertIn("desktop-anime-wallpaper.png", pcmanfm.read_text())
        self.assertIn("edge=bottom", panel.read_text())
        for name in ("browser", "files", "terminal"):
            self.assertIn(
                f"id=asterinas-{name}.desktop",
                panel.read_text(),
            )
        self.assertEqual(pcmanfm.stat().st_uid, 1000)
        self.assertEqual(panel.stat().st_uid, 1000)
        for relative in (
            ".config/pcmanfm",
            ".config/pcmanfm/Asterinas",
            ".config/lxpanel",
            ".config/lxpanel/Asterinas",
            ".config/lxpanel/Asterinas/panels",
        ):
            self.assertEqual((stage / "home/asterinas" / relative).stat().st_uid, 1000)

        desktop = stage / "home/asterinas/Desktop"
        self.assertEqual(
            sorted(path.name for path in desktop.glob("*.desktop")),
            [
                "asterinas-browser.desktop",
                "asterinas-files.desktop",
                "asterinas-terminal.desktop",
            ],
        )
        browser = desktop / "asterinas-browser.desktop"
        self.assertIn("Exec=", browser.read_text())
        self.assertIn("firefox", browser.read_text().lower())
        self.assertNotIn("netsurf", browser.read_text().lower())
        self.assertEqual(stat.S_IMODE(browser.stat().st_mode), 0o755)
        self.assertEqual(browser.stat().st_uid, 1000)
        self.assertIn(
            "Exec=xterm -title Asterinas-Terminal",
            (desktop / "asterinas-terminal.desktop").read_text(),
        )
        for name in ("files", "terminal"):
            icon = stage / f"usr/share/asterinas/desktop-{name}-icon.svg"
            source = (
                REPOSITORY_ROOT
                / f"tools/riscv/debian/rootfs/desktop_{name}_icon.svg"
            )
            self.assertEqual(icon.read_bytes(), source.read_bytes())
            self.assertIn(
                f"Icon=/usr/share/asterinas/desktop-{name}-icon.svg",
                (desktop / f"asterinas-{name}.desktop").read_text(),
            )
        self.assertIn("xdotool", get_profile("browser-web").requested_packages)
        launcher = stage / "usr/lib/asterinas/browser-web-open-firefox"
        self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o755)
        self.assertIn("windowactivate", launcher.read_text())

        session = (
            REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_session.sh"
        ).read_text()
        self.assertIn("/usr/bin/pcmanfm --desktop --profile Asterinas", session)
        self.assertIn("/usr/bin/lxpanel --profile Asterinas", session)
        self.assertIn("/usr/sbin/runuser --user asterinas", session)

        m4_session = DESKTOP_M4_SESSION_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("map_client_windows", m4_session)
        self.assertIn("xdotool windowmap", m4_session)
        self.assertIn("DESKTOP_M4_WINDOW_HANDOFF status=complete", m4_session)

    def test_desktop_m4_evidence_requires_application_windows(self) -> None:
        evidence_script = DESKTOP_M4_EVIDENCE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "ASTERINAS_DESKTOP_M4_PROBE_TIMEOUT_SECONDS:-30",
            evidence_script,
        )
        self.assertIn(
            "probe logind pgrep -x systemd-logind",
            evidence_script,
        )
        for window_class in ("pcmanfm", "lxpanel", "xterm", "netsurf-gtk"):
            self.assertIn(
                f"visible_window '^{window_class}$'",
                evidence_script,
            )

    def test_desktop_m4_x11_paint_uses_translated_root_coordinates(self) -> None:
        paint_script = DESKTOP_M4_X11_PAINT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("XTranslateCoordinates", paint_script)
        self.assertIn(
            "(screen_y.value + WINDOW_HEIGHT // 2) * stride",
            paint_script,
        )
        self.assertIn(
            "(screen_x.value + center_x) * 4",
            paint_script,
        )
        self.assertNotIn(
            "(WINDOW_Y + WINDOW_HEIGHT // 2) * stride",
            paint_script,
        )
        input_directory = self.directory / "desktop-m4-input"
        input_directory.mkdir()
        (input_directory / "event0").touch()
        (input_directory / "event1").touch()
        xorg_log = self.directory / "desktop-m4-Xorg.0.log"
        xorg_log.write_text(
            """(II) FBDEV(0): using shadow framebuffer
(II) XINPUT: Adding extended input device \"Asterinas keyboard\"
(II) XINPUT: Adding extended input device \"Asterinas pointer\"
""",
            encoding="utf-8",
        )
        fake_bin = self._make_desktop_m4_evidence_tools()

        cases = {
            "": None,
            "desktop-pcmanfm": "pcmanfm",
            "lxpanel": "lxpanel",
            "openbox": "openbox",
            "hung-openbox": "openbox-probe-timeout",
            "netsurf": "netsurf",
            "xterm": "xterm",
            "mapped-netsurf": "netsurf-window",
        }
        for missing, expected_diagnostic in cases.items():
            with self.subTest(missing=missing or "none"):
                console = self.directory / f"desktop-m4-console-{missing or 'complete'}"
                console.write_text("", encoding="utf-8")
                environment = os.environ.copy()
                environment.update(
                    PATH=f"{fake_bin}:/usr/bin:/bin",
                    ASTERINAS_DESKTOP_M4_CONSOLE=str(console),
                    ASTERINAS_DESKTOP_M4_INPUT_DIRECTORY=str(input_directory),
                    ASTERINAS_DESKTOP_M4_XORG_LOG=str(xorg_log),
                    ASTERINAS_DESKTOP_M4_TIMEOUT_SECONDS="0",
                    ASTERINAS_DESKTOP_M4_PROBE_TIMEOUT_SECONDS="1",
                    ASTERINAS_DESKTOP_M4_TEST_MISSING=missing,
                )

                started_at = time.monotonic()
                result = subprocess.run(
                    ["/bin/bash", str(DESKTOP_M4_EVIDENCE_SCRIPT)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                elapsed = time.monotonic() - started_at

                evidence = console.read_text(encoding="utf-8")
                if not missing:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(evidence, "\n".join(DESKTOP_M4_MILESTONES) + "\n")
                else:
                    self.assertNotEqual(result.returncode, 0)
                    if missing == "hung-openbox":
                        self.assertLess(elapsed, 1.8)
                    self.assertIn(
                        f"DEBIAN_DESKTOP_M4_DIAGNOSTIC missing={expected_diagnostic}\n",
                        evidence,
                    )
                    if missing == "mapped-netsurf":
                        self.assertIn(
                            '0x200003 "Asterinas Start - NetSurf": '
                            '("netsurf" "NetSurf")',
                            evidence,
                        )
                    self.assertTrue(
                        evidence.endswith(
                            "DEBIAN_DESKTOP_M4_FAIL reason=desktop-timeout\n"
                        ),
                        evidence,
                    )

    def test_desktop_m4_evidence_prepares_wallpaper_overview(self) -> None:
        input_directory = self.directory / "desktop-m4-overview-input"
        input_directory.mkdir()
        (input_directory / "event0").touch()
        (input_directory / "event1").touch()
        xorg_log = self.directory / "desktop-m4-overview-Xorg.0.log"
        xorg_log.write_text(
            """(II) FBDEV(0): using shadow framebuffer
(II) XINPUT: Adding extended input device \"Asterinas keyboard\"
(II) XINPUT: Adding extended input device \"Asterinas pointer\"
""",
            encoding="utf-8",
        )
        fake_bin = self._make_desktop_m4_evidence_tools()

        for missing in ("", "overview-xdotool"):
            with self.subTest(missing=missing or "none"):
                console = self.directory / f"desktop-m4-overview-{missing or 'ok'}"
                xdotool_log = self.directory / f"xdotool-{missing or 'ok'}.log"
                console.write_text("", encoding="utf-8")
                environment = os.environ.copy()
                environment.update(
                    PATH=f"{fake_bin}:/usr/bin:/bin",
                    ASTERINAS_DESKTOP_M4_CONSOLE=str(console),
                    ASTERINAS_DESKTOP_M4_INPUT_DIRECTORY=str(input_directory),
                    ASTERINAS_DESKTOP_M4_XORG_LOG=str(xorg_log),
                    ASTERINAS_DESKTOP_M4_TIMEOUT_SECONDS="0",
                    ASTERINAS_DESKTOP_M4_TEST_MISSING=missing,
                    ASTERINAS_DESKTOP_M4_XDOTOOL_LOG=str(xdotool_log),
                    ASTERINAS_DESKTOP_SHOW_OVERVIEW="1",
                )

                result = subprocess.run(
                    ["/bin/bash", str(DESKTOP_M4_EVIDENCE_SCRIPT)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )

                if not missing:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        xdotool_log.read_text(encoding="utf-8").splitlines(),
                        [
                            "search --onlyvisible --classname ^netsurf-gtk$",
                            "search --onlyvisible --classname ^xterm$",
                            "search --onlyvisible --classname ^netsurf-gtk$",
                            "set_desktop_for_window 42 1",
                            "search --onlyvisible --classname ^xterm$",
                            "set_desktop_for_window 43 1",
                        ],
                    )
                    self.assertEqual(
                        console.read_text(encoding="utf-8").splitlines(),
                        list(DESKTOP_M4_MILESTONES),
                    )
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(
                        console.read_text(encoding="utf-8").endswith(
                            "DEBIAN_DESKTOP_M4_FAIL reason=desktop-timeout\n"
                        )
                    )

    def test_desktop_m3_evidence_requires_complete_session(self) -> None:
        input_directory = self.directory / "desktop-input"
        input_directory.mkdir()
        (input_directory / "event0").touch()
        (input_directory / "event1").touch()
        xorg_log = self.directory / "Xorg.0.log"
        complete_xorg_log = """(II) FBDEV(0): using shadow framebuffer
(II) XINPUT: Adding extended input device \"Asterinas keyboard\"
(II) XINPUT: Adding extended input device \"Asterinas pointer\"
"""
        fake_bin = self._make_desktop_m3_evidence_tools()

        for missing in (
            "",
            "udev",
            "logind",
            "login-session",
            "keyboard",
            "pointer",
            "xorg-log",
            "mapped-window",
            "matchbox-window-manager",
            "xterm",
        ):
            with self.subTest(missing=missing or "none"):
                keyboard = input_directory / "event0"
                pointer = input_directory / "event1"
                keyboard.touch()
                pointer.touch()
                if missing == "keyboard":
                    keyboard.unlink()
                if missing == "pointer":
                    pointer.unlink()
                console = self.directory / f"desktop-console-{missing or 'complete'}"
                console.write_text("", encoding="utf-8")
                xorg_log.write_text(
                    "" if missing == "xorg-log" else complete_xorg_log,
                    encoding="utf-8",
                )
                environment = os.environ.copy()
                environment.update(
                    PATH=f"{fake_bin}:/usr/bin:/bin",
                    ASTERINAS_DESKTOP_M3_CONSOLE=str(console),
                    ASTERINAS_DESKTOP_M3_INPUT_DIRECTORY=str(input_directory),
                    ASTERINAS_DESKTOP_M3_XORG_LOG=str(xorg_log),
                    ASTERINAS_DESKTOP_M3_TIMEOUT_SECONDS="0",
                    ASTERINAS_DESKTOP_M3_TEST_MISSING=missing,
                )

                result = subprocess.run(
                    ["/bin/bash", str(DESKTOP_M3_EVIDENCE_SCRIPT)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )

                evidence = console.read_text(encoding="utf-8")
                if not missing:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        evidence,
                        "\n".join(DESKTOP_M3_MILESTONES) + "\n",
                    )
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(
                        evidence.endswith(
                            "DEBIAN_DESKTOP_M3_FAIL reason=desktop-timeout\n",
                        ),
                        evidence,
                    )
                    self.assertIn("--- Xorg log ---\n", evidence)

    def _make_desktop_m3_evidence_tools(self) -> Path:
        fake_bin = self.directory / "desktop-evidence-tools"
        fake_bin.mkdir()
        scripts = {
            "systemctl": """#!/bin/sh
case "$ASTERINAS_DESKTOP_M3_TEST_MISSING:$*" in
  'udev:is-active --quiet systemd-udevd.service'|\
  'logind:is-active --quiet systemd-logind.service') exit 1 ;;
  *':is-active --quiet systemd-udevd.service'|\
  *':is-active --quiet systemd-logind.service') exit 0 ;;
  *) exit 1 ;;
esac
""",
            "loginctl": """#!/bin/sh
[ "$ASTERINAS_DESKTOP_M3_TEST_MISSING" = login-session ] && exit 0
printf '1 1000 asterinas seat0 tty1\n'
""",
            "pgrep": """#!/bin/sh
case "$ASTERINAS_DESKTOP_M3_TEST_MISSING:$*" in
  'matchbox-window-manager:'*matchbox-window-manager*|\
  'xterm:'*xterm*) exit 1 ;;
  *'-x matchbox-window-manager') exit 2 ;;
  *) exit 0 ;;
esac
""",
            "xwininfo": """#!/bin/sh
if [ "$ASTERINAS_DESKTOP_M3_TEST_MISSING" = mapped-window ]; then
  printf 'xwininfo: Window id: 0x1 (the root window)\n'
else
  printf '0x200001 "Asterinas Debian": ("xterm" "XTerm") 100x30+48+72\n'
fi
""",
        }
        for name, script in scripts.items():
            path = fake_bin / name
            path.write_text(script, encoding="utf-8")
            path.chmod(0o755)
        return fake_bin

    def _make_desktop_m4_evidence_tools(self) -> Path:
        fake_bin = self.directory / "desktop-m4-evidence-tools"
        fake_bin.mkdir()
        scripts = {
            "systemctl": """#!/bin/sh
case "$*" in
  'is-active --quiet systemd-udevd.service'|\
  'is-active --quiet systemd-logind.service') exit 0 ;;
  *) exit 1 ;;
esac
""",
            "loginctl": """#!/bin/sh
printf '1 1000 asterinas seat0 tty1\n'
""",
            "pgrep": """#!/bin/sh
case "$ASTERINAS_DESKTOP_M4_TEST_MISSING:$*" in
  'hung-openbox:'*'openbox'*) sleep 2; exit 0 ;;
  'desktop-pcmanfm:'*'--desktop'*|\
  'lxpanel:'*'lxpanel'*|\
  'openbox:'*'openbox'*|\
  'netsurf:'*'-x netsurf-gtk'|\
  'xterm:'*'-x xterm') exit 1 ;;
  *) exit 0 ;;
esac
""",
            "xwininfo": """#!/bin/sh
printf '0x200001 "Asterinas Terminal": ("xterm" "XTerm")\n'
printf '0x200003 "Asterinas Start - NetSurf": ("netsurf" "NetSurf")\n'
""",
            "xdotool": """#!/bin/sh
[ -z "${ASTERINAS_DESKTOP_M4_XDOTOOL_LOG:-}" ] || \
  printf '%s\n' "$*" >>"$ASTERINAS_DESKTOP_M4_XDOTOOL_LOG"
[ "$ASTERINAS_DESKTOP_M4_TEST_MISSING" != overview-xdotool ] || exit 1
[ "$ASTERINAS_DESKTOP_M4_TEST_MISSING" != mapped-netsurf ] || \
  [ "$*" != 'search --onlyvisible --classname ^netsurf-gtk$' ] || exit 1
case "$*" in
  'search --onlyvisible --classname ^netsurf-gtk$') printf '42\n' ;;
  'search --onlyvisible --classname ^xterm$') printf '43\n' ;;
  'search '*) exit 9 ;;
esac
""",
        }
        for name, script in scripts.items():
            path = fake_bin / name
            path.write_text(script, encoding="utf-8")
            path.chmod(0o755)
        return fake_bin

    def _make_systemd_m2_evidence_tools(
        self,
        reboot_log: Path,
        sync_log: Path,
    ) -> Path:
        fake_bin = (
            self.directory / f"evidence-tools-{len(list(self.directory.iterdir()))}"
        )
        fake_bin.mkdir()
        scripts = {
            "uname": "#!/bin/sh\nprintf 'riscv64\\n'\n",
            "stat": "#!/bin/sh\ncase \"$*\" in\n  *' /tmp') printf 'tmpfs\\n' ;;\n  *) printf 'ext2/ext3\\n' ;;\nesac\n",
            "dpkg-query": "#!/bin/sh\nprintf 'systemd\\t257.8-1\\nsystemd-sysv\\t257.8-1\\n'\n",
            "systemctl": (
                "#!/bin/sh\ncase \"$*\" in\n"
                "  'start systemd-logind.service'|'is-active --quiet systemd-logind.service') exit 0 ;;\n"
                f"  '--no-block --no-wall reboot') printf 'systemctl:%s\\n' \"$*\" >>'{reboot_log}'; exit 0 ;;\n"
                "  *) exit 1 ;;\nesac\n"
            ),
            "sync": f"#!/bin/sh\nprintf 'sync\\n' >>'{sync_log}'\n",
            "reboot": f"#!/bin/sh\nprintf '%s\\n' \"$*\" >>'{reboot_log}'\n",
        }
        for name, script in scripts.items():
            path = fake_bin / name
            path.write_text(script, encoding="utf-8")
            path.chmod(0o755)
        return fake_bin

    def test_rejects_unknown_argument(self) -> None:
        result = _run_builder("--not-an-option", cwd=self.directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown argument", result.stderr)

    def test_rejects_non_https_mirror(self) -> None:
        result = _run_builder(
            "--mirror",
            "http://deb.debian.org/debian",
            cwd=self.directory,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTPS", result.stderr)

    def test_rejects_unsupported_suite(self) -> None:
        result = _run_builder("--suite", "bookworm", cwd=self.directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported suite", result.stderr)

    def test_rejects_missing_required_tool(self) -> None:
        bin_directory = _make_fake_tools(self.directory)
        (bin_directory / "mke2fs").unlink()
        environment = os.environ.copy()
        environment["PATH"] = str(bin_directory)

        result = _run_builder(cwd=self.directory, environment=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing required tool: mke2fs", result.stderr)

    def test_rejects_unsafe_output_and_cache_paths(self) -> None:
        real_directory = self.directory / "real"
        real_directory.mkdir()
        symlink_path = self.directory / "symlink"
        symlink_path.symlink_to(real_directory, target_is_directory=True)
        masked_symlink_path = self.directory / "missing" / ".." / "symlink"
        cases = (
            ("--output-dir", str(symlink_path)),
            ("--cache-dir", str(symlink_path)),
            ("--output-dir", str(masked_symlink_path)),
            ("--cache-dir", str(masked_symlink_path)),
            (
                "--output-dir",
                str(real_directory),
                "--cache-dir",
                str(real_directory),
            ),
            (
                "--output-dir",
                str(real_directory),
                "--cache-dir",
                str(symlink_path / "cache"),
            ),
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = _run_builder(*arguments, cwd=self.directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe", result.stderr)

    def test_rejects_symlinked_publication_directory(self) -> None:
        output_directory = self.directory / "output"
        metadata_directory = self.directory / "metadata"
        output_directory.mkdir()
        metadata_directory.mkdir()
        (output_directory / "source-metadata").symlink_to(
            metadata_directory,
            target_is_directory=True,
        )

        result = _run_builder(
            "--output-dir",
            str(output_directory),
            "--cache-dir",
            str(self.directory / "cache"),
            cwd=self.directory,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe", result.stderr)

    def test_rejects_symlinked_content_cache_directory(self) -> None:
        cache_directory = self.directory / "cache"
        cache_target = self.directory / "cache-target"
        cache_directory.mkdir()
        cache_target.mkdir()
        (cache_directory / "sha256").symlink_to(
            cache_target,
            target_is_directory=True,
        )

        result = _run_builder(
            "--output-dir",
            str(self.directory / "output"),
            "--cache-dir",
            str(cache_directory),
            cwd=self.directory,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe", result.stderr)

    def test_cache_admission_rejects_symlinked_digest_prefix(self) -> None:
        work_directory = self.directory / "cache-work"
        debs_directory = work_directory / "debs"
        metadata_directory = work_directory / "source-metadata"
        debs_directory.mkdir(parents=True)
        metadata_directory.mkdir()
        archive = debs_directory / "package.deb"
        archive.write_bytes(b"signed package archive")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (work_directory / "package-index-checksums").write_text(
            f"package\triscv64\t1.0\t{digest}\n",
            encoding="utf-8",
        )
        (work_directory / "packages.lock").write_text(
            "package\triscv64\t1.0\n",
            encoding="utf-8",
        )
        cache_directory = self.directory / "cache"
        sha256_directory = cache_directory / "sha256"
        outside_directory = self.directory / "outside"
        sha256_directory.mkdir(parents=True)
        outside_directory.mkdir()
        (sha256_directory / digest[:2]).symlink_to(
            outside_directory,
            target_is_directory=True,
        )

        result = _run_admit_downloaded_packages(work_directory, cache_directory)

        self.assertFalse((outside_directory / f"{digest}.deb").exists())
        self.assertNotEqual(result.returncode, 0)

    def test_skips_archive_superseded_by_installed_version(self) -> None:
        work_directory = self.directory / "superseded-work"
        debs_directory = work_directory / "debs"
        metadata_directory = work_directory / "source-metadata"
        debs_directory.mkdir(parents=True)
        metadata_directory.mkdir()
        archive = (
            debs_directory / "firefox-esr_140.16.0esr-1~deb13u1_riscv64.deb"
        )
        _make_test_deb(
            archive,
            package="firefox-esr",
            version="140.16.0esr-1~deb13u1",
        )
        (work_directory / "package-index-checksums").write_text(
            "",
            encoding="utf-8",
        )
        (work_directory / "packages.lock").write_text(
            "firefox-esr\triscv64\t153.5.1esr-1~deb13u1\n",
            encoding="utf-8",
        )

        result = _run_admit_downloaded_packages(
            work_directory,
            self.directory / "cache",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (metadata_directory / "package-checksums").read_text(encoding="utf-8"),
            "",
        )

    def test_rejects_installed_archive_absent_from_verified_indexes(self) -> None:
        work_directory = self.directory / "absent-work"
        debs_directory = work_directory / "debs"
        metadata_directory = work_directory / "source-metadata"
        debs_directory.mkdir(parents=True)
        metadata_directory.mkdir()
        archive = debs_directory / "example_1.0_riscv64.deb"
        _make_test_deb(archive, package="example", version="1.0")
        (work_directory / "package-index-checksums").write_text(
            "",
            encoding="utf-8",
        )
        (work_directory / "packages.lock").write_text(
            "example\triscv64\t1.0\n",
            encoding="utf-8",
        )

        result = _run_admit_downloaded_packages(
            work_directory,
            self.directory / "cache",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "downloaded package hash is absent from the verified package index",
            result.stderr,
        )

    def test_rejects_invalid_source_date_epoch(self) -> None:
        for value in ("", "00", "01", "+1", "-1", "1.0", "4294967296"):
            with self.subTest(value=value):
                environment = os.environ.copy()
                environment["SOURCE_DATE_EPOCH"] = value
                result = _run_builder(cwd=self.directory, environment=environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SOURCE_DATE_EPOCH", result.stderr)

    def test_rejects_packages_index_not_bound_to_retained_inrelease(self) -> None:
        work_directory = self.directory / "work"
        metadata_directory = work_directory / "source-metadata"
        metadata_directory.mkdir(parents=True)
        (metadata_directory / "InRelease").write_text(
            """Codename: trixie
Version: 13.6
SHA256:
 0000000000000000000000000000000000000000000000000000000000000000 12 main/binary-riscv64/Packages
""",
            encoding="utf-8",
        )
        package_index = work_directory / "Packages"
        package_index.write_bytes(b"Package: bash\n")
        result = _run_builder_function(
            "authenticate_package_index",
            str(package_index),
            "main/binary-riscv64/Packages",
            str(metadata_directory / "InRelease"),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not authenticated by retained InRelease", result.stderr)

    def test_rejects_release_drift_after_package_install(self) -> None:
        work_directory = self.directory / "work"
        metadata_directory = work_directory / "source-metadata"
        metadata_directory.mkdir(parents=True)
        (metadata_directory / "InRelease").write_text(
            "Codename: trixie\nVersion: 13.6\n",
            encoding="utf-8",
        )
        bin_directory = self.directory / "release-tools"
        bin_directory.mkdir()
        curl = bin_directory / "curl"
        curl.write_text(
            """#!/bin/sh
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--output" ]; then
        shift
        printf 'Codename: trixie\nVersion: 13.7\n' >"$1"
        exit 0
    fi
    shift
done
exit 64
""",
            encoding="utf-8",
        )
        curl.chmod(0o755)
        gpgv = bin_directory / "gpgv"
        gpgv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gpgv.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"

        result = _run_builder_function(
            "verify_release_is_unchanged",
            str(work_directory),
            "https://deb.debian.org/debian",
            "trixie",
            "13.6",
            environment=environment,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("signed release changed during build", result.stderr)

    def test_accepts_safe_packaged_keyring_paths(self) -> None:
        keyring_directory = self.directory / "keyrings"
        keyring_directory.mkdir()
        regular_keyring = keyring_directory / "archive.pgp"
        regular_keyring.write_bytes(b"keyring")
        regular_keyring.chmod(0o644)
        packaged_link = keyring_directory / "archive.gpg"
        packaged_link.symlink_to(regular_keyring.name)
        fake_bin = _make_fake_root_stat(self.directory)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

        for keyring in (regular_keyring, packaged_link):
            with self.subTest(keyring=keyring):
                result = _run_builder_function(
                    "require_safe_keyring_path",
                    str(keyring),
                    environment=environment,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_unsafe_keyring_paths(self) -> None:
        keyring_directory = self.directory / "unsafe-keyrings"
        keyring_directory.mkdir()

        safe_target = keyring_directory / "safe-target.pgp"
        safe_target.write_bytes(b"keyring")
        safe_target.chmod(0o644)
        nested_directory = keyring_directory / "nested"
        nested_directory.mkdir()
        nested_target = nested_directory / "target.pgp"
        nested_target.write_bytes(b"keyring")
        writable_target = keyring_directory / "writable.pgp"
        writable_target.write_bytes(b"keyring")
        writable_target.chmod(0o664)
        nonroot_target = keyring_directory / "nonroot.pgp"
        nonroot_target.write_bytes(b"keyring")
        directory_target = keyring_directory / "directory-target"
        directory_target.mkdir()
        second_link = keyring_directory / "second-link"
        second_link.symlink_to(safe_target.name)
        control_target = keyring_directory / "control\nname"
        control_target.write_bytes(b"keyring")

        unsafe_paths = []
        for name, target in (
            ("absolute", str(safe_target)),
            ("slash", "nested/target.pgp"),
            ("dotdot", "safe..target.pgp"),
            ("missing-link", "missing-target.pgp"),
            ("directory", directory_target.name),
            ("second-symlink", second_link.name),
            ("writable", writable_target.name),
            ("nonroot", nonroot_target.name),
            ("control", control_target.name),
        ):
            link = keyring_directory / name
            link.symlink_to(target)
            unsafe_paths.append(link)
        unsafe_paths.append(keyring_directory / "missing-regular")

        fake_bin = _make_fake_root_stat(self.directory)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
        for keyring in unsafe_paths:
            with self.subTest(keyring=keyring):
                result = _run_builder_function(
                    "require_safe_keyring_path",
                    str(keyring),
                    environment=environment,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe Debian archive keyring", result.stderr)

    def test_image_bash_smoke_uses_executable_debugfs_dump(self) -> None:
        work_directory = self.directory / "image-work"
        (work_directory / "stage").mkdir(parents=True)
        bin_directory = self.directory / "image-tools"
        bin_directory.mkdir()
        fake_tools = {
            "mke2fs": "#!/bin/sh\nexit 0\n",
            "dumpe2fs": """#!/bin/sh
cat <<EOF
Filesystem volume name:   ASTER_DEBIANROOT
Filesystem UUID:          7b7ad749-77d0-4e59-89e4-e117244a70aa
Block size:               4096
Filesystem features:      ext_attr resize_inode dir_index filetype sparse_super large_file
EOF
""",
            "debugfs": """#!/bin/sh
case "$2" in
    "stat /usr/bin/qemu-riscv64-static")
        printf 'File not found by ext2_lookup\n'
        ;;
    "stat "*)
        printf 'Inode: 12   Type: regular\n'
        ;;
    "dump /bin/bash "*)
        destination=${2#dump /bin/bash }
        printf 'fake ELF' >"$destination"
        chmod 0600 "$destination"
        ;;
    *) exit 64 ;;
esac
""",
            "qemu-riscv64-static": """#!/bin/sh
if [ ! -x "$3" ]; then
    exit 86
fi
printf 'QEMU_SMOKE_EXECUTED\n'
""",
        }
        for name, contents in fake_tools.items():
            path = bin_directory / name
            path.write_text(contents, encoding="utf-8")
            path.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"

        result = _run_image_creation(work_directory, environment=environment)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("QEMU_SMOKE_EXECUTED", result.stdout)

    def test_publish_permissions_only_open_new_directories(self) -> None:
        work_directory = self.directory / "publish-work"
        (work_directory / "source-metadata").mkdir(parents=True)
        for relative_path in PUBLISHED_ARTIFACTS:
            source = work_directory / relative_path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(relative_path.encode())

        new_output = self.directory / "new-output"
        result = _run_publish_artifacts(work_directory, new_output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(new_output.stat().st_mode & 0o777, 0o755)
        self.assertEqual(
            (new_output / "source-metadata").stat().st_mode & 0o777,
            0o755,
        )
        for relative_path in PUBLISHED_ARTIFACTS:
            self.assertEqual((new_output / relative_path).stat().st_mode & 0o777, 0o644)

        existing_output = self.directory / "existing-output"
        existing_metadata = existing_output / "source-metadata"
        existing_metadata.mkdir(parents=True)
        existing_output.chmod(0o700)
        existing_metadata.chmod(0o700)
        result = _run_publish_artifacts(work_directory, existing_output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(existing_output.stat().st_mode & 0o777, 0o700)
        self.assertEqual(existing_metadata.stat().st_mode & 0o777, 0o700)

    def test_main_propagates_publication_failure_without_success_log(self) -> None:
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                """source "$1"
parse_arguments() { :; }
validate_configuration() { :; }
require_tools() { :; }
prepare_private_workspace() { :; }
fetch_and_verify_release() { :; }
bootstrap_rootfs() { :; }
install_rootfs_packages() { :; }
audit_packages() { :; }
configure_and_normalize_rootfs() { :; }
create_and_verify_image() { :; }
write_rootfs_manifest() { :; }
publish_artifacts() { return 143; }
main
""",
                "builder-publication-failure-test",
                str(BUILD_SCRIPT),
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 143)
        self.assertNotIn("published signed Debian rootfs", result.stderr)

    def test_workspace_permissions_separate_public_and_private_paths(self) -> None:
        public_root = self.directory / "new-public"
        output_directory = public_root / "nested" / "rootfs"
        cache_root = self.directory / "new-cache"
        cache_directory = cache_root / "nested"

        result = _run_prepare_private_workspace(output_directory, cache_directory)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PRIVATE_MODE=700", result.stdout)
        self.assertEqual(public_root.stat().st_mode & 0o777, 0o755)
        self.assertEqual((public_root / "nested").stat().st_mode & 0o777, 0o755)
        self.assertEqual(cache_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(cache_directory.stat().st_mode & 0o777, 0o700)

        existing_public_root = self.directory / "existing-public"
        existing_public_root.mkdir()
        existing_public_root.chmod(0o700)
        result = _run_prepare_private_workspace(
            existing_public_root / "nested" / "rootfs",
            self.directory / "second-cache",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(existing_public_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (existing_public_root / "nested").stat().st_mode & 0o777,
            0o755,
        )

    def test_command_failure_preserves_every_published_artifact(self) -> None:
        output_directory = self.directory / "output with spaces"
        output_directory.mkdir()
        original_contents: dict[str, bytes] = {}
        for index, artifact in enumerate(PUBLISHED_ARTIFACTS):
            artifact_path = output_directory / artifact
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            contents = f"existing artifact {index}\n".encode()
            artifact_path.write_bytes(contents)
            original_contents[artifact] = contents

        bin_directory = _make_fake_tools(self.directory, failing_tool="curl")
        environment = os.environ.copy()
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"
        result = _run_builder(
            "--output-dir",
            str(output_directory),
            "--cache-dir",
            str(self.directory / "cache with spaces"),
            cwd=self.directory,
            environment=environment,
        )

        self.assertEqual(result.returncode, 97)
        for artifact, contents in original_contents.items():
            self.assertEqual((output_directory / artifact).read_bytes(), contents)


if __name__ == "__main__":
    unittest.main()
