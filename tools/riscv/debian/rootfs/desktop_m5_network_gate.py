#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Classify the Debian M5 wired-network evidence from a drained console log."""

from __future__ import annotations

from enum import Enum
import re

from tools.riscv.debian.rootfs.desktop_m3_gate import classify_desktop
from tools.riscv.debian.rootfs.desktop_m4_gate import DESKTOP_M4_MILESTONES
from tools.riscv.debian.rootfs.gate_protocol import GateResult


class NetworkMode(str, Enum):
    """The externally distinguishable web-network path under test."""

    PROXY = "proxy"
    DIRECT = "direct"

    def __str__(self) -> str:
        return self.value


NETWORK_LAYERS = (
    "link",
    "address",
    "neighbor",
    "reachability",
    "dns",
    "http",
    "https",
    "baidu-asset",
    "repeat",
    "medium",
)
_WEB_NETWORK_FAILURE_RE = re.compile(
    rb"DEBIAN_WEB_NETWORK_FAIL mode=([a-z]+) layer=([a-z-]+) "
    rb"reason=([^\r\n ]+)"
)


def classify_web_network(transcript: bytes, *, mode: NetworkMode) -> GateResult:
    """Require one ordered ten-layer transcript for exactly one network mode."""

    if not isinstance(transcript, bytes):
        return GateResult(False, "web network transcript must be bytes", None)
    if not isinstance(mode, NetworkMode):
        return GateResult(False, "web network mode must be a NetworkMode", None)

    failure = _WEB_NETWORK_FAILURE_RE.search(transcript)
    if failure is not None:
        failure_mode = failure.group(1).decode("ascii")
        layer = failure.group(2).decode("ascii")
        reason = failure.group(3).decode("ascii")
        if failure_mode != mode.value:
            return GateResult(False, "mixed web network modes", None)
        if layer not in NETWORK_LAYERS:
            return GateResult(False, "web network failure has unknown layer", None)
        return GateResult(
            False,
            f"web network {layer} failure: {reason}",
            None,
        )

    positions: list[int] = []
    for layer in NETWORK_LAYERS:
        marker = (
            f"DEBIAN_WEB_NETWORK_LAYER mode={mode.value} "
            f"layer={layer} status=pass"
        ).encode()
        if transcript.count(marker) != 1:
            return GateResult(
                False,
                f"missing or duplicate {layer} layer",
                None,
            )
        positions.append(transcript.find(marker))

    ready = (
        f"DEBIAN_WEB_NETWORK_READY mode={mode.value} "
        f"layers={len(NETWORK_LAYERS)}"
    ).encode()
    if transcript.count(ready) != 1:
        return GateResult(False, "missing or duplicate mode-qualified ready", None)
    positions.append(transcript.find(ready))

    foreign = NetworkMode.DIRECT if mode is NetworkMode.PROXY else NetworkMode.PROXY
    if f"DEBIAN_WEB_NETWORK_READY mode={foreign.value} ".encode() in transcript:
        return GateResult(False, "mixed web network modes", None)
    if positions != sorted(positions):
        return GateResult(False, "web network layers out of order", None)
    return GateResult(True, "pass", None)


DESKTOP_M5_MEGREZ_MILESTONES = (
    "DEBIAN_NETWORK_M5_LINK interface=eth0 address=10.100.19.200/21 state=lower-up",
    "DEBIAN_NETWORK_M5_MEGREZ_PROXY endpoint=10.100.19.216:17893",
    "DEBIAN_NETWORK_M5_STRESS requests=20 bytes=1310720 sha256=7daca2095d0438260fa849183dfc67faa459fdf4936e1bc91eec6b281b27e4c2 endpoint=10.100.19.216:17894",
    "DEBIAN_NETWORK_M5_CLOCK source=http-date proxy=10.100.19.216:17893",
    "DEBIAN_NETWORK_M5_MEGREZ_HTTPS host=www.baidu.com status=200 address=10.100.19.200 proxy=10.100.19.216:17893",
    "DEBIAN_NETWORK_M5_MEGREZ_ASSET host=www.baidu.com resource=logo-png proxy=10.100.19.216:17893",
    "DEBIAN_NETWORK_M5_MEGREZ_READY mode=static-rj45-host-proxy",
)
DESKTOP_M5_NETWORK_MILESTONES = DESKTOP_M5_MEGREZ_MILESTONES
DESKTOP_M5_QEMU_MILESTONES = (
    "DEBIAN_NETWORK_M5_STRESS requests=20 bytes=1310720 sha256=7daca2095d0438260fa849183dfc67faa459fdf4936e1bc91eec6b281b27e4c2 endpoint=10.0.2.2:17894",
    "DEBIAN_NETWORK_M5_QEMU_DNS resolver=10.0.2.3 host=www.baidu.com",
    "DEBIAN_NETWORK_M5_QEMU_HTTPS host=www.baidu.com status=200 address=10.0.2.15",
    "DEBIAN_NETWORK_M5_QEMU_READY mode=qemu-slirp",
)


def classify_desktop_m5_network(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Require ordered link, DNS, HTTPS, and asset evidence."""

    if any(
        transcript.count(marker.encode()) > 1 for marker in DESKTOP_M5_MEGREZ_MILESTONES
    ):
        return GateResult(False, "duplicate desktop milestone", None)
    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=DESKTOP_M5_NETWORK_MILESTONES,
        failure_marker=b"DEBIAN_NETWORK_M5_FAIL reason=",
    )


def classify_desktop_m5_qemu(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Require QEMU DNS/HTTPS evidence before the complete M4 desktop."""

    if b"debian_desktop_m4_fail reason=" in transcript.lower():
        return GateResult(False, "desktop guest failure", None)
    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=(*DESKTOP_M5_QEMU_MILESTONES, *DESKTOP_M4_MILESTONES),
        failure_marker=b"DEBIAN_NETWORK_M5_FAIL reason=",
    )


def classify_network_m5_qemu(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Require only ordered QEMU transfer, DNS, and HTTPS evidence."""

    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=DESKTOP_M5_QEMU_MILESTONES,
        failure_marker=b"DEBIAN_NETWORK_M5_FAIL reason=",
    )
