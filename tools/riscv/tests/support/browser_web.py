#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
import zlib

from tools.riscv.debian.rootfs import browser_web_desktop_shell_qemu_gate as shell_gate
from tools.riscv.debian.rootfs import browser_web_marionette_gate as web_gate
from tools.riscv.debian.rootfs import browser_web_qemu_gate as web_qemu_gate
from tools.riscv.debian.rootfs.browser_web_marionette_gate import (
    GateError,
    MAX_SCREENSHOT_COMMAND_SECONDS,
    _navigate,
    _clear_document,
    _probe,
    _submit_fixture_search,
    _trigger_fixture_download,
    _submit_baidu_search,
    _script_value,
    _wait_for_fixture_download,
    _wait_for_probe,
    _wait_across_windows,
    _wait_baidu_search_outcome,
    _playback_probe,
    _wait_for_bilibili_playback,
    _write_evidence,
    _probe_mapping,
    probe_baidu_home,
    probe_baidu_search,
    probe_about_blank,
    probe_fixture_search,
    probe_fixture_home,
    probe_fixture_capabilities,
    run_baidu_home_gate,
    run_bilibili_playback_gate,
    select_bilibili_video,
    validate_gecko_profiler_environment,
    validate_baidu_home,
    validate_baidu_search,
    validate_baidu_challenge,
    validate_baidu_search_outcome,
    validate_bilibili_detail,
    validate_bilibili_playback,
    validate_fixture_search,
    fixture_index_url_from_environment,
    main as marionette_gate_main,
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
    Package as OverlayPackage,
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
from tools.riscv.debian.rootfs.signed_sources import SIGNED_SOURCES
from tools.riscv.debian.rootfs.desktop_network_gate import NetworkMode
from tools.riscv.debian.rootfs.desktop_network_qemu_gate import DESKTOP_NETWORK_QEMU_BOOTARGS
from tools.riscv.debian.rootfs.profiles import get_profile


ROOT = Path(__file__).resolve().parents[4]
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
    fixture_url = "http://10.0.2.2:17894/browser-quality/index.html?q=asterinas"
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
    bilibili_detail["playback"] = {
        "status": "pass",
        "url": selected,
        "source": "blob:https://www.bilibili.com/fixture-stream",
        "sourceKind": "blob",
        "playPromise": "resolved",
        "readyState": 4,
        "networkState": 2,
        "paused": False,
        "ended": False,
        "duration": 12.0,
        "bufferedEnd": 4.0,
        "initialCurrentTime": 0.0,
        "finalCurrentTime": 1.0,
        "progress": 1.0,
        "sampleCount": 3,
        "elapsedSeconds": 2.0,
        "events": {
            "loadedmetadata": 1,
            "canplay": 1,
            "playing": 1,
            "timeupdate": 4,
            "waiting": 0,
            "stalled": 0,
            "ended": 0,
            "error": 0,
        },
        "videoWidth": 1920,
        "videoHeight": 1080,
        "decodedFrames": 30,
        "droppedFrames": 5,
        "errorCode": None,
        "errorMessage": None,
    }
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
            "DEBIAN_BROWSER_WEB_BILIBILI_PLAYBACK status=pass bv=BV1Ab411c7De\n"
            "DEBIAN_BROWSER_WEB_PLATFORM_READY baidu_home=pass bilibili_home=pass "
            "bilibili_detail=pass bv=BV1Ab411c7De tls=verified\n"
        ).encode(),
        "firefox-user.js": (
            'user_pref("browser.download.folderList", 2);\n'
            'user_pref("network.proxy.type", 0);\n'
        ).encode(),
        "runtime-provenance.json": (
            json.dumps(
                {
                    "schema_version": 1,
                    "display_provider": "fbdev",
                    "framebuffer": {
                        "width": 1920,
                        "height": 1080,
                        "stride_bytes": 7680,
                        "bits_per_pixel": 32,
                    },
                    "firefox": {
                        "executable": "/usr/bin/firefox-esr",
                        "jit_overlay": False,
                    },
                    "packages": {
                        "xserver-xorg-core": "2:21.1.16-1",
                        "xserver-xorg-video-fbdev": "1:0.5.0-2",
                    },
                },
                sort_keys=True,
            )
            + "\n"
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

class _BrowserWebFixtures:
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


__all__ = tuple(
    name
    for name in globals()
    if name not in {"annotations"} and not name.startswith("__")
)
