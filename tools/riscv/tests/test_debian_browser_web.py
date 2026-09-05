#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
import zlib

from tools.riscv.debian.rootfs.browser_m5_qemu_gate import BROWSER_M5_MILESTONES
from tools.riscv.debian.rootfs.browser_web_marionette_gate import (
    GateError,
    _navigate,
    _clear_document,
    _probe,
    _submit_fixture_search,
    _trigger_fixture_download,
    _start_fixture_capabilities,
    _submit_baidu_search,
    _script_value,
    _wait_for_fixture_download,
    _wait_for_probe,
    _wait_across_windows,
    _wait_baidu_search_outcome,
    _probe_mapping,
    probe_baidu_home,
    probe_baidu_search,
    probe_about_blank,
    probe_fixture_search,
    probe_fixture_home,
    probe_fixture_capabilities,
    select_bilibili_video,
    validate_gecko_profiler_environment,
    validate_baidu_home,
    validate_baidu_search,
    validate_baidu_challenge,
    validate_baidu_search_outcome,
    validate_bilibili_detail,
    validate_fixture_search,
    fixture_index_url_from_environment,
)
from tools.riscv.debian.rootfs.browser_web_online_rootfs_check import (
    CheckFailure as OnlineCheckFailure,
    check_root as check_online_root,
)
from tools.riscv.debian.rootfs.browser_web_trust_check import (
    OVERLAY_COMMIT,
    OVERLAY_PACKAGES,
    OVERLAY_RUNTIME_PATHS,
    check_root as check_trust_root,
)
from tools.riscv.debian.rootfs.firefox_jit_overlay import (
    OverlayError,
    install as install_firefox_jit_overlay,
)
from tools.riscv.debian.rootfs.browser_web_qemu_gate import (
    BROWSER_WEB_MILESTONES,
    BrowserWebQemuOperations,
    KERNEL_FATAL_MARKERS,
    WEB_EVIDENCE_PATHS,
    _extract_web_evidence,
    _diagnostic_gdb_port,
    _EXTERNAL_BLOCK,
    browser_web_qemu_argv,
    browser_web_milestones,
    classify_browser_web_qemu,
    firefox_ready_marker,
    validate_uploaded_baidu_screenshot,
    validate_web_evidence,
)
from tools.riscv.debian.rootfs import browser_startup_cache_check as cache_check
from tools.riscv.debian.rootfs.contract import (
    ContractError,
    _gate_versions,
    load_manifest,
    write_manifest,
)
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure
from tools.riscv.debian.rootfs.signed_sources import M5_SOURCES
from tools.riscv.debian.rootfs.desktop_m5_network_gate import NetworkMode
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
        "browserCapabilities": None,
        "dom": {
            "baiduLogo": False,
            "baiduKeyword": False,
            "baiduSubmit": False,
            "baiduResults": 0,
            "bilibiliHome": False,
            "bilibiliDetail": False,
            "fixtureQuery": False,
            "fixtureImage": False,
            "fixtureSecond": False,
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


def fixture_capabilities(phase: str) -> dict[str, object]:
    return {
        "version": 1,
        "phase": phase,
        "state": "complete",
        "checks": {
            name: True
            for name in (
                "audio",
                "canvas",
                "cookie",
                "fetch",
                "indexedDb",
                "localStorage",
                "sessionStorage",
                "wasm",
                "worker",
            )
        },
        "error": None,
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
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
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
    baidu_home["dom"]["baiduLogo"] = True
    baidu_search = snapshot("https://www.baidu.com/s?wd=Asterinas", tls=0)
    baidu_search["dom"]["baiduResults"] = 2
    fixture_url = (
        "http://10.0.2.2:17894/browser-quality/index.html?q=asterinas"
    )
    fixture_search = snapshot(fixture_url, tls=0)
    fixture_search["title"] = "asterinas - Asterinas Browser Quality"
    fixture_search["bodyText"] = (
        "Asterinas browser quality / 浏览器质量 Search Second page Download"
    )
    fixture_search["dom"]["fixtureQuery"] = True
    fixture_search["dom"]["fixtureImage"] = True
    fixture_search["dom"]["fixtureSecond"] = True
    fixture_search["browserCapabilities"] = fixture_capabilities("search")
    fixture_search["resources"] = [
        {
            "name": "http://10.0.2.2:17894/browser-quality/pattern.png",
            "initiatorType": "img",
            "duration": 1,
            "transferSize": 123,
        }
    ]
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
        "fixture-search.json": (json.dumps(fixture_search) + "\n").encode(),
        "fixture-download.json": (
            json.dumps(
                {
                    "bytes": 256 * 1024,
                    "filename": "asterinas-browser-quality.bin",
                    "sha256": (
                        "2312394bd99545d9de131c24efb781e765ac1aec243f2ed9347597a793a415e9"
                    ),
                }
            )
            + "\n"
        ).encode(),
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
            "BROWSER_WEB_NETWORK_ENV parent_pid=100 mode=direct\n"
            "BROWSER_WEB_SECURITY service_pid=100 nrestarts=0 stable=1 active=1\n"
            "BROWSER_WEB_SECURITY child_pid=101 role=content caps=zero nnp=1 seccomp=2\n"
            "BROWSER_WEB_SECURITY child_pid=102 role=socket caps=zero nnp=1 seccomp=2\n"
        ).encode(),
        "firefox-stderr.log": b"sandbox enabled\n",
        "firefox-mozilla.log": b"",
        "MarionetteActivePort": b"2828\n",
        "trust-static.log": trust,
        "ca-certificates.crt": system_ca,
        "timeline.log": (
            "A_WEB_TIMELINE marker=BOOT_SYSTEMD_BEGIN guest_monotonic_ns=100 firefox_pid=0\n"
            "A_WEB_TIMELINE marker=BOOT_BASIC_TARGET guest_monotonic_ns=200 firefox_pid=0\n"
            "A_WEB_TIMELINE marker=BOOT_NETWORK_READY guest_monotonic_ns=300 firefox_pid=0\n"
            "A_WEB_TIMELINE marker=BOOT_X_SOCKET_READY guest_monotonic_ns=400 firefox_pid=0\n"
            "A_WEB_TIMELINE marker=BOOT_FIREFOX_WRAPPER_START guest_monotonic_ns=500 firefox_pid=100\n"
            "A_WEB_TIMELINE marker=BOOT_FIREFOX_EXEC guest_monotonic_ns=600 firefox_pid=100\n"
            "A_WEB_TIMELINE marker=BOOT_MARIONETTE_PORT_READY guest_monotonic_ns=700 firefox_pid=100\n"
            "A_WEB_TIMELINE marker=BOOT_MARIONETTE_CONNECTED guest_monotonic_ns=800 firefox_pid=100\n"
            "A_WEB_TIMELINE marker=BOOT_NEW_SESSION_DONE guest_monotonic_ns=900 firefox_pid=100\n"
            "A_WEB_TIMELINE marker=BOOT_FIRST_WINDOW_READY guest_monotonic_ns=1000 firefox_pid=100\n"
            "A_WEB_TIMELINE marker=BOOT_DOM_READY guest_monotonic_ns=1100 firefox_pid=100 page=baidu-home\n"
            "A_WEB_TIMELINE marker=BOOT_DOM_READY guest_monotonic_ns=1200 firefox_pid=100 page=fixture-search\n"
            "A_WEB_TIMELINE marker=BOOT_DOM_READY guest_monotonic_ns=1300 firefox_pid=100 page=bilibili-home\n"
            "A_WEB_TIMELINE marker=BOOT_DOM_READY guest_monotonic_ns=1400 firefox_pid=100 page=bilibili-detail\n"
            "A_WEB_TIMELINE marker=BOOT_DOM_READY guest_monotonic_ns=1500 firefox_pid=100 page=baidu-search\n"
            "A_WEB_PHASE phase=tcp-connect state=start firefox_pid=100\n"
            "A_WEB_PHASE phase=tcp-connect state=done firefox_pid=100\n"
            "A_WEB_SELECTED_BV url=https://www.bilibili.com/video/BV1Ab411c7De/?track_id=\n"
            "DEBIAN_BROWSER_WEB_PLATFORM_READY baidu_home=pass bilibili_home=pass "
            "bilibili_detail=pass bv=BV1Ab411c7De tls=verified\n"
        ).encode(),
        "firefox-user.js": (
            'user_pref("browser.download.folderList", 2);\n'
            'user_pref("network.proxy.type", 0);\n'
        ).encode(),
    }
    for name in (
        "baidu-home",
        "baidu-search",
        "fixture-search",
        "bilibili-home",
        "bilibili-detail",
    ):
        values[f"{name}.png"] = png()
    assert set(values) == set(WEB_EVIDENCE_PATHS)
    return values


def proxy_web_evidence() -> dict[str, bytes]:
    evidence = web_evidence()
    evidence["curl.log"] = (
        "DNS_DELEGATED mode=proxy host=www.baidu.com proxy=http://10.0.2.2:17893\n"
        "DNS_DELEGATED mode=proxy host=www.bilibili.com proxy=http://10.0.2.2:17893\n"
        "HTTPS requested=https://www.baidu.com/ status=200 "
        "effective=https://www.baidu.com/ verify=0\n"
        "HTTPS requested=https://www.bilibili.com/ status=302 "
        "effective=https://www.bilibili.com/ verify=0\n"
    ).encode()
    evidence["security.log"] = evidence["security.log"].replace(
        b"mode=direct", b"mode=proxy"
    )
    evidence["firefox-user.js"] = (
        'user_pref("browser.download.folderList", 2);\n'
        'user_pref("network.proxy.type", 1);\n'
        'user_pref("network.proxy.http", "10.0.2.2");\n'
        'user_pref("network.proxy.http_port", 17893);\n'
        'user_pref("network.proxy.ssl", "10.0.2.2");\n'
        'user_pref("network.proxy.ssl_port", 17893);\n'
        'user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1, 10.0.2.2");\n'
    ).encode()
    return evidence


class BrowserWebContractTests(unittest.TestCase):
    _BACKGROUND_STARTUP_PREFERENCES = {
        'user_pref("browser.newtabpage.enabled", false);',
        'user_pref("browser.pagethumbnails.capturing_disabled", true);',
        'user_pref("browser.region.network.url", "");',
        'user_pref("browser.topsites.contile.enabled", false);',
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
            profile = (
                home / ".mozilla/asterinas-browser-web/user.js"
            ).read_text(encoding="utf-8")
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
                oct((home / ".mozilla/asterinas-browser-web/user.js").stat().st_mode & 0o777),
                "0o600",
            )
            self.assertEqual(
                list((home / ".mozilla/asterinas-browser-web").glob("user.js.tmp.*")),
                [],
            )
            launcher = (ROOTFS / "browser_web_firefox.sh").read_text()
            self.assertIn(
                "export ASTERINAS_FIREFOX_WEB_NETWORK_MODE=\"$NETWORK_MODE\"",
                launcher,
            )
            self.assertIn(
                "unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY",
                launcher,
            )

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
            profile = (
                home / ".mozilla/asterinas-browser-web/user.js"
            ).read_text(encoding="utf-8")
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
                        (
                            home / ".mozilla/asterinas-browser-web/user.js"
                        ).read_text(encoding="utf-8"),
                        profile,
                    )

    def test_firefox_basic_profile_disables_public_background_fetchers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            result = self._prepare_firefox_profile(
                home, mode="direct", basic_only=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            profile = (
                home / ".mozilla/asterinas-browser-web/user.js"
            ).read_text(encoding="utf-8")
            for preference in (
                'user_pref("app.update.enabled", false);',
                'user_pref("browser.safebrowsing.downloads.remote.enabled", false);',
                'user_pref("extensions.update.enabled", false);',
                'user_pref("network.trr.mode", 5);',
                'user_pref("services.settings.server", "");',
            ):
                self.assertIn(preference, profile)

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
                proxy.replace(
                    b"DEBIAN_WEB_NETWORK_READY mode=proxy layers=10\n", b""
                ),
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
        probe = {name: source[name] for name in (
            "url", "title", "readyState", "bodyText", "jsComplete",
            "browserCapabilities", "dom",
        )}
        probe_fixture_capabilities(probe, url)

    def test_readiness_probe_accepts_optional_capability_types(self) -> None:
        source = snapshot("https://www.baidu.com/")
        source["dom"]["baiduKeyword"] = True
        source["dom"]["baiduSubmit"] = True
        source["dom"]["baiduLogo"] = True
        probe = {name: source[name] for name in (
            "url", "title", "readyState", "bodyText", "jsComplete",
            "browserCapabilities", "dom",
        )}
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
        self.assertEqual(profile.root_size_bytes, 2 * 1024 * 1024 * 1024)
        for package in (
            "firefox-esr",
            "python3-minimal",
            "ca-certificates",
            "curl",
            "iproute2",
            "iputils-ping",
            "xdotool",
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
            "desktop_m5_network_evidence.sh",
            "desktop_m5_network_gate.py",
            "browser_web_firefox.sh",
            "browser_web_marionette_gate.py",
            "browser_m5_marionette_gate.py",
            "browser_web_evidence.sh",
            "browser_web.service",
            "browser_web_evidence.service",
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

    def test_browser_startup_orders_after_network_clock_sync(self) -> None:
        service = (ROOTFS / "browser_web_evidence.service").read_text()
        self.assertIn(
            "Wants=network-online.target asterinas-desktop-m5.service", service
        )
        self.assertIn(
            "After=network-online.target asterinas-desktop-m5.service", service
        )
        self.assertNotIn("Requires=network-online.target", service)
        self.assertNotIn("Requires=asterinas-desktop-m5.service", service)
        self.assertNotIn("Environment=ASTERINAS_WEB_NETWORK_MODE=", service)
        self.assertNotIn("Environment=ASTERINAS_DESKTOP_PROXY", service)
        browser_service = (ROOTFS / "browser_web.service").read_text()
        self.assertIn("Requires=asterinas-desktop-m5-network.service", browser_service)
        self.assertIn("After=asterinas-desktop-m5-network.service", browser_service)

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
                "schema7-browser-m5",
                "missing-source-role",
                "missing-runtime-digest",
                "malformed-runtime-digest",
            ):
                forged = copy.deepcopy(payload)
                if mutation == "schema6-browser-web":
                    forged["schema_version"] = 6
                elif mutation == "schema7-browser-m5":
                    forged["profile"] = "browser-m5"
                elif mutation == "missing-runtime-digest":
                    forged["tool_versions"].pop("browser-web-runtime")
                elif mutation == "malformed-runtime-digest":
                    forged["tool_versions"]["browser-web-runtime"] = "not-a-digest"
                else:
                    forged["downloaded_packages"][0].pop("source_role")
                path.write_text(json.dumps(forged))
                with self.subTest(mutation=mutation), self.assertRaises(ContractError):
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
                mirror_url=M5_SOURCES[0].mirror_url,
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
        self.assertIn('about:blank', launcher)
        self.assertIn('ASTERINAS_FIREFOX_WEB_TARGET_URL', launcher)
        self.assertIn('Environment=ASTERINAS_FIREFOX_WEB_TARGET_URL=https://www.baidu.com/', unit)
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
        self.assertIn("DEBIAN_BROWSER_WEB_EXTERNAL_BLOCK site=baidu reason=captcha", evidence)
        self.assertIn("unavailable-firefox-riscv64-build", evidence)
        self.assertIn('emit "DEBIAN_BROWSER_WEB_FAIL reason=browser-content"', evidence)
        self.assertLess(
            evidence.index('emit "DEBIAN_BROWSER_WEB_FAIL reason=browser-content"'),
            evidence.index('/usr/bin/timeout 20 /usr/bin/sync || true'),
        )
        self.assertIn("/usr/bin/timeout 20 /usr/bin/sync || fail evidence-sync", evidence)
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
            "DEBIAN_FIREFOX_BAIDU_READY",
        ):
            with self.subTest(marker=marker):
                evidence = offline_evidence if "_M5_" in marker else online_evidence
                self.assertIn(marker, evidence)
        self.assertIn(
            'readonly CONSOLE="${ASTERINAS_BROWSER_WEB_CONSOLE:-/dev/console}"',
            online_evidence,
        )

    def test_startup_timeline_requires_ordered_guest_monotonic_phases(self) -> None:
        evidence = web_evidence()
        validate_web_evidence(evidence)
        self.assertEqual(evidence["timeline.log"].count(b"A_WEB_TIMELINE marker="), 15)
        unit = (ROOTFS / "browser_web.service").read_text()
        builder = (ROOTFS / "build_rootfs.sh").read_text()
        self.assertNotIn("ExecStartPre=/usr/lib/asterinas/browser-web-timeline wait-x", unit)
        self.assertIn(
            "ExecStartPre=+/usr/lib/asterinas/desktop-m5-device-access", unit
        )
        self.assertIn("/usr/lib/asterinas/browser-web-timeline wait-x", (ROOTFS / "browser_web_firefox.sh").read_text())
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
        self.assertIn("basic.target.wants/asterinas-browser-web-timeline-basic.service", builder)
        self.assertIn('desktop_after="local-fs.target dbus.service"', builder)
        self.assertIn("desktop_session_options=$'TTYPath=/dev/tty1", builder)
        self.assertIn("StandardInput=tty", builder)
        self.assertIn("Environment=ASTERINAS_BROWSER_WEB_SESSION=1", builder)
        self.assertIn("SupplementaryGroups=video input tty", builder)
        self.assertIn("desktop_user=root", builder)
        desktop_session = (ROOTFS / "desktop_m5_session.sh").read_text()
        self.assertIn('-logfile "$HOME/Xorg.0.log" vt1', desktop_session)
        self.assertIn("-novtswitch -keeptty", desktop_session)
        self.assertIn("runuser --user asterinas", desktop_session)
        self.assertIn('/usr/bin/tail -n 0 -f "$HOME/Xorg.0.log" >&2', desktop_session)
        self.assertIn('/usr/bin/rm -f -- /tmp/.X11-unix/X0', desktop_session)
        self.assertIn('/usr/bin/timeout 5 /usr/bin/xdpyinfo -display "$DISPLAY"', desktop_session)
        self.assertIn('firefox-web-stderr.log', (ROOTFS / "browser_web_firefox.sh").read_text())
        launcher = (ROOTFS / "browser_web_firefox.sh").read_text()
        self.assertIn("ASTERINAS_FIREFOX_PS_DIAGNOSTIC", launcher)
        self.assertIn("/usr/bin/timeout 5 /usr/bin/ps", launcher)
        self.assertIn("/usr/bin/timeout 12 /usr/bin/sleep 10", launcher)
        self.assertIn("ASTERINAS_FIREFOX_PREWARM", launcher)
        self.assertIn('FIREFOX_LIBRARY_DIR=/usr/lib/firefox-esr', launcher)
        self.assertIn('FIREFOX_LIBRARY_DIR=/usr/lib/firefox', launcher)
        self.assertIn('exec "$FIREFOX_BIN"', launcher)
        self.assertIn('"$FIREFOX_HOME/Downloads"', launcher)
        self.assertIn('browser.download.useDownloadDir', launcher)
        self.assertIn('browser.helperApps.neverAsk.saveToDisk', launcher)
        self.assertIn('</proc/uptime', launcher)
        self.assertNotIn('EPOCHREALTIME', launcher)
        self.assertIn('browser-web-firefox.pid', builder)
        self.assertIn('asterinas-browser-web"', builder)
        self.assertIn("BROWSER_WEB_DESKTOP_STAGE=device-access-start", (ROOTFS / "desktop_m3_device_access.sh").read_text())
        device_access = (ROOTFS / "desktop_m3_device_access.sh").read_text()
        self.assertIn("device_deadline=$((SECONDS + 120))", device_access)
        self.assertIn("/usr/bin/sleep 1", device_access)
        self.assertIn("while [[ ! -c /dev/fb0 ]]", device_access)
        self.assertIn("input-devices-absent", device_access)
        self.assertIn("device-access-failed reason=fb0-timeout", device_access)
        self.assertIn("device-access-failed reason=fb0-permissions", device_access)
        self.assertIn("BROWSER_WEB_DESKTOP_STAGE=fb0-ready", device_access)
        self.assertIn('"$stage/etc/systemd/system/systemd-udevd.service"', builder)
        self.assertIn('"$stage/etc/systemd/system/systemd-logind.service"', builder)
        self.assertIn('configure_desktop_m5_network "$stage" m5 false lightweight', builder)
        self.assertNotIn("2> >(tee", (ROOTFS / "browser_web_evidence.sh").read_text())
        begin_unit = (ROOTFS / "browser_web_timeline_begin.service").read_text()
        basic_unit = (ROOTFS / "browser_web_timeline_basic.service").read_text()
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
        diagnostics = (
            b'A_WEB_PROBE_COMMAND state=start\n'
            b'A_WEB_PROBE_RETRY error="document not ready"\n'
            b'A_WEB_PROBE_CAPABILITIES state=running error="None" '
            b'checks={"canvas":true,"wasm":true}\n'
            b'A_WEB_PROBE_COMMAND state=done\n'
        )
        validate_web_evidence(
            {
                **evidence,
                "timeline.log": diagnostics + evidence["timeline.log"],
            }
        )
        with self.assertRaisesRegex(GateFailure, "invalid record"):
            validate_web_evidence(
                {
                    **evidence,
                    "timeline.log": (
                        b'A_WEB_PROBE_RETRY error="unterminated\n'
                        + evidence["timeline.log"]
                    ),
                }
            )
        with self.assertRaisesRegex(GateFailure, "invalid record"):
            validate_web_evidence(
                {
                    **evidence,
                    "timeline.log": (
                        b'A_WEB_PROBE_CAPABILITIES state=running error="None" '
                        b'checks={"wasm":"yes"}\n'
                        + evidence["timeline.log"]
                    ),
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
            ': >"$stage/etc/.updated"',
            ': >"$stage/var/.updated"',
        ):
            self.assertIn(command, builder)
        cache_function = builder.split("finalize_browser_startup_caches()", 1)[1].split(
            "configure_desktop_m5_network()", 1
        )[0]
        self.assertNotIn('systemd-sysusers --root', cache_function)
        self.assertNotIn('journalctl --root', cache_function)
        self.assertNotIn("|| true", cache_function)
        self.assertNotIn("systemctl mask", cache_function)
        self.assertNotIn("/dev/null", cache_function)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (
                "etc/systemd/system",
                "usr/share/asterinas",
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
            (root / "var/cache/fontconfig/fixture.cache-9").write_bytes(
                b"\x04\xfc\x02\xfc" + b"\0" * 28
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

                unit_contents = unit.read_text()
                unit.unlink()
                self.assertEqual(
                    cache_check.check_cache_profile(root, profile="desktop-m5-network"),
                    "DESKTOP_STARTUP_CACHE_PASS profile=desktop-m5-network "
                    "sysusers=static ldconfig=riscv64 journal=catalog "
                    "fontconfig=cached stamps=current",
                )
                unit.write_text(unit_contents)

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
                font.write_bytes(b"\x04\xfc\x02\xfc" + b"\0" * 28)
                font.write_bytes(b"arbitrary-font-bytes")
                with self.assertRaisesRegex(cache_check.CacheCheckError, "fontconfig"):
                    cache_check.check_cache_profile(root)
                font.write_bytes(b"\x04\xfc\x02\xfc" + b"\0" * 28)

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
attempt_file="$2/attempts"
scenario="$3"
printf '0\n' >"$attempt_file"
export SOURCE_DATE_EPOCH=1704067200
chroot() {
    current="$(cat "$attempt_file")"
    current="$((current + 1))"
    printf '%s\n' "$current" >"$attempt_file"
    if [[ "$scenario" == success && -z "${SOURCE_DATE_EPOCH-}" && " $* " == *" -v "* ]]; then
        printf 'cache\n' >"$1/var/cache/fontconfig/retry.cache-9"
    fi
    return 0
}
generate_fontconfig_cache "$stage" "$3"
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for scenario, expected_status, expected_attempts in (
                ("success", 0, "1"),
                ("empty", 2, "1"),
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

    def test_desktop_network_profile_requires_prebuilt_startup_caches(self) -> None:
        builder = ROOTFS / "build_rootfs.sh"
        tools = subprocess.run(
            [str(builder), "--profile", "desktop-m5-network", "--print-tools"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertNotIn("systemd-sysusers", tools)
        self.assertNotIn("journalctl", tools)
        for profile, expected in (
            ("desktop-m5-network", 0),
            ("browser-web", 0),
            ("minimal-m1", 1),
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
            b"DEBIAN_NETWORK_M5_FAIL reason=qemu-https",
            b"DEBIAN_WEB_NETWORK_FAIL mode=direct layer=baidu-asset reason=dns",
            *KERNEL_FATAL_MARKERS,
        ):
            with self.subTest(marker=marker):
                result = classify_browser_web_qemu(
                    passing + marker + b"\n", expected_debian_release="13.6"
                )
                self.assertFalse(result.passed)
        self.assertFalse(set(BROWSER_WEB_MILESTONES) & set(BROWSER_M5_MILESTONES))

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

    def test_basic_capability_runner_is_started_explicitly(self) -> None:
        client = mock.Mock()
        client.command.return_value = {"value": "basic-capabilities-started"}
        _start_fixture_capabilities(client)
        command, arguments = client.command.call_args.args
        self.assertEqual(command, "WebDriver:ExecuteScript")
        self.assertIn("asterinas-basic-capabilities-start", arguments["script"])

    def test_basic_capability_runner_accepts_firefox_null_dispatch_result(self) -> None:
        client = mock.Mock()
        client.command.return_value = {"value": None}
        _start_fixture_capabilities(client)

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
            with self.subTest(forged=forged), mock.patch.dict(
                os.environ, {"ASTERINAS_DESKTOP_FIXTURE_URL": forged}, clear=True
            ), self.assertRaisesRegex(GateError, "fixture URL"):
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
            if key in {
                "url", "title", "readyState", "bodyText", "jsComplete",
                "browserCapabilities", "dom",
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
            if key in {
                "url", "title", "readyState", "bodyText", "jsComplete",
                "browserCapabilities", "dom",
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
        self.assertLess(
            gate.index('"navigate-fixture-home"'),
            gate.index('"navigate-baidu-home"'),
        )

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
            if key in {
                "url", "title", "readyState", "bodyText", "jsComplete",
                "browserCapabilities", "dom",
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

    def test_marionette_wait_recovers_about_blank_once(self) -> None:
        ready = snapshot("https://www.baidu.com/")
        ready["dom"]["baiduKeyword"] = True
        ready["dom"]["baiduSubmit"] = True
        ready["dom"]["baiduLogo"] = True
        fields = {
            "url", "title", "readyState", "bodyText", "jsComplete",
            "browserCapabilities", "dom",
        }
        about_blank = snapshot("about:blank")
        client = mock.Mock()
        client.command.side_effect = [
            {"value": json.dumps({key: about_blank[key] for key in fields})},
            {"value": json.dumps({key: ready[key] for key in fields})},
        ]
        recover = mock.Mock()
        with mock.patch(
            "tools.riscv.debian.rootfs.browser_web_marionette_gate.time.sleep"
        ):
            observed, result = _wait_for_probe(
                client,
                probe_baidu_home,
                time.monotonic() + 5,
                recover=recover,
            )
        self.assertEqual(observed["url"], "https://www.baidu.com/")
        self.assertIsNone(result)
        recover.assert_called_once()
        self.assertEqual(client.command.call_count, 2)

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
            "url", "title", "readyState", "bodyText", "jsComplete",
            "browserCapabilities", "dom",
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
                client, lambda probe: probe_fixture_home(probe, url),
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
            if key in {
                "url", "title", "readyState", "bodyText", "jsComplete",
                "browserCapabilities", "dom",
            }
        }
        search_probe = {
            key: value
            for key, value in search.items()
            if key in {
                "url", "title", "readyState", "bodyText", "jsComplete",
                "browserCapabilities", "dom",
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
            if key in {
                "url", "title", "readyState", "bodyText", "jsComplete",
                "browserCapabilities", "dom",
            }
        }
        client = mock.Mock()
        client.command.side_effect = [
            {"value": ["challenge-window"]},
            {"value": None},
            {"value": json.dumps(probe)},
            {"value": json.dumps(challenge)},
        ]
        observed, outcome = _wait_baidu_search_outcome(
            client, time.monotonic() + 5
        )
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
            if key in {
                "url", "title", "readyState", "bodyText", "jsComplete",
                "browserCapabilities", "dom",
            }
        }
        client.command.return_value = {"value": json.dumps(probe)}
        self.assertEqual(_probe(client), probe)
        script = client.command.call_args.args[1]["script"]
        self.assertNotIn("innerText", script)
        self.assertNotIn("getBoundingClientRect", script)
        self.assertNotIn("getEntriesByType", script)
        self.assertNotIn("querySelectorAll('a[href*=", script)

    def test_lightweight_public_probe_avoids_body_enumeration(self) -> None:
        client = mock.Mock()
        probe = snapshot("https://www.bilibili.com/")
        probe["bodyText"] = "Bilibili https://www.bilibili.com/"
        probe["dom"]["bilibiliHome"] = True
        fields = {
            "url", "title", "readyState", "bodyText", "jsComplete",
            "browserCapabilities", "dom",
        }
        client.command.return_value = {
            "value": json.dumps({key: probe[key] for key in fields})
        }
        self.assertEqual(_probe(client, lightweight=True)["url"], probe["url"])
        script = client.command.call_args.args[1]["script"]
        self.assertIn("document.title || ''", script)
        self.assertNotIn("document.body.textContent", script)
        self.assertNotIn("Array.from(document.querySelectorAll", script)

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
        self.assertIn('/usr/bin/timeout 10 /usr/bin/head -n "$HOT_MAP_MAX_LINES"', evidence)
        self.assertIn('"$PROC_ROOT/$hot_pid/maps"', evidence)
        sampler = evidence.split("start_gate_sampler()", 1)[1].split(
            "capture_gecko_profile()", 1
        )[0]
        self.assertNotIn('"$process/cmdline"', sampler)
        self.assertIn("/usr/bin/sync", evidence)
        self.assertNotIn("ASTERINAS_FIREFOX_GECKO_PROFILE", service)
        self.assertIn("Environment=ASTERINAS_FIREFOX_GECKO_PROFILE=1", diagnostic)
        self.assertIn("Environment=MOZ_PROFILER_STARTUP=1", diagnostic)
        self.assertIn(
            "Environment=MOZ_PROFILER_STARTUP_INTERVAL=10", diagnostic
        )

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
        for invalid in (b"seccomp=1", b"seccomp=0\nBROWSER_WEB_SECURITY child_pid=103 role=content caps=zero nnp=1 seccomp=2"):
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
        proxy_no_timing = dict(proxy)
        proxy_home = json.loads(proxy_no_timing["baidu-home.json"])
        proxy_home["navigation"]["secureConnectionStart"] = 0
        proxy_no_timing["baidu-home.json"] = (
            json.dumps(proxy_home) + "\n"
        ).encode()
        validate_web_evidence(proxy_no_timing, network_mode=NetworkMode.PROXY)
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
            validate_uploaded_baidu_screenshot(
                {**summary, "sha256": "0" * 64}, payload
            )
        with self.assertRaisesRegex(GateFailure, "upload evidence"):
            validate_uploaded_baidu_screenshot(
                summary, payload, expected_payload=png(width=3)
            )

    def test_post_stop_evidence_records_strict_external_captcha(self) -> None:
        evidence = web_evidence()
        challenge = snapshot(
            "https://wappass.baidu.com/static/captcha/tuxing_v2.html?"
            "backurl=https%3A%2F%2Fwww.baidu.com%2Fs%3Fwd%3DAsterinas"
        )
        challenge_evidence = {
            **evidence,
            "baidu-search.json": (json.dumps(challenge) + "\n").encode(),
        }
        index = validate_web_evidence(challenge_evidence)
        self.assertEqual(
            index["baidu-search.json"]["outcome"], "external-captcha"
        )
        self.assertNotIn(
            "fail baidu-search-not-pass",
            (ROOTFS / "browser_web_evidence.sh").read_text(),
        )
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
                self.assertIn(
                    "mode=system-nss-jit-overlay", check_trust_root(root)
                )

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
        forged_challenge["url"] = forged_challenge["url"].replace(
            "Asterinas", "forged"
        )
        with self.assertRaisesRegex(GateError, "back URL"):
            validate_baidu_search_outcome(forged_challenge)

    def test_gate_explicitly_disables_insecure_certs_and_records_evidence(self) -> None:
        gate = (ROOTFS / "browser_web_marionette_gate.py").read_text()
        self.assertIn('"acceptInsecureCerts": False', gate)
        self.assertIn('capabilities.get("acceptInsecureCerts") is not False', gate)
        self.assertIn("WebDriver:TakeScreenshot", gate)
        self.assertIn("DEBIAN_BROWSER_WEB_PLATFORM_READY", gate)
        self.assertIn('arguments[0] && arguments[0].lightweight', gate)
        self.assertIn('_snapshot(client, lightweight=True)', gate)
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
