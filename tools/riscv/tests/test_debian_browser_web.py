#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import copy
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.browser_m5_qemu_gate import BROWSER_M5_MILESTONES
from tools.riscv.debian.rootfs.browser_web_marionette_gate import (
    GateError,
    select_bilibili_video,
    validate_baidu_home,
    validate_baidu_search,
    validate_bilibili_detail,
)
from tools.riscv.debian.rootfs.browser_web_qemu_gate import (
    BROWSER_WEB_MILESTONES,
    BrowserWebQemuOperations,
    browser_web_qemu_argv,
    classify_browser_web_qemu,
)
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


class BrowserWebContractTests(unittest.TestCase):
    def test_profile_is_independent_and_packages_system_nss_trust(self) -> None:
        profile = get_profile("browser-web")
        self.assertEqual(profile.schema_version, 7)
        self.assertEqual(profile.root_label, "ASTER_BROWSERWEB")
        for package in (
            "firefox-esr", "curl", "ca-certificates", "libnss3",
            "libnss3-tools", "p11-kit", "p11-kit-modules",
        ):
            self.assertIn(package, profile.requested_packages)
            self.assertIn(package, profile.identity_packages)
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
            "/usr/lib/riscv64-linux-gnu/libnssckbi.so",
            "/usr/lib/riscv64-linux-gnu/pkcs11/p11-kit-trust.so",
            "Seccomp:[[:space:]]+2",
            "NoNewPrivs:[[:space:]]+1",
        ):
            self.assertIn(required, evidence)
        self.assertNotIn("curl -k", evidence)
        self.assertNotIn("--insecure", evidence)

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
        passing = ("\n".join(BROWSER_WEB_MILESTONES) + "\n").encode()
        self.assertTrue(classify_browser_web_qemu(passing, expected_debian_release="13.6").passed)
        for marker in (
            b"DEBIAN_BROWSER_WEB_FAIL reason=challenge",
            b"DEBIAN_NETWORK_M5_FAIL reason=qemu-https",
        ):
            with self.subTest(marker=marker):
                result = classify_browser_web_qemu(
                    passing + marker + b"\n", expected_debian_release="13.6"
                )
                self.assertFalse(result.passed)
        self.assertFalse(set(BROWSER_WEB_MILESTONES) & set(BROWSER_M5_MILESTONES))

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
