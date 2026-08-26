#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Classify the Debian M5 wired-network evidence from a drained console log."""

from __future__ import annotations

from tools.riscv.debian.rootfs.desktop_m3_gate import classify_desktop
from tools.riscv.debian.rootfs.gate_protocol import GateResult


DESKTOP_M5_NETWORK_MILESTONES = (
    "DEBIAN_NETWORK_M5_LINK interface=eth0 address=10.100.19.200/21 state=lower-up",
    "DEBIAN_NETWORK_M5_GUEST_PING peer=10.100.19.216 count=10",
    "DEBIAN_NETWORK_M5_READY interface=eth0",
)


def classify_desktop_m5_network(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Require ordered link, address, and peer reachability evidence."""

    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=DESKTOP_M5_NETWORK_MILESTONES,
        failure_marker=b"DEBIAN_NETWORK_M5_FAIL reason=",
    )
