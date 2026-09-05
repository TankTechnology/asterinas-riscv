#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Cold-boot the schema-seven real-web Firefox profile with one slirp NIC."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from tools.riscv.debian.rootfs.browser_web_contract import (
    firefox_ready_marker,
    validate_png_evidence,
    validate_uploaded_baidu_screenshot,
)
from tools.riscv.debian.rootfs.browser_web_marionette_gate import (
    select_bilibili_video,
    validate_baidu_home,
    validate_baidu_search_outcome,
    validate_bilibili_detail,
    validate_fixture_search,
)
from tools.riscv.debian.rootfs.desktop_m3_gate import classify_desktop
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import (
    DesktopM5QemuOperations,
    desktop_m5_qemu_argv,
    qemu_web_network_bootargs,
)
from tools.riscv.debian.rootfs.desktop_m5_network_gate import NetworkMode
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import GateTermination, TerminationSignalState
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig, GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate
from tools.riscv.megrez_proxy_bridge import (
    ProxyBridge,
    proxy_bridge_config_from_environment,
)


_BROWSER_WEB_PREFIX_MILESTONES = (
    "DEBIAN_BROWSER_WEB_TRUST_STATIC xul_ckbi=audited ca_bundle=audited package_closure=verified",
)
_BROWSER_WEB_SUFFIX_MILESTONES = (
    "DEBIAN_BROWSER_WEB_SECURITY parent_uid=1000 caps=zero nnp=1 content_processes=audited",
    "DEBIAN_BROWSER_WEB_CONTENT fixture_search=pass baidu_home=pass baidu_search=observed bilibili_home=pass bilibili_detail=pass bv=BV",
    "DEBIAN_BROWSER_WEB_TLS cert_verify=strict firefox_https=success override=absent",
)

_BASIC_BROWSER_WEB_SUFFIX_MILESTONES = (
    "DEBIAN_BROWSER_WEB_SECURITY parent_uid=1000 caps=zero nnp=1 content_processes=audited",
    "DEBIAN_BROWSER_WEB_CONTENT fixture_search=pass download=pass public_sites=not-run capabilities=fixture",
    "DEBIAN_BROWSER_WEB_TLS cert_verify=strict firefox_https=success override=absent",
)


def browser_web_milestones(
    mode: NetworkMode, *, basic_only: bool = False
) -> tuple[str, ...]:
    if not isinstance(mode, NetworkMode):
        raise ValueError("browser web mode must be a NetworkMode")
    dns = "proxy-delegated" if mode is NetworkMode.PROXY else "10.0.2.3"
    suffix = _BASIC_BROWSER_WEB_SUFFIX_MILESTONES if basic_only else _BROWSER_WEB_SUFFIX_MILESTONES
    return (
        f"DEBIAN_WEB_NETWORK_READY mode={mode.value} layers=10",
        *_BROWSER_WEB_PREFIX_MILESTONES,
        f"DEBIAN_BROWSER_WEB_NETWORK mode={mode.value} nic=virtio-slirp "
        f"dns={dns} https=curl-verified",
        *suffix,
        (
            f"DEBIAN_FIREFOX_BASIC_READY mode={mode.value} fixture=pass stable=pass"
            if basic_only else firefox_ready_marker(mode)
        ),
    )


BROWSER_WEB_MILESTONES = browser_web_milestones(NetworkMode.DIRECT)
_NETWORK_FAILURES = (
    b"DEBIAN_NETWORK_M5_FAIL reason=",
    b"DEBIAN_WEB_NETWORK_FAIL mode=",
)
_WEB_FAILURE = b"DEBIAN_BROWSER_WEB_FAIL reason="
_EXTERNAL_BLOCK = b"DEBIAN_BROWSER_WEB_EXTERNAL_BLOCK site=baidu reason=captcha"
KERNEL_FATAL_MARKERS = (
    b"Uncaught panic:",
    b"Kernel panic - not syncing",
)
WEB_EVIDENCE_PATHS = {
    "baidu-home.json": "/home/asterinas/browser-web-evidence/baidu-home.json",
    "baidu-home.png": "/home/asterinas/browser-web-evidence/baidu-home.png",
    "baidu-search.json": "/home/asterinas/browser-web-evidence/baidu-search.json",
    "baidu-search.png": "/home/asterinas/browser-web-evidence/baidu-search.png",
    "fixture-search.json": "/home/asterinas/browser-web-evidence/fixture-search.json",
    "fixture-search.png": "/home/asterinas/browser-web-evidence/fixture-search.png",
    "fixture-download.json": (
        "/home/asterinas/browser-web-evidence/fixture-download.json"
    ),
    "bilibili-home.json": "/home/asterinas/browser-web-evidence/bilibili-home.json",
    "bilibili-home.png": "/home/asterinas/browser-web-evidence/bilibili-home.png",
    "bilibili-detail.json": "/home/asterinas/browser-web-evidence/bilibili-detail.json",
    "bilibili-detail.png": "/home/asterinas/browser-web-evidence/bilibili-detail.png",
    "curl.log": "/home/asterinas/browser-web-curl-evidence.log",
    "security.log": "/home/asterinas/browser-web-security-evidence.log",
    "firefox-stderr.log": "/home/asterinas/firefox-web-stderr.log",
    "firefox-mozilla.log": "/home/asterinas/firefox-web-mozilla.log",
    "MarionetteActivePort": (
        "/home/asterinas/.mozilla/asterinas-browser-web/MarionetteActivePort"
    ),
    "trust-static.log": "/usr/share/asterinas/browser-web-trust-static.log",
    "ca-certificates.crt": "/etc/ssl/certs/ca-certificates.crt",
    "timeline.log": "/home/asterinas/browser-web-timeline.log",
    "firefox-user.js": "/home/asterinas/.mozilla/asterinas-browser-web/user.js",
}
BASIC_WEB_EVIDENCE_PATHS = {
    "fixture-search.json": "/home/asterinas/browser-web-evidence/fixture-search.json",
    "fixture-search.png": "/home/asterinas/browser-web-evidence/fixture-search.png",
    "fixture-download.json": "/home/asterinas/browser-web-evidence/fixture-download.json",
    "curl.log": "/home/asterinas/browser-web-curl-evidence.log",
    "security.log": "/home/asterinas/browser-web-security-evidence.log",
    "firefox-stderr.log": "/home/asterinas/firefox-web-stderr.log",
    "firefox-mozilla.log": "/home/asterinas/firefox-web-mozilla.log",
    "MarionetteActivePort": "/home/asterinas/.mozilla/asterinas-browser-web/MarionetteActivePort",
    "trust-static.log": "/usr/share/asterinas/browser-web-trust-static.log",
    "ca-certificates.crt": "/etc/ssl/certs/ca-certificates.crt",
    "timeline.log": "/home/asterinas/browser-web-timeline.log",
    "firefox-user.js": "/home/asterinas/.mozilla/asterinas-browser-web/user.js",
}
MAX_WEB_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_WEB_EVIDENCE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_WEB_OPAQUE_LOG_BYTES = 16 * 1024 * 1024
WEB_EVIDENCE_EXTRACT_TIMEOUT = 120.0
WEB_EVIDENCE_FILE_TIMEOUT = 15.0
MAX_TIMELINE_PHASE_DELTA_NS = 7200 * 1_000_000_000
_TRUST_LINE = re.compile(
    r"FIREFOX_TRUST_PASS mode=(embedded-xul|system-nss-jit-overlay) "
    r"ca_certificates=([1-9][0-9]{2,}) "
    r"firefox=installed ca_package=installed riscv_elf=1 nss_loader=1"
)
_DNS_LINE = re.compile(r"DNS host=(www\.(?:baidu|bilibili)\.com) address=([0-9.]+)")
_HTTPS_LINE = re.compile(
    r"HTTPS requested=(https://www\.(?:baidu|bilibili)\.com/) "
    r"status=([23][0-9]{2}) effective=(https://\S+) verify=0"
)
_HTTPS_TIMING_LINE = re.compile(
    r"HTTPS_TIMING requested=(https://www\.(?:baidu|bilibili)\.com/) "
    r"namelookup=(unknown|[0-9]+\.[0-9]+) "
    r"connect=(unknown|[0-9]+\.[0-9]+) "
    r"appconnect=(unknown|[0-9]+\.[0-9]+) "
    r"starttransfer=(unknown|[0-9]+\.[0-9]+)"
)
_PARENT_SECURITY_LINE = re.compile(
    r"BROWSER_WEB_SECURITY parent_pid=([1-9][0-9]*) uid=1000 caps=zero "
    r"nnp=1 sandbox_disable=absent"
)
_SERVICE_SECURITY_LINE = re.compile(
    r"BROWSER_WEB_SECURITY service_pid=([1-9][0-9]*) "
    r"nrestarts=0 stable=1 active=1"
)
_NETWORK_ENV_LINE = re.compile(
    r"BROWSER_WEB_NETWORK_ENV parent_pid=([1-9][0-9]*) mode=(proxy|direct)"
)
_DNS_DELEGATED_LINE = re.compile(
    r"DNS_DELEGATED mode=proxy host=(www\.(?:baidu|bilibili)\.com) "
    r"proxy=http://10\.0\.2\.2:17893"
)
_CHILD_SECURITY_LINE = re.compile(
    r"BROWSER_WEB_SECURITY child_pid=([1-9][0-9]*) "
    r"role=(child|content|socket|rdd) caps=zero nnp=1 seccomp=([012])"
)
_HASH_SECURITY_LINE = re.compile(
    r"(SYSTEM_CA|TRUST_STATIC)_SHA256 sha256=([0-9a-f]{64}) path=(/\S+)"
)
_TIMELINE_LINE = re.compile(
    rb"A_WEB_TIMELINE marker=(BOOT_[A-Z_]+) guest_monotonic_ns=([0-9]+) "
    rb"firefox_pid=([0-9]+)(?: page=([a-z-]+))?"
)
_TIMELINE_PHASE_LINE = re.compile(
    rb"A_WEB_PHASE phase=([a-z0-9-]+) state=(start|done) "
    rb"firefox_pid=([1-9][0-9]*)"
)
_TIMELINE_SELECTED_BV_LINE = re.compile(
    rb"A_WEB_SELECTED_BV url=https://www\.bilibili\.com/video/"
    rb"(BV[0-9A-Za-z]{10})/?(?:[?#]\S*)?"
)
_TIMELINE_PLATFORM_LINE = re.compile(
    rb"DEBIAN_BROWSER_WEB_PLATFORM_READY baidu_home=pass bilibili_home=pass "
    rb"bilibili_detail=pass bv=(BV[0-9A-Za-z]{10}) tls=verified"
)
_TIMELINE_PLATFORM_BASIC_LINE = re.compile(
    rb"DEBIAN_BROWSER_WEB_PLATFORM_READY_BASIC fixture_search=pass download=pass"
)
_TIMELINE_PROBE_COMMAND_LINE = re.compile(
    rb"A_WEB_PROBE_COMMAND state=(?:start|done)"
)
_TIMELINE_PROBE_RETRY_LINE = re.compile(
    rb'A_WEB_PROBE_RETRY error=("(?:[^"\\]|\\.){1,1024}")'
)
_TIMELINE_PROBE_CAPABILITIES_LINE = re.compile(
    rb'A_WEB_PROBE_CAPABILITIES state=(running|error) '
    rb'error=("(?:[^"\\]|\\.){0,256}") checks=(\{[ -~]{2,1536}\})'
)
MAX_TIMELINE_PROBE_DIAGNOSTICS = 256


def _valid_timeline_probe_retry(line: bytes) -> bool:
    match = _TIMELINE_PROBE_RETRY_LINE.fullmatch(line)
    if match is None:
        return False
    try:
        error = json.loads(match.group(1))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(error, str)


def _valid_timeline_probe_capabilities(line: bytes) -> bool:
    match = _TIMELINE_PROBE_CAPABILITIES_LINE.fullmatch(line)
    if match is None:
        return False
    try:
        error = json.loads(match.group(2))
        checks = json.loads(match.group(3))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(error, str)
        and isinstance(checks, dict)
        and 0 < len(checks) <= 32
        and all(
            isinstance(name, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", name)
            and isinstance(value, bool)
            for name, value in checks.items()
        )
    )


def _decode_json(contents: bytes, name: str) -> dict[str, object]:
    if not contents or len(contents) > 1024 * 1024:
        raise GateFailure(f"web evidence JSON has invalid size: {name}")
    try:
        value = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GateFailure(f"web evidence JSON is malformed: {name}") from error
    if not isinstance(value, dict):
        raise GateFailure(f"web evidence JSON is not an object: {name}")
    return value


def _text_lines(contents: bytes, name: str) -> list[str]:
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GateFailure(f"web evidence text is not UTF-8: {name}") from error
    if not text.endswith("\n"):
        raise GateFailure(f"web evidence text lacks final newline: {name}")
    return text.splitlines()


def _validate_curl_log(contents: bytes, *, network_mode: NetworkMode) -> None:
    lines = _text_lines(contents, "curl.log")
    dns: dict[str, str] = {}
    https: dict[str, tuple[str, str]] = {}
    timings: dict[str, tuple[str, str, str, str]] = {}
    delegated: set[str] = set()
    for line in lines:
        if match := _DNS_LINE.fullmatch(line):
            host, address = match.groups()
            octets = address.split(".")
            if len(octets) != 4 or any(not part.isdigit() or int(part) > 255 for part in octets):
                raise GateFailure("curl DNS evidence has an invalid address")
            if address.startswith(("0.", "127.")) or host in dns:
                raise GateFailure("curl DNS evidence is unsafe or duplicated")
            dns[host] = address
        elif match := _DNS_DELEGATED_LINE.fullmatch(line):
            host = match.group(1)
            if host in delegated:
                raise GateFailure("curl delegated DNS evidence is duplicated")
            delegated.add(host)
        elif match := _HTTPS_LINE.fullmatch(line):
            requested, status, effective = match.groups()
            if requested in https:
                raise GateFailure("curl HTTPS evidence is duplicated")
            requested_host = urlsplit(requested).hostname
            effective_host = urlsplit(effective).hostname
            if effective_host != requested_host and not effective_host.endswith(
                f".{requested_host}"
            ):
                raise GateFailure("curl HTTPS redirect leaves the requested site")
            https[requested] = (status, effective)
        elif match := _HTTPS_TIMING_LINE.fullmatch(line):
            requested, lookup, connect, appconnect, starttransfer = match.groups()
            if requested in timings:
                raise GateFailure("curl HTTPS timing evidence is duplicated")
            timings[requested] = (lookup, connect, appconnect, starttransfer)
        else:
            raise GateFailure("curl evidence contains an unstructured record")
    expected_hosts = {"www.baidu.com", "www.bilibili.com"}
    if network_mode is NetworkMode.DIRECT:
        dns_valid = set(dns) == expected_hosts and not delegated
    else:
        dns_valid = delegated == expected_hosts and not dns
    if not dns_valid or set(https) != {
        "https://www.baidu.com/",
        "https://www.bilibili.com/",
    }:
        raise GateFailure("curl evidence is missing a required endpoint")
    if set(timings) - set(https):
        raise GateFailure("curl HTTPS timing evidence has no matching HTTPS record")


def _validate_basic_curl_log(contents: bytes, *, network_mode: NetworkMode) -> None:
    """Validate the single controlled HTTPS reachability probe for basic mode."""

    lines = _text_lines(contents, "curl.log")
    expected_url = "https://deb.debian.org/"
    expected_host = "deb.debian.org"
    if network_mode is NetworkMode.DIRECT:
        dns = [line for line in lines if line.startswith("DNS host=")]
        if len(dns) != 1 or not dns[0].startswith(f"DNS host={expected_host} address="):
            raise GateFailure("basic curl DNS evidence is missing")
    else:
        delegated = [line for line in lines if line.startswith("DNS_DELEGATED mode=proxy")]
        if len(delegated) != 1 or f"host={expected_host} " not in delegated[0]:
            raise GateFailure("basic curl delegated DNS evidence is missing")
    https = [line for line in lines if line.startswith("HTTPS requested=")]
    if len(https) != 1 or f"requested={expected_url} " not in https[0] or " verify=0" not in https[0]:
        raise GateFailure("basic curl HTTPS evidence is missing")


def _validate_security_log(
    contents: bytes,
    *,
    network_mode: NetworkMode,
) -> tuple[dict[str, tuple[str, str]], int, str]:
    lines = _text_lines(contents, "security.log")
    parent_pid: str | None = None
    service_pid: str | None = None
    child_pids: set[str] = set()
    content_seccomp_modes: set[str] = set()
    network_parent_pid: str | None = None
    hashes: dict[str, tuple[str, str]] = {}
    for line in lines:
        if match := _HASH_SECURITY_LINE.fullmatch(line):
            kind, digest, path = match.groups()
            if kind in hashes:
                raise GateFailure("browser security hash evidence is duplicated")
            hashes[kind] = (digest, path)
        elif match := _PARENT_SECURITY_LINE.fullmatch(line):
            if parent_pid is not None:
                raise GateFailure("browser parent security evidence is duplicated")
            parent_pid = match.group(1)
        elif match := _SERVICE_SECURITY_LINE.fullmatch(line):
            if service_pid is not None:
                raise GateFailure("browser service stability evidence is duplicated")
            service_pid = match.group(1)
        elif match := _NETWORK_ENV_LINE.fullmatch(line):
            if network_parent_pid is not None or match.group(2) != network_mode.value:
                raise GateFailure("browser network environment evidence is invalid")
            network_parent_pid = match.group(1)
        elif match := _CHILD_SECURITY_LINE.fullmatch(line):
            pid, role, seccomp = match.groups()
            if pid in child_pids:
                raise GateFailure("browser security child evidence is duplicated")
            child_pids.add(pid)
            if role == "content":
                if seccomp == "1":
                    raise GateFailure("browser content process has invalid strict seccomp")
                content_seccomp_modes.add(seccomp)
        else:
            raise GateFailure("browser security evidence contains an unstructured record")
    expected_paths = {
        "SYSTEM_CA": "/etc/ssl/certs/ca-certificates.crt",
        "TRUST_STATIC": "/usr/share/asterinas/browser-web-trust-static.log",
    }
    if (
        set(hashes) != set(expected_paths)
        or any(hashes[kind][1] != path for kind, path in expected_paths.items())
        or parent_pid is None
        or service_pid != parent_pid
        or network_parent_pid != parent_pid
        or not child_pids
        or not content_seccomp_modes
    ):
        raise GateFailure("browser security evidence is incomplete")
    if len(content_seccomp_modes) != 1:
        raise GateFailure("browser content processes have mixed seccomp modes")
    content_seccomp = next(iter(content_seccomp_modes))
    sandbox_outcome = (
        "enabled"
        if content_seccomp == "2"
        else "unavailable-firefox-riscv64-build"
    )
    assert parent_pid is not None
    return hashes, int(parent_pid), sandbox_outcome


def _validate_firefox_logs(stderr: bytes, mozilla: bytes) -> None:
    for line in (stderr + b"\n" + mozilla).splitlines():
        if b"Exiting due to channel error." in line:
            raise GateFailure("Firefox log records a channel-error exit")
        if b"SCM_RIGHTS" in line and (
            re.search(rb"(^|[^A-Z])EPERM([^A-Z]|$)", line)
            or b"Operation not permitted" in line
        ):
            raise GateFailure("Firefox log records SCM_RIGHTS permission failure")


def _validate_firefox_network_profile(
    contents: bytes,
    *,
    network_mode: NetworkMode,
) -> None:
    lines = _text_lines(contents, "firefox-user.js")
    proxy_lines = [line for line in lines if "network.proxy." in line]
    if network_mode is NetworkMode.DIRECT:
        expected = {'user_pref("network.proxy.type", 0);'}
    else:
        expected = {
            'user_pref("network.proxy.type", 1);',
            'user_pref("network.proxy.http", "10.0.2.2");',
            'user_pref("network.proxy.http_port", 17893);',
            'user_pref("network.proxy.ssl", "10.0.2.2");',
            'user_pref("network.proxy.ssl_port", 17893);',
            'user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1, 10.0.2.2");',
        }
    if set(proxy_lines) != expected or len(proxy_lines) != len(expected):
        raise GateFailure("Firefox network profile does not match selected mode")


def _validate_timeline(
    contents: bytes, *, basic_only: bool = False
) -> tuple[int, str]:
    required = (
        (b"BOOT_SYSTEMD_BEGIN", None),
        (b"BOOT_BASIC_TARGET", None),
        (b"BOOT_NETWORK_READY", None),
        (b"BOOT_X_SOCKET_READY", None),
        (b"BOOT_FIREFOX_WRAPPER_START", None),
        (b"BOOT_FIREFOX_EXEC", None),
        (b"BOOT_MARIONETTE_PORT_READY", None),
        (b"BOOT_MARIONETTE_CONNECTED", None),
        (b"BOOT_NEW_SESSION_DONE", None),
        (b"BOOT_FIRST_WINDOW_READY", None),
    )
    if basic_only:
        required = (*required, (b"BOOT_DOM_READY", b"fixture-search"))
    else:
        required = (
            *required[:10],
            (b"BOOT_DOM_READY", b"baidu-home"),
            (b"BOOT_DOM_READY", b"fixture-search"),
            (b"BOOT_DOM_READY", b"bilibili-home"),
            (b"BOOT_DOM_READY", b"bilibili-detail"),
            (b"BOOT_DOM_READY", b"baidu-search"),
        )
    observed: list[tuple[bytes, bytes | None]] = []
    phase_pids: set[int] = set()
    pending_phase: bytes | None = None
    phase_pairs = 0
    selected_bv: bytes | None = None
    platform_bv: bytes | None = None
    probe_diagnostics = 0
    previous_ns = -1
    legacy_clock_reset = False
    browser_pid: int | None = None
    for line in contents.splitlines():
        match = _TIMELINE_LINE.fullmatch(line)
        if match is None:
            if phase_match := _TIMELINE_PHASE_LINE.fullmatch(line):
                phase_name, state, phase_pid = phase_match.groups()
                phase_pids.add(int(phase_pid))
                if state == b"start":
                    if pending_phase is not None:
                        raise GateFailure("browser phase diagnostics overlap")
                    pending_phase = phase_name
                elif pending_phase != phase_name:
                    raise GateFailure("browser phase diagnostics are not paired")
                else:
                    pending_phase = None
                    phase_pairs += 1
                continue
            if selected_match := _TIMELINE_SELECTED_BV_LINE.fullmatch(line):
                if selected_bv is not None:
                    raise GateFailure("browser selected BV evidence is duplicated")
                selected_bv = selected_match.group(1)
                continue
            if platform_match := _TIMELINE_PLATFORM_LINE.fullmatch(line):
                if platform_bv is not None:
                    raise GateFailure("browser platform evidence is duplicated")
                platform_bv = platform_match.group(1)
                continue
            if basic_only and _TIMELINE_PLATFORM_BASIC_LINE.fullmatch(line):
                continue
            if (
                _TIMELINE_PROBE_COMMAND_LINE.fullmatch(line)
                or _valid_timeline_probe_retry(line)
                or _valid_timeline_probe_capabilities(line)
            ):
                probe_diagnostics += 1
                if probe_diagnostics > MAX_TIMELINE_PROBE_DIAGNOSTICS:
                    raise GateFailure(
                        "browser startup timeline has too many probe diagnostics"
                    )
                continue
            raise GateFailure("browser startup timeline contains an invalid record")
        marker, monotonic, pid_text, page = match.groups()
        current_ns = int(monotonic)
        if current_ns < previous_ns:
            # Images built before the uptime-clock fix used wall time for the
            # shell markers and CLOCK_MONOTONIC for the Python markers.  Admit
            # exactly that one known transition, at the fixed producer
            # boundary, while keeping every value within each domain ordered.
            if not (
                not legacy_clock_reset
                and len(observed) == 7
                and previous_ns >= 1_000_000_000_000_000
                and current_ns < 1_000_000_000_000_000
                and marker == b"BOOT_MARIONETTE_CONNECTED"
            ):
                raise GateFailure("browser startup timeline is not monotonic")
            legacy_clock_reset = True
        if previous_ns >= 0 and current_ns - previous_ns > MAX_TIMELINE_PHASE_DELTA_NS:
            raise GateFailure("browser startup timeline phase delta is unbounded")
        previous_ns = current_ns
        pid = int(pid_text)
        if len(observed) < 4:
            if pid != 0:
                raise GateFailure("system startup timeline unexpectedly has a Firefox PID")
        elif browser_pid is None:
            if pid <= 1:
                raise GateFailure("browser startup timeline has an invalid Firefox PID")
            browser_pid = pid
        elif pid != browser_pid:
            raise GateFailure("browser startup timeline changed Firefox PID")
        observed.append((marker, page))
    if len(observed) != len(required):
        raise GateFailure("browser startup timeline is not exact-one per phase")
    if tuple(observed) != required:
        raise GateFailure("browser startup timeline is incomplete or out of order")
    assert browser_pid is not None
    if pending_phase is not None or phase_pairs == 0 or phase_pids != {browser_pid}:
        raise GateFailure("browser phase diagnostics are incomplete or inconsistent")
    if not basic_only and (selected_bv is None or platform_bv != selected_bv):
        raise GateFailure("browser public-site evidence is incomplete or inconsistent")
    return browser_pid, (
        "legacy-split-realtime-monotonic" if legacy_clock_reset else "monotonic"
    )


def validate_web_evidence(
    evidence: Mapping[str, bytes],
    *,
    network_mode: NetworkMode = NetworkMode.DIRECT,
    basic_only: bool = False,
) -> dict[str, dict[str, object]]:
    """Validate the exact extracted browser evidence set and return its index."""

    if not isinstance(network_mode, NetworkMode):
        raise GateFailure("browser web evidence network mode is invalid")
    expected_paths = BASIC_WEB_EVIDENCE_PATHS if basic_only else WEB_EVIDENCE_PATHS
    if set(evidence) != set(expected_paths):
        raise GateFailure("browser web evidence file set is incomplete")
    if sum(len(contents) for contents in evidence.values()) > MAX_WEB_EVIDENCE_TOTAL_BYTES:
        raise GateFailure("browser web evidence exceeds aggregate size cap")
    for name, contents in evidence.items():
        if len(contents) > MAX_WEB_EVIDENCE_BYTES:
            raise GateFailure(f"browser web evidence exceeds size cap: {name}")
    for name in ("firefox-stderr.log", "firefox-mozilla.log"):
        if len(evidence[name]) > MAX_WEB_OPAQUE_LOG_BYTES:
            raise GateFailure(f"browser web log exceeds size cap: {name}")
    _validate_firefox_logs(
        evidence["firefox-stderr.log"], evidence["firefox-mozilla.log"]
    )
    timeline_pid, timeline_clock = _validate_timeline(
        evidence["timeline.log"], basic_only=basic_only
    )

    if basic_only:
        validate_fixture_search(
            _decode_json(evidence["fixture-search.json"], "fixture-search.json"),
            "http://10.0.2.2:17894/browser-quality/index.html?q=asterinas",
        )
        fixture_download = _decode_json(
            evidence["fixture-download.json"], "fixture-download.json"
        )
        if fixture_download != {
            "bytes": 256 * 1024,
            "filename": "asterinas-browser-quality.bin",
            "sha256": (
                "2312394bd99545d9de131c24efb781e765ac1aec243f2ed9347597a793a415e9"
            ),
        }:
            raise GateFailure("controlled Firefox download evidence is malformed")
        validate_png_evidence(evidence["fixture-search.png"], "fixture-search")
        _validate_basic_curl_log(evidence["curl.log"], network_mode=network_mode)
        security_hashes, security_pid, sandbox_outcome = _validate_security_log(
            evidence["security.log"], network_mode=network_mode
        )
        _validate_firefox_network_profile(
            evidence["firefox-user.js"], network_mode=network_mode
        )
        if timeline_pid != security_pid:
            raise GateFailure("browser startup timeline PID does not match security evidence")
        if evidence["MarionetteActivePort"].strip() != b"2828":
            raise GateFailure("MarionetteActivePort is not the fixed endpoint")
        trust_lines = _text_lines(evidence["trust-static.log"], "trust-static.log")
        if len(trust_lines) != 1 or not _TRUST_LINE.fullmatch(trust_lines[0]):
            raise GateFailure("static Firefox trust evidence is missing")
        if security_hashes["TRUST_STATIC"][0] != hashlib.sha256(
            evidence["trust-static.log"]
        ).hexdigest() or security_hashes["SYSTEM_CA"][0] != hashlib.sha256(
            evidence["ca-certificates.crt"]
        ).hexdigest():
            raise GateFailure("Firefox trust evidence hash does not match")
        index = {
            name: {"sha256": hashlib.sha256(contents).hexdigest(), "size": len(contents)}
            for name, contents in sorted(evidence.items())
        }
        index["security.log"]["sandbox_outcome"] = sandbox_outcome
        index["trust-static.log"]["trust_mode"] = _TRUST_LINE.fullmatch(
            trust_lines[0]
        ).group(1)
        index["timeline.log"]["clock_outcome"] = timeline_clock
        return index

    snapshots = {
        name: _decode_json(evidence[f"{name}.json"], f"{name}.json")
        for name in (
            "baidu-home",
            "baidu-search",
            "fixture-search",
            "bilibili-home",
            "bilibili-detail",
        )
    }
    # With an HTTP proxy, Firefox's NavigationTiming secureConnectionStart is
    # allowed to be zero because the browser records the proxy connection
    # rather than the proxy's upstream CONNECT/TLS handshake.  The proxy
    # network gate already verifies the upstream certificate and HTTPS status;
    # direct mode retains the browser-side timing requirement.
    validate_baidu_home(
        snapshots["baidu-home"],
        require_tls_handshake=network_mode is NetworkMode.DIRECT,
    )
    baidu_search_outcome = validate_baidu_search_outcome(
        snapshots["baidu-search"]
    )
    validate_fixture_search(
        snapshots["fixture-search"],
        "http://10.0.2.2:17894/browser-quality/index.html?q=asterinas",
    )
    fixture_download = _decode_json(
        evidence["fixture-download.json"], "fixture-download.json"
    )
    if fixture_download != {
        "bytes": 256 * 1024,
        "filename": "asterinas-browser-quality.bin",
        "sha256": (
            "2312394bd99545d9de131c24efb781e765ac1aec243f2ed9347597a793a415e9"
        ),
    }:
        raise GateFailure("controlled Firefox download evidence is malformed")
    selected = select_bilibili_video(snapshots["bilibili-home"])
    validate_bilibili_detail(snapshots["bilibili-detail"], selected)

    for name in (
        "baidu-home",
        "baidu-search",
        "fixture-search",
        "bilibili-home",
        "bilibili-detail",
    ):
        validate_png_evidence(evidence[f"{name}.png"], name)

    _validate_curl_log(evidence["curl.log"], network_mode=network_mode)
    security_hashes, security_pid, sandbox_outcome = _validate_security_log(
        evidence["security.log"], network_mode=network_mode
    )
    _validate_firefox_network_profile(
        evidence["firefox-user.js"], network_mode=network_mode
    )
    if timeline_pid != security_pid:
        raise GateFailure("browser startup timeline PID does not match security evidence")
    if evidence["MarionetteActivePort"].strip() != b"2828":
        raise GateFailure("MarionetteActivePort is not the fixed endpoint")
    trust_lines = _text_lines(evidence["trust-static.log"], "trust-static.log")
    if len(trust_lines) != 1 or not _TRUST_LINE.fullmatch(trust_lines[0]):
        raise GateFailure("static Firefox trust evidence is missing")
    if security_hashes["TRUST_STATIC"][0] != hashlib.sha256(
        evidence["trust-static.log"]
    ).hexdigest():
        raise GateFailure("static Firefox trust evidence hash does not match")
    if security_hashes["SYSTEM_CA"][0] != hashlib.sha256(
        evidence["ca-certificates.crt"]
    ).hexdigest():
        raise GateFailure("system CA evidence hash does not match")

    index = {
        name: {
            "sha256": hashlib.sha256(contents).hexdigest(),
            "size": len(contents),
        }
        for name, contents in sorted(evidence.items())
    }
    index["security.log"]["sandbox_outcome"] = sandbox_outcome
    index["baidu-search.json"]["outcome"] = baidu_search_outcome
    index["trust-static.log"]["trust_mode"] = _TRUST_LINE.fullmatch(
        trust_lines[0]
    ).group(1)
    index["timeline.log"]["clock_outcome"] = timeline_clock
    return index


def _extract_web_evidence(
    root_fd: int, directory: Path, *, basic_only: bool = False
) -> dict[str, bytes]:
    extracted: dict[str, bytes] = {}
    image = f"/proc/self/fd/{root_fd}"
    deadline = time.monotonic() + WEB_EVIDENCE_EXTRACT_TIMEOUT
    paths = BASIC_WEB_EVIDENCE_PATHS if basic_only else WEB_EVIDENCE_PATHS
    for name, guest_path in paths.items():
        destination = directory / name
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GateFailure("browser web evidence extraction exceeded total deadline")
        try:
            result = subprocess.run(
                ["debugfs", "-R", f"dump -p {guest_path} {name}", image],
                check=False,
                capture_output=True,
                pass_fds=(root_fd,),
                cwd=directory,
                timeout=min(WEB_EVIDENCE_FILE_TIMEOUT, remaining),
            )
        except subprocess.TimeoutExpired as error:
            raise GateFailure(f"debugfs timed out extracting web evidence: {name}") from error
        if result.returncode != 0:
            raise GateFailure(f"debugfs failed to extract web evidence: {name}")
        try:
            metadata = destination.lstat()
        except FileNotFoundError as error:
            raise GateFailure(f"browser web evidence is missing: {name}") from error
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_WEB_EVIDENCE_BYTES:
            raise GateFailure(f"browser web evidence is unsafe: {name}")
        extracted[name] = destination.read_bytes()
    return extracted


def _diagnostic_gdb_port() -> int | None:
    """Return the explicitly requested loopback-only QEMU GDB port."""

    raw = os.environ.get("ASTERINAS_QEMU_GDB_PORT")
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError("ASTERINAS_QEMU_GDB_PORT must be a decimal TCP port")
    port = int(raw)
    if not 1024 <= port <= 65535:
        raise ValueError("ASTERINAS_QEMU_GDB_PORT must be between 1024 and 65535")
    return port


def browser_web_qemu_argv(
    *, gdb_port: int | None = None, **arguments: Any
) -> tuple[str, ...]:
    """Admit one slirp NIC and an optional loopback diagnostic GDB stub."""

    if gdb_port is not None and (
        isinstance(gdb_port, bool)
        or not isinstance(gdb_port, int)
        or not 1024 <= gdb_port <= 65535
    ):
        raise ValueError("diagnostic QEMU GDB port must be between 1024 and 65535")

    argv = desktop_m5_qemu_argv(**arguments)
    root_drives = [
        (index, value)
        for index, value in enumerate(argv)
        if value.startswith("if=none,format=raw,file=")
        and ",id=rootdisk,cache=directsync" in value
    ]
    if len(root_drives) != 1:
        raise ValueError("online web runner requires one writable run-copy disk")
    root_index, root_drive = root_drives[0]
    argv = (
        *argv[:root_index],
        root_drive.replace(",cache=directsync", ",cache=writeback"),
        *argv[root_index + 1 :],
    )
    if argv.count("-netdev") != 1 or argv.count("user,id=net0") != 1:
        raise ValueError("online web runner requires exactly one slirp backend")
    if argv.count("-device") < 1 or argv.count("virtio-net-device,netdev=net0") != 1:
        raise ValueError("online web runner requires exactly one virtio NIC")
    if "-nic" in argv:
        raise ValueError("online web runner contains a conflicting NIC contract")
    if gdb_port is not None:
        if "-gdb" in argv or "-s" in argv or "-S" in argv:
            raise ValueError("online web runner contains a conflicting GDB contract")
        argv = (*argv, "-gdb", f"tcp:127.0.0.1:{gdb_port}")
    return argv


def classify_browser_web_qemu(
    transcript: bytes,
    *,
    expected_debian_release: str,
    network_mode: NetworkMode = NetworkMode.DIRECT,
    basic_only: bool = False,
) -> GateResult:
    if not isinstance(network_mode, NetworkMode):
        return GateResult(False, "Firefox network mode is invalid", None)
    clean = transcript.lower()
    if any(marker.lower() in clean for marker in KERNEL_FATAL_MARKERS):
        return GateResult(False, "kernel fatal marker", None)
    if any(marker.lower() in clean for marker in _NETWORK_FAILURES):
        return GateResult(False, "network guest failure", None)
    foreign_mode = (
        NetworkMode.PROXY if network_mode is NetworkMode.DIRECT else NetworkMode.DIRECT
    )
    if firefox_ready_marker(foreign_mode).encode() in transcript:
        return GateResult(False, "mixed Firefox network modes", None)
    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=browser_web_milestones(network_mode, basic_only=basic_only),
        failure_marker=_WEB_FAILURE,
    )


class BrowserWebQemuOperations(DesktopM5QemuOperations):
    SCHEMA_VERSION = 7
    PROFILE_NAME = "browser-web"
    ARTIFACT_PREFIX = "browser-web-qemu"
    MILESTONES = BROWSER_WEB_MILESTONES
    FAILURE_MARKER = _WEB_FAILURE
    ADDITIONAL_FAILURE_MARKERS = (*_NETWORK_FAILURES, *KERNEL_FATAL_MARKERS)
    BOOTARGS = qemu_web_network_bootargs(NetworkMode.DIRECT)

    def __init__(
        self,
        config: GateConfig,
        *,
        network_mode: NetworkMode = NetworkMode.DIRECT,
        proxy_bridge: ProxyBridge | None = None,
        basic_only: bool = False,
        **arguments: Any,
    ) -> None:
        if not isinstance(network_mode, NetworkMode):
            raise ValueError("browser network mode must be a NetworkMode")
        if network_mode is NetworkMode.DIRECT and proxy_bridge is not None:
            raise ValueError("direct browser mode cannot own a proxy bridge")
        self.network_mode = network_mode
        if not isinstance(basic_only, bool):
            raise ValueError("basic_only must be a bool")
        self.basic_only = basic_only
        self.BOOTARGS = qemu_web_network_bootargs(
            network_mode, basic_only=basic_only
        )
        self.MILESTONES = browser_web_milestones(
            network_mode, basic_only=basic_only
        )
        self.proxy_bridge = proxy_bridge
        if network_mode is NetworkMode.PROXY and self.proxy_bridge is None:
            self.proxy_bridge = ProxyBridge(
                proxy_bridge_config_from_environment(listen_address="0.0.0.0")
            )
        super().__init__(config, **arguments)
        self._web_evidence: dict[str, bytes] = {}
        self._web_evidence_index: dict[str, dict[str, object]] = {}

    def __enter__(self) -> BrowserWebQemuOperations:
        try:
            super().__enter__()
            if self.proxy_bridge is not None:
                self.proxy_bridge.start()
            return self
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self.proxy_bridge is not None:
                self.proxy_bridge.close()
        finally:
            super().close()

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return browser_web_qemu_argv(
            gdb_port=_diagnostic_gdb_port(), **arguments
        )

    def run_protocol(self, session: dict[str, Any], config: GateConfig) -> None:
        try:
            super().run_protocol(session, config)
        except GateFailure:
            serial = session["serial"]
            if any(marker in serial.transcript for marker in KERNEL_FATAL_MARKERS):
                # The generic desktop gate returns as soon as it sees the panic
                # prefix.  Give the kernel logger one bounded cleanup interval
                # to print the backtrace before HMP terminates QEMU; otherwise
                # the most valuable diagnostic is consistently truncated.
                serial.drain(time.monotonic() + config.cleanup_timeout)
            raise

    def invalidate(self, config: GateConfig) -> None:
        super().invalidate(config)
        self._require_output().invalidate(
            *(f"browser-web-{name}" for name in (
                BASIC_WEB_EVIDENCE_PATHS if self.basic_only else WEB_EVIDENCE_PATHS
            )),
            "browser-web-evidence.SHA256SUMS",
            "browser-web-evidence-index.json",
            "proxy-bridge.json",
        )

    def hash_final_root(self, config: GateConfig, prepared: Any) -> str:
        output = self._require_output()
        root_fd = os.open(
            "debian-root.run.ext2",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=output._operation_fd,
        )
        try:
            if not stat.S_ISREG(os.fstat(root_fd).st_mode):
                raise GateFailure("writable run root is not a regular file")
            with tempfile.TemporaryDirectory(
                prefix=".browser-web-extract-",
                dir=f"/proc/self/fd/{output._operation_fd}",
            ) as temporary:
                self._web_evidence = _extract_web_evidence(
                    root_fd, Path(temporary), basic_only=self.basic_only
                )
        finally:
            os.close(root_fd)
        self._web_evidence_index = validate_web_evidence(
            self._web_evidence,
            network_mode=self.network_mode,
            basic_only=self.basic_only,
        )
        return super().hash_final_root(config, prepared)

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        output = self._require_output()
        result["network_mode"] = self.network_mode.value
        if self.proxy_bridge is not None:
            summary = self.proxy_bridge.summary()
            result["proxy_bridge"] = summary
            output.atomic_write(
                "proxy-bridge.json",
                (
                    json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode(),
            )
        if result.get("passed"):
            if not self.basic_only:
                try:
                    expected_capture = self._web_evidence.get("baidu-search.png")
                    if expected_capture is None:
                        raise GateFailure("validated Baidu screenshot was not retained")
                    capture_summary = validate_uploaded_baidu_screenshot(
                        self.fixture.capture_summary(),
                        self.fixture.capture_payload(),
                        expected_payload=expected_capture,
                    )
                except GateFailure as error:
                    result["passed"] = False
                    result["reason"] = error.reason
                else:
                    result["baidu_screenshot"] = {
                        **capture_summary,
                        "artifact": "browser-web-baidu-search.png",
                    }
        if result.get("passed"):
            if not self._web_evidence or not self._web_evidence_index:
                raise GateFailure("validated browser web evidence was not retained")
            for name, contents in sorted(self._web_evidence.items()):
                output.atomic_write(f"browser-web-{name}", contents)
            sums = "".join(
                f"{metadata['sha256']}  browser-web-{name}\n"
                for name, metadata in sorted(self._web_evidence_index.items())
            )
            output.atomic_write("browser-web-evidence.SHA256SUMS", sums.encode())
            index = {
                "files": self._web_evidence_index,
            }
            if self.basic_only:
                index["profile"] = "basic-fixture"
            else:
                index["selected_bv_url"] = json.loads(
                    self._web_evidence["bilibili-detail.json"]
                )["url"]
                index["baidu_search_outcome"] = validate_baidu_search_outcome(
                    json.loads(self._web_evidence["baidu-search.json"])
                )
            output.atomic_write(
                "browser-web-evidence-index.json",
                (json.dumps(index, indent=2, sort_keys=True) + "\n").encode(),
            )
            result["web_evidence"] = index
        super().publish(config, prepared, transcript, result)


def orchestrate_browser_web_qemu_gate(
    config: GateConfig, operations: BrowserWebQemuOperations
) -> dict[str, object]:
    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=lambda transcript, expected_debian_release: (
            classify_browser_web_qemu(
                transcript,
                expected_debian_release=expected_debian_release,
                network_mode=operations.network_mode,
                basic_only=operations.basic_only,
            )
        ),
    )


def _parse_network_mode(arguments: list[str] | None) -> tuple[NetworkMode, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--network-mode",
        choices=tuple(NetworkMode),
        type=NetworkMode,
        required=True,
    )
    parser.add_argument("--basic-only", action="store_true")
    values, remaining = parser.parse_known_args(
        sys.argv[1:] if arguments is None else arguments
    )
    if values.basic_only:
        remaining = [*remaining, "--basic-only"]
    return values.network_mode, remaining


def main(arguments: list[str] | None = None) -> int:
    try:
        network_mode, gate_arguments = _parse_network_mode(arguments)
        basic_only = "--basic-only" in gate_arguments
        if basic_only:
            gate_arguments = [arg for arg in gate_arguments if arg != "--basic-only"]
        config = parse_gate_args(gate_arguments)
        _safe_output(config.output_directory)
        with (
            TerminationSignalState(),
            BrowserWebQemuOperations(
                config, network_mode=network_mode, basic_only=basic_only
            ) as operations,
        ):
            result = orchestrate_browser_web_qemu_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-browser-web-qemu-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-browser-web-qemu-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
