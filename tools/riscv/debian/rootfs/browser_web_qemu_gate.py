#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Cold-boot the schema-seven real-web Firefox profile with one slirp NIC."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping
from urllib.parse import urlsplit
import zlib

from tools.riscv.debian.rootfs.browser_web_marionette_gate import (
    select_bilibili_video,
    validate_baidu_home,
    validate_baidu_search,
    validate_bilibili_detail,
)
from tools.riscv.debian.rootfs.desktop_m3_gate import classify_desktop
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import (
    DESKTOP_M5_QEMU_BOOTARGS,
    DesktopM5QemuOperations,
    desktop_m5_qemu_argv,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import GateTermination, TerminationSignalState
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig, GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate


BROWSER_WEB_MILESTONES = (
    "DEBIAN_BROWSER_WEB_NETWORK nic=virtio-slirp dns=10.0.2.3 https=curl-verified",
    "DEBIAN_BROWSER_WEB_TRUST_STATIC xul_ckbi=audited ca_bundle=audited package_closure=verified",
    "DEBIAN_BROWSER_WEB_SECURITY parent_uid=1000 caps=zero nnp=1 content_seccomp=2 sandbox=normal",
    "DEBIAN_BROWSER_WEB_CONTENT baidu_home=pass baidu_search=pass bilibili_home=pass bilibili_detail=pass bv=BV",
    "DEBIAN_BROWSER_WEB_TLS cert_verify=strict firefox_https=success override=absent",
    "DEBIAN_BROWSER_WEB_READY user=asterinas display=:0",
)
_NETWORK_FAILURE = b"DEBIAN_NETWORK_M5_FAIL reason="
_WEB_FAILURE = b"DEBIAN_BROWSER_WEB_FAIL reason="
KERNEL_FATAL_MARKERS = (
    b"Uncaught panic:",
    b"Kernel panic - not syncing",
)
WEB_EVIDENCE_PATHS = {
    "baidu-home.json": "/home/asterinas/browser-web-evidence/baidu-home.json",
    "baidu-home.png": "/home/asterinas/browser-web-evidence/baidu-home.png",
    "baidu-search.json": "/home/asterinas/browser-web-evidence/baidu-search.json",
    "baidu-search.png": "/home/asterinas/browser-web-evidence/baidu-search.png",
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
}
MAX_WEB_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_WEB_EVIDENCE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_WEB_OPAQUE_LOG_BYTES = 16 * 1024 * 1024
MAX_WEB_SCREENSHOT_PIXELS_BYTES = 64 * 1024 * 1024
WEB_EVIDENCE_EXTRACT_TIMEOUT = 120.0
WEB_EVIDENCE_FILE_TIMEOUT = 15.0
_TRUST_LINE = re.compile(
    r"FIREFOX_TRUST_PASS mode=embedded-xul ca_certificates=([1-9][0-9]{2,}) "
    r"firefox=installed ca_package=installed riscv_elf=1 nss_loader=1"
)
_DNS_LINE = re.compile(r"DNS host=(www\.(?:baidu|bilibili)\.com) address=([0-9.]+)")
_HTTPS_LINE = re.compile(
    r"HTTPS requested=(https://www\.(?:baidu|bilibili)\.com/) "
    r"status=([23][0-9]{2}) effective=(https://\S+) verify=0"
)
_PARENT_SECURITY_LINE = re.compile(
    r"BROWSER_WEB_SECURITY parent_pid=([1-9][0-9]*) uid=1000 caps=zero "
    r"nnp=1 sandbox_disable=absent"
)
_SERVICE_SECURITY_LINE = re.compile(
    r"BROWSER_WEB_SECURITY service_pid=([1-9][0-9]*) "
    r"nrestarts=0 stable=1 active=1"
)
_CHILD_SECURITY_LINE = re.compile(
    r"BROWSER_WEB_SECURITY child_pid=([1-9][0-9]*) "
    r"role=(child|content|socket|rdd) caps=zero nnp=1 seccomp=([012])"
)
_HASH_SECURITY_LINE = re.compile(
    r"(SYSTEM_CA|TRUST_STATIC)_SHA256 sha256=([0-9a-f]{64}) path=(/\S+)"
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


def _validate_png(contents: bytes, name: str) -> None:
    if not contents.startswith(b"\x89PNG\r\n\x1a\n"):
        raise GateFailure(f"browser web screenshot is not PNG: {name}")
    offset = 8
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(contents):
        if offset + 12 > len(contents):
            raise GateFailure(f"browser web screenshot is truncated: {name}")
        length = struct.unpack(">I", contents[offset : offset + 4])[0]
        kind = contents[offset + 4 : offset + 8]
        end = offset + 12 + length
        if length > MAX_WEB_EVIDENCE_BYTES or end > len(contents):
            raise GateFailure(f"browser web screenshot chunk is invalid: {name}")
        payload = contents[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", contents[offset + 8 + length : end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise GateFailure(f"browser web screenshot CRC is invalid: {name}")
        chunks.append((kind, payload))
        offset = end
        if kind == b"IEND":
            break
    if offset != len(contents) or not chunks or chunks[0][0] != b"IHDR":
        raise GateFailure(f"browser web screenshot structure is invalid: {name}")
    ihdr = chunks[0][1]
    if len(ihdr) != 13:
        raise GateFailure(f"browser web screenshot IHDR is invalid: {name}")
    width, height, depth, color_type, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if not width or not height or width > 16384 or height > 16384:
        raise GateFailure(f"browser web screenshot dimensions are invalid: {name}")
    if channels is None or depth != 8 or compression or filtering or interlace:
        raise GateFailure(f"browser web screenshot format is unsupported: {name}")
    compressed = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
    expected_size = height * (1 + width * channels)
    if expected_size > MAX_WEB_SCREENSHOT_PIXELS_BYTES:
        raise GateFailure(f"browser web screenshot decoded size is excessive: {name}")
    try:
        decompressor = zlib.decompressobj()
        pixels = decompressor.decompress(compressed, expected_size + 1)
    except zlib.error as error:
        raise GateFailure(f"browser web screenshot pixels are invalid: {name}") from error
    if (
        len(pixels) != expected_size
        or decompressor.unconsumed_tail
        or decompressor.unused_data
        or not decompressor.eof
    ):
        raise GateFailure(f"browser web screenshot pixel size is invalid: {name}")
    if any(pixels[row * (1 + width * channels)] > 4 for row in range(height)):
        raise GateFailure(f"browser web screenshot filter is invalid: {name}")


def _validate_curl_log(contents: bytes) -> None:
    lines = _text_lines(contents, "curl.log")
    if len(lines) != 4:
        raise GateFailure("curl evidence does not have the exact record set")
    dns: dict[str, str] = {}
    https: dict[str, tuple[str, str]] = {}
    for line in lines:
        if match := _DNS_LINE.fullmatch(line):
            host, address = match.groups()
            octets = address.split(".")
            if len(octets) != 4 or any(not part.isdigit() or int(part) > 255 for part in octets):
                raise GateFailure("curl DNS evidence has an invalid address")
            if address.startswith(("0.", "127.")) or host in dns:
                raise GateFailure("curl DNS evidence is unsafe or duplicated")
            dns[host] = address
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
        else:
            raise GateFailure("curl evidence contains an unstructured record")
    if set(dns) != {"www.baidu.com", "www.bilibili.com"} or set(https) != {
        "https://www.baidu.com/",
        "https://www.bilibili.com/",
    }:
        raise GateFailure("curl evidence is missing a required endpoint")


def _validate_security_log(contents: bytes) -> dict[str, tuple[str, str]]:
    lines = _text_lines(contents, "security.log")
    parent_pid: str | None = None
    service_pid: str | None = None
    child_pids: set[str] = set()
    content_seen = False
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
        elif match := _CHILD_SECURITY_LINE.fullmatch(line):
            pid, role, seccomp = match.groups()
            if pid in child_pids:
                raise GateFailure("browser security child evidence is duplicated")
            child_pids.add(pid)
            if role == "content":
                content_seen = True
                if seccomp != "2":
                    raise GateFailure("browser content process is not seccomp filtered")
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
        or not child_pids
        or not content_seen
    ):
        raise GateFailure("browser security evidence is incomplete")
    return hashes


def _validate_firefox_logs(stderr: bytes, mozilla: bytes) -> None:
    for line in (stderr + b"\n" + mozilla).splitlines():
        if b"Exiting due to channel error." in line:
            raise GateFailure("Firefox log records a channel-error exit")
        if b"SCM_RIGHTS" in line and (
            re.search(rb"(^|[^A-Z])EPERM([^A-Z]|$)", line)
            or b"Operation not permitted" in line
        ):
            raise GateFailure("Firefox log records SCM_RIGHTS permission failure")


def validate_web_evidence(
    evidence: Mapping[str, bytes],
) -> dict[str, dict[str, object]]:
    """Validate the exact extracted browser evidence set and return its index."""

    if set(evidence) != set(WEB_EVIDENCE_PATHS):
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

    snapshots = {
        name: _decode_json(evidence[f"{name}.json"], f"{name}.json")
        for name in ("baidu-home", "baidu-search", "bilibili-home", "bilibili-detail")
    }
    validate_baidu_home(snapshots["baidu-home"])
    validate_baidu_search(snapshots["baidu-search"])
    selected = select_bilibili_video(snapshots["bilibili-home"])
    validate_bilibili_detail(snapshots["bilibili-detail"], selected)

    for name in ("baidu-home", "baidu-search", "bilibili-home", "bilibili-detail"):
        _validate_png(evidence[f"{name}.png"], name)

    _validate_curl_log(evidence["curl.log"])
    security_hashes = _validate_security_log(evidence["security.log"])
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

    return {
        name: {
            "sha256": hashlib.sha256(contents).hexdigest(),
            "size": len(contents),
        }
        for name, contents in sorted(evidence.items())
    }


def _extract_web_evidence(root_fd: int, directory: Path) -> dict[str, bytes]:
    extracted: dict[str, bytes] = {}
    image = f"/proc/self/fd/{root_fd}"
    deadline = time.monotonic() + WEB_EVIDENCE_EXTRACT_TIMEOUT
    for name, guest_path in WEB_EVIDENCE_PATHS.items():
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


def browser_web_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Admit exactly one default slirp backend and one virtio-net transport."""

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
    return argv


def classify_browser_web_qemu(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    clean = transcript.lower()
    if any(marker.lower() in clean for marker in KERNEL_FATAL_MARKERS):
        return GateResult(False, "kernel fatal marker", None)
    if _NETWORK_FAILURE.lower() in clean:
        return GateResult(False, "network guest failure", None)
    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=BROWSER_WEB_MILESTONES,
        failure_marker=_WEB_FAILURE,
    )


class BrowserWebQemuOperations(DesktopM5QemuOperations):
    SCHEMA_VERSION = 7
    PROFILE_NAME = "browser-web"
    ARTIFACT_PREFIX = "browser-web-qemu"
    MILESTONES = BROWSER_WEB_MILESTONES
    FAILURE_MARKER = _WEB_FAILURE
    ADDITIONAL_FAILURE_MARKERS = (_NETWORK_FAILURE, *KERNEL_FATAL_MARKERS)
    BOOTARGS = DESKTOP_M5_QEMU_BOOTARGS

    def __init__(self, config: GateConfig) -> None:
        super().__init__(config)
        self._web_evidence: dict[str, bytes] = {}
        self._web_evidence_index: dict[str, dict[str, object]] = {}

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return browser_web_qemu_argv(**arguments)

    def invalidate(self, config: GateConfig) -> None:
        super().invalidate(config)
        self._require_output().invalidate(
            *(f"browser-web-{name}" for name in WEB_EVIDENCE_PATHS),
            "browser-web-evidence.SHA256SUMS",
            "browser-web-evidence-index.json",
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
                self._web_evidence = _extract_web_evidence(root_fd, Path(temporary))
        finally:
            os.close(root_fd)
        self._web_evidence_index = validate_web_evidence(self._web_evidence)
        return super().hash_final_root(config, prepared)

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        output = self._require_output()
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
                "selected_bv_url": json.loads(
                    self._web_evidence["bilibili-detail.json"]
                )["url"],
            }
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
        config, operations, classifier=classify_browser_web_qemu
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), BrowserWebQemuOperations(config) as operations:
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
