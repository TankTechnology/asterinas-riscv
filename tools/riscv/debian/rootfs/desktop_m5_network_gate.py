#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Classify the Debian M5 wired-network evidence from a drained console log."""

from __future__ import annotations

from tools.riscv.debian.rootfs.desktop_m3_gate import classify_desktop
from tools.riscv.debian.rootfs.desktop_m4_gate import DESKTOP_M4_MILESTONES
from tools.riscv.debian.rootfs.gate_protocol import GateResult


DESKTOP_M5_MEGREZ_MILESTONES = (
    "DEBIAN_NETWORK_M5_LINK interface=eth0 address=10.100.19.200/21 state=lower-up",
    "DEBIAN_NETWORK_M5_GUEST_PING peer=10.100.19.216 count=10",
    "DEBIAN_NETWORK_M5_MEGREZ_DNS resolver=10.2.0.5 fallback=10.2.0.6 host=www.baidu.com",
    "DEBIAN_NETWORK_M5_MEGREZ_HTTPS host=www.baidu.com status=200 address=10.100.19.200",
    "DEBIAN_NETWORK_M5_MEGREZ_ASSET host=www.baidu.com resource=logo-png",
    "DEBIAN_NETWORK_M5_MEGREZ_READY mode=static-rj45",
)
DESKTOP_M5_NETWORK_MILESTONES = DESKTOP_M5_MEGREZ_MILESTONES
DESKTOP_M5_QEMU_MILESTONES = (
    "DEBIAN_NETWORK_M5_QEMU_DNS resolver=10.0.2.3 host=www.baidu.com",
    "DEBIAN_NETWORK_M5_QEMU_HTTPS host=www.baidu.com status=200 address=10.0.2.15",
    "DEBIAN_NETWORK_M5_QEMU_READY mode=qemu-slirp",
)


def classify_desktop_m5_network(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Require ordered link, address, and peer reachability evidence."""

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
