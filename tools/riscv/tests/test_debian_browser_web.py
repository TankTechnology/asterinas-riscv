#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import zlib

from tools.riscv.debian.rootfs.browser_m5_qemu_gate import BROWSER_M5_MILESTONES
from tools.riscv.debian.rootfs.browser_web_marionette_gate import (
    GateError,
    _submit_baidu_search,
    select_bilibili_video,
    validate_baidu_home,
    validate_baidu_search,
    validate_bilibili_detail,
)
from tools.riscv.debian.rootfs.browser_web_online_rootfs_check import (
    CheckFailure as OnlineCheckFailure,
    check_root as check_online_root,
)
from tools.riscv.debian.rootfs.browser_web_qemu_gate import (
    BROWSER_WEB_MILESTONES,
    BrowserWebQemuOperations,
    KERNEL_FATAL_MARKERS,
    WEB_EVIDENCE_PATHS,
    _extract_web_evidence,
    browser_web_qemu_argv,
    classify_browser_web_qemu,
    validate_web_evidence,
)
from tools.riscv.debian.rootfs.contract import ContractError, load_manifest, write_manifest
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure
from tools.riscv.debian.rootfs.signed_sources import M5_SOURCES
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import DESKTOP_M5_QEMU_BOOTARGS
from tools.riscv.debian.rootfs.profiles import get_profile


ROOT = Path(__file__).resolve().parents[3]
ROOTFS = ROOT / "tools/riscv/debian/rootfs"


def navigation(url: str, *, tls: float) -> dict[str, object]:
    return {
        "name": url,
        "entryType": "navigation",
        "startTime": 0,
        "duration": 120,
        "domainLookupStart": 1,
        "domainLookupEnd": 2,
        "connectStart": 2,
        "secureConnectionStart": tls,
        "connectEnd": 5,
        "requestStart": 6,
        "responseStart": 20,
        "responseEnd": 40,
        "domContentLoadedEventEnd": 80,
        "loadEventEnd": 120,
        "nextHopProtocol": "h2",
    }


def snapshot(url: str, *, tls: float = 3) -> dict[str, object]:
    return {
        "url": url,
        "title": "Asterinas live content",
        "readyState": "complete",
        "bodyText": "Real dynamic web content with enough visible text for the formal gate.",
        "jsComplete": True,
        "dom": {
            "baiduKeyword": False,
            "baiduSubmit": False,
            "baiduResults": 0,
            "bilibiliHome": False,
            "bilibiliDetail": False,
        },
        "links": [],
        "navigation": navigation(url, tls=tls),
        "resources": [
            {
                "name": "https://static.example.invalid/app.js",
                "initiatorType": "script",
                "duration": 12,
                "transferSize": 42,
            }
        ],
    }


def png(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\0" + b"\x20\x40\x60" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def png_claiming_large_decode() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(
            ">I", zlib.crc32(kind + payload) & 0xFFFFFFFF
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 16000, 1400, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


def png_with_second_zlib_stream() -> bytes:
    source = png()
    offset = 8
    output = bytearray(source[:8])
    while offset < len(source):
        length = struct.unpack(">I", source[offset : offset + 4])[0]
        kind = source[offset + 4 : offset + 8]
        payload = source[offset + 8 : offset + 8 + length]
        if kind == b"IDAT":
            payload += zlib.compress(b"second-stream")
        output += struct.pack(">I", len(payload)) + kind + payload
        output += struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        offset += 12 + length
    return bytes(output)


def web_evidence() -> dict[str, bytes]:
    baidu_home = snapshot("https://www.baidu.com/")
    baidu_home["dom"]["baiduKeyword"] = True
    baidu_home["dom"]["baiduSubmit"] = True
    baidu_search = snapshot("https://www.baidu.com/s?wd=Asterinas", tls=0)
    baidu_search["dom"]["baiduResults"] = 2
    bilibili_home = snapshot("https://www.bilibili.com/")
    bilibili_home["dom"]["bilibiliHome"] = True
    selected = "https://www.bilibili.com/video/BV1Ab411c7De/"
    bilibili_home["links"] = [selected]
    bilibili_detail = snapshot(selected, tls=0)
    bilibili_detail["dom"]["bilibiliDetail"] = True
    trust = (
        "FIREFOX_TRUST_PASS mode=embedded-xul ca_certificates=150 "
        "firefox=installed ca_package=installed riscv_elf=1 nss_loader=1\n"
    ).encode()
    trust_hash = hashlib.sha256(trust).hexdigest()
    system_ca = b"-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"
    system_ca_hash = hashlib.sha256(system_ca).hexdigest()
    values: dict[str, bytes] = {
        "baidu-home.json": (json.dumps(baidu_home) + "\n").encode(),
        "baidu-search.json": (json.dumps(baidu_search) + "\n").encode(),
        "bilibili-home.json": (json.dumps(bilibili_home) + "\n").encode(),
        "bilibili-detail.json": (json.dumps(bilibili_detail) + "\n").encode(),
        "curl.log": (
            "DNS host=www.baidu.com address=1.2.3.4\n"
            "DNS host=www.bilibili.com address=5.6.7.8\n"
            "HTTPS requested=https://www.baidu.com/ status=200 "
            "effective=https://www.baidu.com/ verify=0\n"
            "HTTPS requested=https://www.bilibili.com/ status=302 "
            "effective=https://www.bilibili.com/ verify=0\n"
        ).encode(),
        "security.log": (
            f"SYSTEM_CA_SHA256 sha256={system_ca_hash} path=/etc/ssl/certs/ca-certificates.crt\n"
            f"TRUST_STATIC_SHA256 sha256={trust_hash} path=/usr/share/asterinas/browser-web-trust-static.log\n"
            "BROWSER_WEB_SECURITY parent_pid=100 uid=1000 caps=zero nnp=1 sandbox_disable=absent\n"
            "BROWSER_WEB_SECURITY service_pid=100 nrestarts=0 stable=1 active=1\n"
            "BROWSER_WEB_SECURITY child_pid=101 role=content caps=zero nnp=1 seccomp=2\n"
            "BROWSER_WEB_SECURITY child_pid=102 role=socket caps=zero nnp=1 seccomp=2\n"
        ).encode(),
        "firefox-stderr.log": b"sandbox enabled\n",
        "firefox-mozilla.log": b"",
        "MarionetteActivePort": b"2828\n",
        "trust-static.log": trust,
        "ca-certificates.crt": system_ca,
    }
    for name in ("baidu-home", "baidu-search", "bilibili-home", "bilibili-detail"):
        values[f"{name}.png"] = png()
    assert set(values) == set(WEB_EVIDENCE_PATHS)
    return values


class BrowserWebContractTests(unittest.TestCase):
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
            "build_timestamp": "2026-08-28T00:00:00Z",
            "root_image_sha256": zero,
            "gate_packages": {name: "1" for name in profile.identity_packages},
        }

    def test_profile_uses_firefox_private_nss_and_embedded_xul_trust(self) -> None:
        profile = get_profile("browser-web")
        self.assertEqual(profile.schema_version, 7)
        self.assertEqual(profile.root_label, "ASTER_BROWSERWEB")
        for package in ("firefox-esr", "curl", "ca-certificates"):
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
            [str(ROOTFS / "build_rootfs.sh"), "--profile", "browser-web", "--print-packages"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertEqual(printed, list(profile.requested_packages))

    def test_schema_seven_manifest_load_mismatch_and_source_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            payload = self._schema7_payload()
            path.write_text(json.dumps(payload))
            manifest = load_manifest(path)
            self.assertEqual((manifest.schema_version, manifest.profile), (7, "browser-web"))
            self.assertEqual(manifest.downloaded_packages[0][4], "security")
            for mutation in ("schema6-browser-web", "schema7-browser-m5", "missing-source-role"):
                forged = copy.deepcopy(payload)
                if mutation == "schema6-browser-web":
                    forged["schema_version"] = 6
                elif mutation == "schema7-browser-m5":
                    forged["profile"] = "browser-m5"
                else:
                    forged["downloaded_packages"][0].pop("source_role")
                path.write_text(json.dumps(forged))
                with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                    load_manifest(path)

    @mock.patch("tools.riscv.debian.rootfs.contract._write_validated_manifest_atomically")
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
            checksums.write_text("".join(
                f"{n}\t{a}\t{v}\t{hashlib.sha256(n.encode()).hexdigest()}\t"
                f"{'security' if n == 'firefox-esr' else 'base'}\n"
                for n, a, v in rows
            ))
            inrelease = root / "legacy-InRelease"
            inrelease.write_bytes(b"legacy")
            sources = {role: root / f"{role}-InRelease" for role in ("base", "security")}
            for role, path in sources.items():
                path.write_bytes(role.encode())
            write_manifest(
                output=root / "manifest.json",
                image=image,
                packages_lock=package_lock,
                inrelease=inrelease,
                package_checksums=checksums,
                mirror_url=M5_SOURCES[0].mirror_url,
                suite="trixie",
                debian_release="13.6",
                build_timestamp="2026-08-28T00:00:00Z",
                tool_versions=("debootstrap=test",),
                profile_name="browser-web",
                signed_source_files=sources,
            )
        payload = json.loads(publish.call_args.args[1])
        self.assertEqual((payload["schema_version"], payload["profile"]), (7, "browser-web"))
        firefox = next(row for row in payload["downloaded_packages"] if row["name"] == "firefox-esr")
        self.assertEqual(firefox["source_role"], "security")

    def test_unit_launcher_and_evidence_preserve_normal_sandbox_and_tls(self) -> None:
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
        self.assertIn("https://www.baidu.com/", launcher)
        for required in (
            "nameserver[[:space:]]+10\\.0\\.2\\.3",
            "getent ahostsv4",
            "--proto '=https'",
            "--tlsv1.2",
            "%{ssl_verify_result}",
            "Seccomp:[[:space:]]*",
            "NoNewPrivs:[[:space:]]+1",
            "mode=embedded-xul",
        ):
            self.assertIn(required, evidence)
        self.assertNotIn("curl -k", evidence)
        self.assertNotIn("--insecure", evidence)
        self.assertGreaterEqual(evidence.count("--property NRestarts"), 2)
        self.assertIn("--property MainPID", evidence)
        self.assertIn("systemctl is-active --quiet", evidence)
        self.assertIn("firefox-pid-changed-during-gate", evidence)

    def test_m5_profile_keeps_formal_markers_without_debug_console_flood(self) -> None:
        builder = (ROOTFS / "build_rootfs.sh").read_text()
        offline_evidence = (ROOTFS / "desktop_m5_evidence.sh").read_text()
        online_evidence = (ROOTFS / "browser_web_evidence.sh").read_text()
        self.assertIn("desktop_standard_output=journal", builder)
        self.assertIn("desktop_standard_error=journal", builder)
        self.assertIn("StandardOutput=$desktop_standard_output", builder)
        self.assertIn("StandardError=$desktop_standard_error", builder)
        self.assertNotIn("systemd.log_level=debug", DESKTOP_M5_QEMU_BOOTARGS)
        for marker in (
            "DEBIAN_BROWSER_M5_READY",
            "DEBIAN_BROWSER_WEB_READY",
        ):
            with self.subTest(marker=marker):
                evidence = offline_evidence if "_M5_" in marker else online_evidence
                self.assertIn(marker, evidence)
        self.assertIn(
            'readonly CONSOLE="${ASTERINAS_BROWSER_WEB_CONSOLE:-/dev/console}"',
            online_evidence,
        )

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
        passing = ("\n".join(BROWSER_WEB_MILESTONES) + "\n").encode()
        self.assertTrue(classify_browser_web_qemu(passing, expected_debian_release="13.6").passed)
        for marker in (
            b"DEBIAN_BROWSER_WEB_FAIL reason=challenge",
            b"DEBIAN_NETWORK_M5_FAIL reason=qemu-https",
            *KERNEL_FATAL_MARKERS,
        ):
            with self.subTest(marker=marker):
                result = classify_browser_web_qemu(
                    passing + marker + b"\n", expected_debian_release="13.6"
                )
                self.assertFalse(result.passed)
        self.assertFalse(set(BROWSER_WEB_MILESTONES) & set(BROWSER_M5_MILESTONES))

    def test_baidu_search_is_submitted_from_live_homepage(self) -> None:
        client = mock.Mock()
        client.command.return_value = {"value": "search-click-scheduled"}
        _submit_baidu_search(client)
        command, arguments = client.command.call_args.args
        self.assertEqual(command, "WebDriver:ExecuteScript")
        self.assertIn("document.querySelector('#kw')", arguments["script"])
        self.assertIn("document.querySelector('#su')", arguments["script"])
        self.assertIn("submit.click()", arguments["script"])
        run_source = inspect.getsource(__import__(
            "tools.riscv.debian.rootfs.browser_web_marionette_gate",
            fromlist=["run_gate"],
        ).run_gate)
        self.assertIn("_submit_baidu_search(client)", run_source)
        self.assertNotIn("_navigate(client, BAIDU_SEARCH)", run_source)
        client.command.return_value = {"value": "missing-controls"}
        with self.assertRaisesRegex(GateError, "could not be submitted"):
            _submit_baidu_search(client)

    def test_post_stop_evidence_is_exact_and_rejects_combined_forgery(self) -> None:
        evidence = web_evidence()
        self.assertEqual(set(validate_web_evidence(evidence)), set(WEB_EVIDENCE_PATHS))
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
        for name in ("baidu-home", "baidu-search", "bilibili-home", "bilibili-detail"):
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

        trust_digest = hashlib.sha256(evidence["trust-static.log"]).hexdigest().encode()
        trust_mismatch = evidence["security.log"].replace(trust_digest, b"0" * 64)
        with self.assertRaises(GateFailure):
            validate_web_evidence({**evidence, "security.log": trust_mismatch})

        duplicate_ca = evidence["security.log"].replace(
            b"TRUST_STATIC_SHA256", b"SYSTEM_CA_SHA256"
        ).replace(
            b"path=/usr/share/asterinas/browser-web-trust-static.log",
            b"path=/etc/ssl/certs/ca-certificates.crt",
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
            ("firefox-stderr.log", b"Exiting due to channel error.\n"),
            (
                "firefox-mozilla.log",
                b"EPERM SCM_RIGHTS contains a file container\n",
            ),
        ):
            with self.subTest(log_name=log_name), self.assertRaises(GateFailure):
                validate_web_evidence({**evidence, log_name: log})

        for changed in (
            b"BROWSER_WEB_SECURITY service_pid=999 nrestarts=0 stable=1 active=1",
            b"BROWSER_WEB_SECURITY service_pid=100 nrestarts=1 stable=1 active=1",
        ):
            original = b"BROWSER_WEB_SECURITY service_pid=100 nrestarts=0 stable=1 active=1"
            with self.subTest(changed=changed), self.assertRaises(GateFailure):
                validate_web_evidence(
                    {
                        **evidence,
                        "security.log": evidence["security.log"].replace(original, changed),
                    }
                )

    def test_online_root_checker_rejects_non_slirp_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (
                "var/lib/dpkg/status", "usr/bin/getent", "usr/bin/curl",
                "usr/lib/firefox-esr/firefox-esr", "etc/nsswitch.conf", "etc/resolv.conf",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
                path.chmod(0o755)
            (root / "var/lib/dpkg/status").write_text("".join(
                f"Package: {package}\nStatus: install ok installed\n\n"
                for package in ("firefox-esr", "ca-certificates", "curl")
            ))
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
            "https://www.bilibili.com/video/BV1Ab411c7De/",
        ]
        selected = select_bilibili_video(home)
        self.assertEqual(selected, "https://www.bilibili.com/video/BV1Ab411c7De/")
        detail = snapshot(selected, tls=0)
        detail["dom"]["bilibiliDetail"] = True
        validate_bilibili_detail(detail, selected)
        detail["url"] = "https://www.bilibili.com/video/BV9OtherId/"
        with self.assertRaisesRegex(GateError, "selected live BV"):
            validate_bilibili_detail(detail, selected)

    def test_challenge_403_snapshot_always_fails(self) -> None:
        challenged = snapshot("https://www.baidu.com/")
        challenged["dom"]["baiduKeyword"] = True
        challenged["dom"]["baiduSubmit"] = True
        challenged["title"] = "403 Forbidden"
        challenged["bodyText"] = "安全验证 challenge access denied"
        with self.assertRaisesRegex(GateError, "403"):
            validate_baidu_home(challenged)

    def test_gate_explicitly_disables_insecure_certs_and_records_evidence(self) -> None:
        gate = (ROOTFS / "browser_web_marionette_gate.py").read_text()
        self.assertIn('"acceptInsecureCerts": False', gate)
        self.assertIn('capabilities.get("acceptInsecureCerts") is not False', gate)
        self.assertIn("WebDriver:TakeScreenshot", gate)
        self.assertIn("ResourceTiming", gate)
        self.assertIn("NavigationTiming", gate)
        for forbidden in ("mock", "snapshot.html", "host proxy", "acceptInsecureCerts\": True"):
            self.assertNotIn(forbidden, gate)


if __name__ == "__main__":
    unittest.main()
