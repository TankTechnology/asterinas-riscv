# SPDX-License-Identifier: MPL-2.0

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.riscv.debian.rootfs.profiles import get_profile
from tools.riscv.debian.rootfs.contract import ContractError, load_manifest, write_manifest
from tools.riscv.debian.rootfs import fsops as fsops_module
from tools.riscv.debian.rootfs import contract as contract_module
from tools.riscv.debian.rootfs.signed_sources import (
    BASE_SOURCE,
    M5_SOURCES,
    SECURITY_SOURCE,
    authenticate_packages,
    require_unchanged,
    signed_sources_manifest,
    source_for_apt_list,
    verify_inrelease,
)


class DebianSignedSourcesTests(unittest.TestCase):
    BASE_HEADER = """Suite: stable
Version: 13.6
Codename: trixie
Architectures: all amd64 arm64 riscv64
Components: main contrib non-free-firmware non-free
"""
    SECURITY_HEADER = """Suite: stable-security
Version: 13
Codename: trixie-security
Architectures: amd64 arm64 riscv64
Components: updates/main updates/contrib updates/non-free-firmware updates/non-free
"""

    def test_browser_m5_profile_replaces_netsurf_with_firefox(self) -> None:
        profile = get_profile("browser-m5")
        self.assertEqual(profile.schema_version, 6)
        self.assertEqual(profile.root_label, "ASTER_BROWSERM5")
        self.assertLessEqual(len(profile.root_label.encode("ascii")), 16)
        self.assertIn("firefox-esr", profile.requested_packages)
        self.assertIn("iproute2", profile.requested_packages)
        self.assertIn("iputils-ping", profile.requested_packages)
        self.assertIn("iproute2", profile.identity_packages)
        self.assertIn("iputils-ping", profile.identity_packages)
        self.assertIn("curl", profile.requested_packages)
        self.assertIn("curl", profile.identity_packages)
        self.assertNotIn("netsurf-gtk", profile.requested_packages)
        self.assertNotIn("xdotool", profile.requested_packages)

    @mock.patch("subprocess.run")
    def test_each_release_is_independently_gpg_verified(self, run: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keyring = root / "archive.gpg"
            keyring.touch()
            base = root / "base.InRelease"
            base.write_text(self.BASE_HEADER)
            security = root / "security.InRelease"
            security.write_text(self.SECURITY_HEADER)
            self.assertEqual(verify_inrelease(BASE_SOURCE, base, keyring), "13.6")
            self.assertEqual(verify_inrelease(SECURITY_SOURCE, security, keyring), "13")
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:2], ["gpgv", "--keyring"])
            self.assertTrue(call.kwargs["check"])

    @mock.patch("subprocess.run")
    def test_rejects_wrong_duplicate_and_security_metadata(self, _: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "InRelease"
            keyring = Path(directory) / "keyring"
            for document, reason in (
                (self.SECURITY_HEADER + "Codename: trixie-security\n", "duplicate"),
                (self.SECURITY_HEADER.replace("trixie-security", "bookworm-security"), "Codename"),
                (self.SECURITY_HEADER.replace("Version: 13\n", ""), "Version"),
                (self.SECURITY_HEADER.replace("Version: 13\n", "Version: 13.0\n"), "Version"),
                (self.SECURITY_HEADER.replace("Suite: stable-security", "Suite: testing-security"), "Suite"),
                (self.SECURITY_HEADER.replace(" arm64 riscv64", " arm64"), "Architectures"),
                (self.SECURITY_HEADER.replace("updates/main", "main"), "Components"),
            ):
                path.write_text(document)
                with self.assertRaisesRegex(ValueError, reason):
                    verify_inrelease(SECURITY_SOURCE, path, keyring)

    def test_drift_and_unknown_or_ambiguous_list_ownership_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retained = Path(directory) / "retained"
            current = Path(directory) / "current"
            retained.write_bytes(b"one")
            current.write_bytes(b"two")
            with self.assertRaisesRegex(ValueError, "changed"):
                require_unchanged(retained, current, "security")
        for source in M5_SOURCES:
            for configured_component in source.components:
                component = configured_component.rsplit("/", 1)[-1]
                for architecture in ("riscv64", "all"):
                    with self.subTest(
                        role=source.role,
                        component=component,
                        architecture=architecture,
                    ):
                        self.assertEqual(
                            source_for_apt_list(
                                f"mirror_dists_{source.suite}_{component}_"
                                f"binary-{architecture}_Packages.xz"
                            ),
                            source,
                        )
        with self.assertRaisesRegex(ValueError, "0 source owners"):
            source_for_apt_list(
                "mirror_dists_trixie_firmware_binary-riscv64_Packages.xz"
            )
        with self.assertRaisesRegex(ValueError, "2 source owners"):
            source_for_apt_list(
                "x_dists_trixie_main_binary-riscv64_Packages_"
                "dists_trixie-security_main_binary-riscv64_Packages"
            )

    def test_packages_must_match_exactly_one_hash_size_path_row(self) -> None:
        index = b"Package: firefox-esr\nArchitecture: riscv64\n"
        digest = hashlib.sha256(index).hexdigest()
        row = f" {digest} {len(index)} main/binary-riscv64/Packages\n"
        authenticate_packages(index, "main/binary-riscv64/Packages", ("SHA256:\n" + row).encode())
        for release in (b"SHA256:\n", ("SHA256:\n" + row + row).encode()):
            with self.assertRaisesRegex(ValueError, "uniquely authenticated"):
                authenticate_packages(index, "main/binary-riscv64/Packages", release)

    def test_manifest_fragment_is_sorted_complete_and_content_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {"base": root / "base", "security": root / "security"}
            files["base"].write_bytes(b"base signed")
            files["security"].write_bytes(b"security signed")
            rows = signed_sources_manifest(tuple(reversed(M5_SOURCES)), files)
        self.assertEqual([row["role"] for row in rows], ["base", "security"])
        self.assertEqual(rows[1]["suite"], "trixie-security")
        self.assertEqual(rows[1]["inrelease_sha256"], hashlib.sha256(b"security signed").hexdigest())

    def _m5_payload(self) -> dict[str, object]:
        profile = get_profile("browser-m5")
        zero = "0" * 64
        return {
            "schema_version": 6,
            "profile": "browser-m5",
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
                for source in M5_SOURCES
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
                "size_bytes": 1073741824,
                "block_size_bytes": 4096,
            },
            "tool_versions": {"debootstrap": "test"},
            "build_timestamp": "2026-08-26T00:00:00Z",
            "root_image_sha256": zero,
            "gate_packages": {name: "1" for name in profile.identity_packages},
        }

    def test_schema_six_loads_exact_sources_and_package_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text(json.dumps(self._m5_payload()))
            manifest = load_manifest(manifest_path)
        self.assertEqual([row[0] for row in manifest.signed_sources], ["base", "security"])
        self.assertEqual(manifest.downloaded_packages[0][4], "security")
        self.assertEqual(manifest.signed_metadata_url, "")

    def test_schema_six_rejects_missing_extra_or_replaced_source_contract(self) -> None:
        mutations = (
            lambda rows: rows.pop(),
            lambda rows: rows.append(dict(rows[0])),
            lambda rows: rows[0].update(mirror_url="https://example.invalid/debian"),
            lambda rows: rows[1].update(suite="sid-security"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            for mutate in mutations:
                payload = self._m5_payload()
                mutate(payload["signed_sources"])
                path.write_text(json.dumps(payload))
                with self.assertRaisesRegex(ContractError, "source contract"):
                    load_manifest(path)

    def test_schema_six_requires_source_role_exact_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            for role in (None, "updates"):
                payload = self._m5_payload()
                package = payload["downloaded_packages"][0]
                if role is None:
                    package.pop("source_role")
                else:
                    package["source_role"] = role
                path.write_text(json.dumps(payload))
                with self.assertRaises(ContractError):
                    load_manifest(path)

    def test_signed_sources_writer_rejects_partial_source_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"
            base.write_bytes(b"base")
            with self.assertRaisesRegex(ValueError, "browser-m5 contract"):
                signed_sources_manifest((BASE_SOURCE,), {"base": base})

    def _writer_inputs(self, root: Path, *, firefox_role: str = "security") -> dict[str, object]:
        profile = get_profile("browser-m5")
        package_names = sorted(set(profile.requested_packages + profile.identity_packages))
        rows = [(name, "riscv64", "1") for name in package_names]
        image = root / "image"
        image.write_bytes(b"image")
        lock = root / "packages.lock"
        lock.write_text("".join(f"{name}\t{arch}\t{version}\n" for name, arch, version in rows))
        checksums = root / "package-checksums"
        checksum_rows = [
            (name, arch, version, hashlib.sha256(name.encode()).hexdigest(), firefox_role if name == "firefox-esr" else "base")
            for name, arch, version in rows
        ]
        checksums.write_text("".join("\t".join(row) + "\n" for row in checksum_rows))
        inrelease = root / "legacy-InRelease"
        inrelease.write_bytes(b"legacy")
        source_files = {"base": root / "base-InRelease", "security": root / "security-InRelease"}
        source_files["base"].write_bytes(b"base")
        source_files["security"].write_bytes(b"security")
        return {
            "output": root / "manifest.json",
            "image": image,
            "packages_lock": lock,
            "inrelease": inrelease,
            "package_checksums": checksums,
            "mirror_url": BASE_SOURCE.mirror_url,
            "suite": "trixie",
            "debian_release": "13.6",
            "build_timestamp": "2026-08-26T00:00:00Z",
            "tool_versions": ("debootstrap=test",),
            "profile_name": "browser-m5",
            "signed_source_files": source_files,
        }

    @mock.patch("tools.riscv.debian.rootfs.contract._write_validated_manifest_atomically")
    def test_schema_six_writer_emits_exact_sources_and_package_roles(self, publish: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inputs = self._writer_inputs(Path(directory))
            write_manifest(**inputs)
        serialized = publish.call_args.args[1]
        payload = json.loads(serialized)
        self.assertNotIn("signed_metadata", payload)
        self.assertEqual([row["role"] for row in payload["signed_sources"]], ["base", "security"])
        firefox = next(row for row in payload["downloaded_packages"] if row["name"] == "firefox-esr")
        curl = next(row for row in payload["downloaded_packages"] if row["name"] == "curl")
        self.assertEqual(firefox["source_role"], "security")
        self.assertEqual(curl["source_role"], "base")

    def test_writer_rejects_firefox_from_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inputs = self._writer_inputs(Path(directory), firefox_role="base")
            with self.assertRaisesRegex(ContractError, "firefox-esr"):
                write_manifest(**inputs)

    def test_writer_rejects_a_base_mirror_not_named_in_schema_six(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inputs = self._writer_inputs(Path(directory))
            inputs["mirror_url"] = "https://example.invalid/debian"
            with self.assertRaisesRegex(ContractError, "mirror_url"):
                write_manifest(**inputs)

    def test_writer_output_cannot_alias_either_signed_inrelease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inputs = self._writer_inputs(Path(directory))
            for role in ("base", "security"):
                aliased = dict(inputs)
                aliased["output"] = inputs["signed_source_files"][role]
                with self.assertRaisesRegex(ContractError, "aliases input"):
                    write_manifest(**aliased)

    def test_old_schema_rejects_signed_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inputs = self._writer_inputs(Path(directory))
            inputs["profile_name"] = "minimal-m1"
            with self.assertRaisesRegex(ContractError, "only valid for browser-m5"):
                write_manifest(**inputs)

    def test_builder_and_session_have_closed_offline_m5_contract(self) -> None:
        rootfs = Path(__file__).parents[1] / "debian/rootfs"
        builder = (rootfs / "build_rootfs.sh").read_text()
        session = (rootfs / "desktop_m5_session.sh").read_text()
        firefox = (rootfs / "browser_m5_firefox.sh").read_text()
        evidence = (rootfs / "desktop_m5_evidence.sh").read_text()
        self.assertIn("$SECURITY_MIRROR/dists/trixie-security/InRelease", builder)
        self.assertIn("verify_m5_releases_are_unchanged", builder)
        self.assertIn("tools.riscv.debian.rootfs.signed_sources owner", builder)
        self.assertIn('print $1, $2, $3, $4, role', builder)
        self.assertIn('--signed-source "base=', builder)
        self.assertIn('--signed-source "security=', builder)
        self.assertIn("browser_m5.webm.base64", builder)
        self.assertIn("probe_video_file", builder)
        self.assertIn("install -m 0644", builder)
        self.assertNotIn("firefox-esr", session)
        self.assertIn("--offline", firefox)
        self.assertIn('"$PROBE_URL"', firefox)
        self.assertIn('--profile "$PROFILE"', firefox)
        self.assertEqual(firefox.count("file:///"), 1)
        self.assertNotIn("http://", firefox)
        self.assertNotIn("https://", firefox)
        for baseline in ("UDEV", "LOGIND", "SESSION", "INPUT", "XORG", "CLIENTS"):
            self.assertIn(f"DEBIAN_BROWSER_M5_{baseline}", evidence)
        self.assertIn("DEBIAN_BROWSER_M5_WORKLOAD mode=offline scheme=file network=private-loopback", evidence)
        self.assertIn("browser-m5-marionette-gate", evidence)
        self.assertIn('emit "$content_evidence"', evidence)
        self.assertNotIn("VIDEO_CANPLAY", evidence)
        self.assertNotIn("VIDEO_ENDED", evidence)

        result = subprocess.run(
            [str(rootfs / "build_rootfs.sh"), "--profile", "browser-m5", "--print-packages"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        packages = result.stdout.splitlines()
        self.assertIn("firefox-esr", packages)
        self.assertIn("curl", packages)
        self.assertIn("iproute2", packages)
        self.assertIn("iputils-ping", packages)
        self.assertNotIn("netsurf-gtk", packages)
        tools = subprocess.run(
            [str(rootfs / "build_rootfs.sh"), "--profile", "browser-m5", "--print-tools"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(tools.returncode, 0, tools.stderr)
        self.assertIn("ffprobe", tools.stdout.splitlines())
        self.assertIn("ffmpeg", tools.stdout.splitlines())

    def test_m5_publication_rolls_back_security_metadata_with_the_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            for base, prefix in ((source, b"new:"), (output, b"old:")):
                for relative in fsops_module._M5_PUBLISHED_PATHS:
                    path = base / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(prefix + relative.encode())
            before = {
                relative: (output / relative).read_bytes()
                for relative in fsops_module._M5_PUBLISHED_PATHS
            }
            real_replace = os.replace
            replacements = 0

            def fail_after_security_is_prepared(*args, **kwargs):
                nonlocal replacements
                replacements += 1
                if replacements == 3:
                    raise OSError("injected M5 publication failure")
                return real_replace(*args, **kwargs)

            with (
                mock.patch.object(fsops_module.os, "replace", new=fail_after_security_is_prepared),
                self.assertRaisesRegex(OSError, "injected M5 publication failure"),
            ):
                fsops_module.publish_set(
                    output, source, include_security_inrelease=True
                )
            self.assertEqual(
                {
                    relative: (output / relative).read_bytes()
                    for relative in fsops_module._M5_PUBLISHED_PATHS
                },
                before,
            )

    @mock.patch("tools.riscv.debian.rootfs.contract._write_validated_manifest_atomically")
    def test_manifest_cli_forwards_both_signed_sources(self, publish: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inputs = self._writer_inputs(Path(directory))
            sources = inputs["signed_source_files"]
            arguments = [
                "write-manifest",
                "--output", str(inputs["output"]),
                "--image", str(inputs["image"]),
                "--packages-lock", str(inputs["packages_lock"]),
                "--inrelease", str(inputs["inrelease"]),
                "--package-checksums", str(inputs["package_checksums"]),
                "--mirror", str(inputs["mirror_url"]),
                "--suite", str(inputs["suite"]),
                "--debian-release", str(inputs["debian_release"]),
                "--build-timestamp", str(inputs["build_timestamp"]),
                "--tool-version", "debootstrap=test",
                "--profile", "browser-m5",
                "--signed-source", f"base={sources['base']}",
                "--signed-source", f"security={sources['security']}",
            ]
            self.assertEqual(contract_module.main(arguments), 0)
        payload = json.loads(publish.call_args.args[1])
        self.assertEqual(
            [source["role"] for source in payload["signed_sources"]],
            ["base", "security"],
        )

    def test_builder_installs_verified_read_only_probe_and_m5_session(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        builder = repository / "tools/riscv/debian/rootfs/build_rootfs.sh"
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            stage = work / "stage"
            for relative in (
                "etc/systemd/system",
                "etc/X11/xorg.conf.d",
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
                    'source "$1"; PROFILE=browser-m5; configure_profile 1; '
                    'WORK_DIR="$2"; configure_and_normalize_rootfs',
                    "m5-configure-test",
                    str(builder),
                    str(work),
                ],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            probe = stage / "usr/share/asterinas/browser-m5/index.html"
            video = stage / "usr/share/asterinas/browser-m5/browser-m5.webm"
            session = stage / "usr/lib/asterinas/desktop-m5-session"
            evidence = stage / "usr/lib/asterinas/desktop-m5-evidence"
            content_gate = stage / "usr/lib/asterinas/browser-m5-marionette-gate"
            firefox_launcher = stage / "usr/lib/asterinas/browser-m5-firefox"
            observer = stage / "usr/lib/asterinas/browser-m5-network-observer"
            firefox_unit = stage / "etc/systemd/system/asterinas-browser-m5.service"
            observer_unit = stage / "etc/systemd/system/asterinas-browser-m5-network-observer.service"
            netsurf_evidence_unit = (
                stage / "etc/systemd/system/asterinas-desktop-m6-browser.service"
            )
            evidence_unit = stage / "etc/systemd/system/asterinas-desktop-m5-evidence.service"
            logind_timeout = (
                stage
                / "etc/systemd/system/systemd-logind.service.d"
                / "asterinas-browser-m5-timeout.conf"
            )
            network_evidence = (
                stage / "usr/lib/asterinas/desktop-m5-network-evidence"
            )
            network_unit = (
                stage
                / "etc/systemd/system/asterinas-desktop-m5-network.service"
            )
            policy = stage / "usr/lib/firefox-esr/distribution/policies.json"
            self.assertEqual(probe.stat().st_mode & 0o777, 0o644)
            self.assertEqual(video.stat().st_mode & 0o777, 0o644)
            self.assertEqual(session.stat().st_mode & 0o777, 0o755)
            self.assertEqual(evidence.stat().st_mode & 0o777, 0o755)
            self.assertEqual(content_gate.stat().st_mode & 0o777, 0o755)
            self.assertEqual(firefox_launcher.stat().st_mode & 0o777, 0o755)
            self.assertEqual(observer.stat().st_mode & 0o777, 0o755)
            self.assertIn('"WebDriver:ExecuteScript"', content_gate.read_text())
            self.assertEqual(network_evidence.stat().st_mode & 0o777, 0o755)
            self.assertIn(
                "After=local-fs.target",
                network_unit.read_text(),
            )
            self.assertIn(
                "Before=asterinas-desktop-m5.service",
                network_unit.read_text(),
            )
            self.assertEqual(policy.stat().st_mode & 0o777, 0o644)
            policies = json.loads(policy.read_text())["policies"]
            self.assertTrue(policies["DisableTelemetry"])
            self.assertTrue(policies["DontCheckDefaultBrowser"])
            self.assertEqual(policies["OverrideFirstRunPage"], "")
            self.assertEqual(video.read_bytes()[:4], bytes.fromhex("1a45dfa3"))
            self.assertNotIn("firefox-esr", session.read_text())
            self.assertIn("--offline", firefox_launcher.read_text())
            self.assertIn(
                "file:///usr/share/asterinas/browser-m5/index.html",
                firefox_launcher.read_text(),
            )
            firefox_unit_text = firefox_unit.read_text()
            observer_unit_text = observer_unit.read_text()
            evidence_unit_text = evidence_unit.read_text()
            self.assertEqual(logind_timeout.stat().st_mode & 0o777, 0o644)
            self.assertEqual(
                logind_timeout.read_text(),
                "[Service]\n"
                "# Software-emulated SMP RISC-V needs more than systemd's default while\n"
                "# constructing logind's mount namespace. Keep the extension profile-local.\n"
                "TimeoutStartSec=300s\n",
            )
            self.assertIn("PrivateNetwork=yes", firefox_unit_text)
            self.assertIn("User=asterinas", firefox_unit_text)
            self.assertNotIn("PrivateNetwork", observer_unit_text)
            self.assertIn("JoinsNamespaceOf=asterinas-browser-m5.service", evidence_unit_text)
            self.assertLess(evidence_unit_text.index("JoinsNamespaceOf="), evidence_unit_text.index("[Service]"))
            self.assertIn("PrivateNetwork=yes", evidence_unit_text)
            self.assertIn("Requires=asterinas-browser-m5.service", evidence_unit_text)
            self.assertFalse(netsurf_evidence_unit.exists())
            self.assertFalse(
                (stage / "usr/lib/asterinas/desktop-m6-browser-evidence").exists()
            )

    def test_builder_rejects_custom_m5_mirror_before_tools_or_network(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        builder = repository / "tools/riscv/debian/rootfs/build_rootfs.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(
                [
                    str(builder),
                    "--profile", "browser-m5",
                    "--mirror", "https://example.invalid/debian",
                    "--output-dir", str(root / "output"),
                    "--cache-dir", str(root / "cache"),
                ],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("base mirror must be exactly", result.stderr)
        self.assertNotIn("missing required tool", result.stderr)
        self.assertNotIn("phase 1/8", result.stderr)

    def test_builder_resolves_identical_cross_source_archives_deterministically(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        builder = repository / "tools/riscv/debian/rootfs/build_rootfs.sh"
        checksum = "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            checksums = Path(directory) / "checksums"
            checksums.write_text(
                f"libfreetype6\triscv64\t1\t{checksum}\tbase\n"
                f"libfreetype6\triscv64\t1\t{checksum}\tsecurity\n"
            )
            base = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    'source "$1"; resolve_downloaded_package_row "$2" archive.deb "$3"',
                    "m5-source-owner-test",
                    str(builder),
                    checksum,
                    str(checksums),
                ],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )
            checksums.write_text(
                f"firefox-esr\triscv64\t1\t{checksum}\tbase\n"
                f"firefox-esr\triscv64\t1\t{checksum}\tsecurity\n"
            )
            firefox = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    'source "$1"; resolve_downloaded_package_row "$2" archive.deb "$3"',
                    "m5-source-owner-test",
                    str(builder),
                    checksum,
                    str(checksums),
                ],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(base.returncode, 0, base.stderr)
        self.assertTrue(base.stdout.rstrip().endswith("\tbase"))
        self.assertEqual(firefox.returncode, 0, firefox.stderr)
        self.assertTrue(firefox.stdout.rstrip().endswith("\tsecurity"))

    def test_builder_rejects_hash_collision_across_package_identities(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        builder = repository / "tools/riscv/debian/rootfs/build_rootfs.sh"
        checksum = "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            checksums = Path(directory) / "checksums"
            checksums.write_text(
                f"first\triscv64\t1\t{checksum}\tbase\n"
                f"second\triscv64\t1\t{checksum}\tsecurity\n"
            )
            result = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    'source "$1"; resolve_downloaded_package_row "$2" archive.deb "$3"',
                    "m5-source-owner-test",
                    str(builder),
                    checksum,
                    str(checksums),
                ],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("multiple identities", result.stderr)

    def test_builder_excludes_superseded_cached_package_versions(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        builder = repository / "tools/riscv/debian/rootfs/build_rootfs.sh"
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "packages.lock"
            lock.write_text("util-linux\triscv64\t2.41.5-0+deb13u1\n")
            result = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    'source "$1"; '
                    'if package_row_is_installed util-linux riscv64 2.41-5 "$2"; '
                    'then old=0; else old=$?; fi; '
                    'if package_row_is_installed util-linux riscv64 2.41.5-0+deb13u1 "$2"; '
                    'then new=0; else new=$?; fi; '
                    'printf "%s %s\\n" "$old" "$new"',
                    "m5-installed-package-test",
                    str(builder),
                    str(lock),
                ],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1 0")

    def test_non_browser_profile_rejects_stale_security_metadata_early(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        builder = repository / "tools/riscv/debian/rootfs/build_rootfs.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            metadata = output / "source-metadata"
            metadata.mkdir(parents=True)
            stale = metadata / "Security-InRelease"
            stale.write_text("stale browser provenance")
            result = subprocess.run(
                [
                    str(builder),
                    "--profile", "desktop-m5-network",
                    "--output-dir", str(output),
                    "--cache-dir", str(root / "cache"),
                ],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("stale browser-m5 artifact", result.stderr)
        self.assertNotIn("missing required tool", result.stderr)
        self.assertNotIn("phase 1/8", result.stderr)

    def test_manifest_cli_rejects_custom_m5_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inputs = self._writer_inputs(Path(directory))
            sources = inputs["signed_source_files"]
            arguments = [
                "write-manifest",
                "--output", str(inputs["output"]),
                "--image", str(inputs["image"]),
                "--packages-lock", str(inputs["packages_lock"]),
                "--inrelease", str(inputs["inrelease"]),
                "--package-checksums", str(inputs["package_checksums"]),
                "--mirror", "https://example.invalid/debian",
                "--suite", "trixie",
                "--debian-release", "13.6",
                "--build-timestamp", "2026-08-26T00:00:00Z",
                "--tool-version", "debootstrap=test",
                "--profile", "browser-m5",
                "--signed-source", f"base={sources['base']}",
                "--signed-source", f"security={sources['security']}",
            ]
            with self.assertRaises(SystemExit) as caught:
                contract_module.main(arguments)
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
